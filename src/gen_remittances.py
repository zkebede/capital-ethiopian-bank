"""Generate inbound diaspora remittances: ordinary family support plus planted rows."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from src.common import (
    NONE,
    daily_rate_lookup,
    ethiopian_names,
    holiday_day_weights,
    random_timestamps,
    rng_for,
    sender_countries,
    uniform_int,
    window,
    zero_pad_ids,
)

HOLIDAY_AMOUNT_LIFT = 1.25  # people send a bit more before holidays
PAYOUT_SPREAD = (0.005, 0.02)  # payout rate sits slightly under the official rate


def sender_pairs(cfg: dict[str, Any], rng: np.random.Generator, receiver_ids: np.ndarray) -> pd.DataFrame:
    """Give each receiver 1–3 regular family senders, each with a habitual MTO and amount."""
    k = uniform_int(rng, cfg["normal"]["senders_per_receiver"], size=len(receiver_ids))
    n = int(k.sum())
    lo, hi = cfg["normal"]["amount_usd"]
    return pd.DataFrame(
        {
            "receiver_id": np.repeat(receiver_ids, k),
            "sender_name": ethiopian_names(rng, n),
            "sender_country": sender_countries(cfg, rng, n),
            "mto": rng.choice(cfg["mtos"], size=n),
            "base_amount": rng.uniform(lo, hi, size=n),
            "activity": rng.gamma(2.0, 1.0, size=n),
        }
    )


def generate(cfg: dict[str, Any], customers: pd.DataFrame, rates: pd.Series, planted: pd.DataFrame) -> pd.DataFrame:
    """Return about ``rows.remittances`` remittances (exactly, if planted rows fit)."""
    rng = rng_for(cfg, "remittances")
    win = window(cfg)
    n_normal = int(cfg["rows"]["remittances"]) - len(planted)
    if n_normal < 0:
        raise ValueError("Planted remittances exceed rows.remittances")

    receivers = customers.loc[customers["diaspora_flag"], "customer_id"].to_numpy()
    pairs = sender_pairs(cfg, rng, receivers)
    pick = rng.choice(len(pairs), size=n_normal, p=(pairs["activity"] / pairs["activity"].sum()).to_numpy())

    day_weights = holiday_day_weights(cfg, win)
    ts = random_timestamps(rng, win, n_normal, day_weights)
    is_holiday = day_weights[(ts.normalize() - win.start).days] > 1

    lo, hi = cfg["normal"]["amount_usd"]
    amount = pairs["base_amount"].to_numpy()[pick] * rng.uniform(0.85, 1.15, size=n_normal)
    amount = np.where(is_holiday, amount * HOLIDAY_AMOUNT_LIFT, amount)
    amount = np.round(np.clip(amount, lo, hi * HOLIDAY_AMOUNT_LIFT) / 5) * 5

    chosen = pairs.iloc[pick]
    normal = pd.DataFrame(
        {
            "receiver_id": chosen["receiver_id"].to_numpy(),
            "sender_name": chosen["sender_name"].to_numpy(),
            "sender_country": chosen["sender_country"].to_numpy(),
            "mto": chosen["mto"].to_numpy(),
            "amount_usd": amount,
            "payout_date": ts,
            "ground_truth": NONE,
        }
    )

    df = pd.concat([normal, planted[normal.columns]], ignore_index=True)
    df["payout_date"] = pd.to_datetime(df["payout_date"]).dt.round("s")
    df["amount_usd"] = df["amount_usd"].astype(float)
    spread = rng.uniform(*PAYOUT_SPREAD, size=len(df))
    df["rate_applied"] = np.round(daily_rate_lookup(rates, df["payout_date"]) * (1 - spread), 4)
    df = df.sort_values(["payout_date", "receiver_id", "amount_usd"], kind="mergesort").reset_index(drop=True)
    df["txn_id"] = zero_pad_ids("R", np.arange(1, len(df) + 1), 7)
    return df[
        [
            "txn_id",
            "receiver_id",
            "sender_name",
            "sender_country",
            "mto",
            "amount_usd",
            "payout_date",
            "rate_applied",
            "ground_truth",
        ]
    ]
