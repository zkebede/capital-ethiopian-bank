"""Generate mobile-wallet transactions: ordinary cash-outs and spending plus planted rows.

Each customer holds one wallet (``C000123`` → ``W000123``). A row is money
leaving that wallet to ``counterparty_wallet``, which is another customer's
wallet (``W…``), a cash-out agent (``AGENT-…``), a merchant (``MERCH-…``), or a
wallet at another provider (``EXT-…``).

``wallet_txn_id`` is the primary key.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from src.common import NONE, random_timestamps, rng_for, window, zero_pad_ids
from src.typologies import wallet_of

CASHOUT_SHARE = 0.6  # share of ordinary wallet rows that are remittance cash-outs
CASHOUT_FRACTION = (0.3, 1.0)  # share of a remittance a family cashes out at once
N_AGENTS = 500
N_MERCHANTS = 800
SPEND_ETB = (100, 20000)


def _cashouts(cfg: dict[str, Any], rng: np.random.Generator, remittances: pd.DataFrame, n: int) -> pd.DataFrame:
    """Families cash out part of an ordinary remittance 3–30 days after payout."""
    win = window(cfg)
    lo_d, hi_d = cfg["normal"]["cashout_days"]
    eligible = remittances[
        remittances["ground_truth"].eq(NONE) & (remittances["payout_date"] < win.end - pd.Timedelta(days=hi_d + 1))
    ]
    src = eligible.iloc[rng.choice(len(eligible), size=n, replace=n > len(eligible))]
    delay = pd.to_timedelta(rng.uniform(lo_d, hi_d, size=n), unit="D")
    etb = src["amount_usd"].to_numpy() * src["rate_applied"].to_numpy() * rng.uniform(*CASHOUT_FRACTION, size=n)
    return pd.DataFrame(
        {
            "customer_id": src["receiver_id"].to_numpy(),
            "counterparty_wallet": zero_pad_ids("AGENT-", rng.integers(1, N_AGENTS + 1, size=n), 4),
            "amount_etb": np.round(etb, 2),
            "timestamp": (pd.DatetimeIndex(src["payout_date"]) + delay).round("s"),
            "channel": "agent_cashout",
            "ground_truth": NONE,
        }
    )


def _spending(cfg: dict[str, Any], rng: np.random.Generator, customers: pd.DataFrame, n: int) -> pd.DataFrame:
    """Everyday merchant payments and person-to-person transfers."""
    win = window(cfg)
    ids = customers["customer_id"].to_numpy()
    p2p = rng.random(n) < 0.5
    counterparty = np.where(
        p2p,
        wallet_of(rng.choice(ids, size=n)),
        zero_pad_ids("MERCH-", rng.integers(1, N_MERCHANTS + 1, size=n), 4),
    )
    lo, hi = SPEND_ETB
    return pd.DataFrame(
        {
            "customer_id": rng.choice(ids, size=n),
            "counterparty_wallet": counterparty,
            "amount_etb": np.round(np.exp(rng.uniform(np.log(lo), np.log(hi), size=n)), 2),
            "timestamp": random_timestamps(rng, win, n),
            "channel": np.where(p2p, "p2p", "merchant"),
            "ground_truth": NONE,
        }
    )


def generate(
    cfg: dict[str, Any], customers: pd.DataFrame, remittances: pd.DataFrame, planted: pd.DataFrame
) -> pd.DataFrame:
    """Return ``rows.wallet_txns`` wallet transactions."""
    rng = rng_for(cfg, "wallets")
    n_normal = int(cfg["rows"]["wallet_txns"]) - len(planted)
    if n_normal < 0:
        raise ValueError("Planted wallet rows exceed rows.wallet_txns")
    n_cash = int(n_normal * CASHOUT_SHARE)

    df = pd.concat(
        [
            _cashouts(cfg, rng, remittances, n_cash),
            _spending(cfg, rng, customers, n_normal - n_cash),
            planted,
        ],
        ignore_index=True,
    )
    df["timestamp"] = pd.to_datetime(df["timestamp"]).dt.round("s")
    # Self-transfers are meaningless; point them at a merchant instead.
    self_tx = df["counterparty_wallet"].eq(wallet_of(df["customer_id"]))
    df.loc[self_tx, "counterparty_wallet"] = zero_pad_ids(
        "MERCH-", rng.integers(1, N_MERCHANTS + 1, int(self_tx.sum())), 4
    )
    df.loc[self_tx, "channel"] = "merchant"

    df["wallet_id"] = wallet_of(df["customer_id"])
    df = df.sort_values(["timestamp", "customer_id", "amount_etb"], kind="mergesort").reset_index(drop=True)
    df["wallet_txn_id"] = zero_pad_ids("WT", np.arange(1, len(df) + 1), 7)
    return df[
        [
            "wallet_txn_id",
            "wallet_id",
            "customer_id",
            "counterparty_wallet",
            "amount_etb",
            "timestamp",
            "channel",
            "ground_truth",
        ]
    ]
