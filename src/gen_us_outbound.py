"""Generate a fictional US bank's outbound remittances to East Africa (the re-point demo).

``beneficiary_id`` is added beside ``beneficiary_name``: names alone collide too
often to identify one beneficiary, which the collector pattern depends on.
Beneficiary country is descriptive only and is never used as a risk signal.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from src.common import (
    EAST_AFRICAN_FAMILY,
    EAST_AFRICAN_GIVEN,
    NONE,
    holiday_day_weights,
    random_timestamps,
    rng_for,
    uniform_int,
    window,
    zero_pad_ids,
)
from src.typologies import month_starts

LARGE_LEGIT_SHARE = 0.005  # e.g. a house or business purchase, reported normally
LARGE_LEGIT_USD = (10000, 25000)


def _beneficiaries(cfg: dict[str, Any], rng: np.random.Generator, n: int) -> pd.DataFrame:
    countries = cfg["us_outbound"]["beneficiary_countries"]
    keys = list(countries)
    p = np.array([countries[k] for k in keys], dtype=float)
    given = rng.choice(EAST_AFRICAN_GIVEN, size=n)
    family = rng.choice(EAST_AFRICAN_FAMILY, size=n)
    return pd.DataFrame(
        {
            "beneficiary_id": zero_pad_ids("B", np.arange(1, n + 1), 6),
            "beneficiary_name": np.char.add(np.char.add(given.astype(str), " "), family.astype(str)),
            "beneficiary_country": rng.choice(keys, size=n, p=p / p.sum()),
        }
    )


def generate(cfg: dict[str, Any]) -> pd.DataFrame:
    """Return ``rows.us_outbound`` transfers with planted structuring and collector patterns."""
    rng = rng_for(cfg, "us_outbound")
    us = cfg["us_outbound"]
    win = window(cfg)
    threshold = float(cfg["reporting_threshold_usd"])
    n_senders = int(us["senders"])
    sender_ids = zero_pad_ids("U", np.arange(1, n_senders + 1), 5)

    # Each sender supports 1–2 beneficiaries.
    per_sender = rng.integers(1, 3, size=n_senders)
    bens = _beneficiaries(cfg, rng, int(per_sender.sum()))
    bens["sender_id"] = np.repeat(sender_ids, per_sender)

    planted_cfg = us["planted"]
    struct_senders = rng.choice(sender_ids, size=int(planted_cfg["structuring"]), replace=False)
    collector_bens = _beneficiaries(cfg, rng, int(planted_cfg["collector"]))
    collector_bens["beneficiary_id"] = zero_pad_ids("BX", np.arange(1, len(collector_bens) + 1), 4)

    p = cfg["typology_params"]
    months = month_starts(cfg)
    rows = []
    for sid in struct_senders:
        ben = bens[bens["sender_id"] == sid].iloc[0]
        for m in rng.choice(len(months), size=int(uniform_int(rng, p["structuring"]["active_months"])), replace=False):
            k = int(uniform_int(rng, p["structuring"]["payouts_per_month"]))
            for day in np.sort(rng.choice(28, size=k, replace=False)):
                amt = np.floor(threshold * rng.uniform(*p["structuring"]["band"]) / 10) * 10
                rows.append(
                    (
                        sid,
                        *ben[["beneficiary_id", "beneficiary_name", "beneficiary_country"]],
                        amt,
                        months[m] + pd.Timedelta(days=int(day), hours=float(rng.uniform(9, 18))),
                        "structuring",
                    )
                )
    for _, ben in collector_bens.iterrows():
        m = int(rng.integers(len(months)))
        n = int(uniform_int(rng, p["collector"]["senders_per_month"]))
        for sid in rng.choice(sender_ids, size=n, replace=False):
            amt = float(np.round(rng.uniform(*p["collector"]["amount_usd"]) / 5) * 5)
            rows.append(
                (
                    sid,
                    *ben[["beneficiary_id", "beneficiary_name", "beneficiary_country"]],
                    amt,
                    months[m] + pd.Timedelta(days=float(rng.uniform(0, 28))),
                    "collector",
                )
            )
    planted = pd.DataFrame(
        rows,
        columns=[
            "sender_id",
            "beneficiary_id",
            "beneficiary_name",
            "beneficiary_country",
            "amount_usd",
            "send_date",
            "ground_truth",
        ],
    )

    n_normal = int(cfg["rows"]["us_outbound"]) - len(planted)
    if n_normal < 0:
        raise ValueError("Planted US rows exceed rows.us_outbound")
    pick = bens.iloc[rng.choice(len(bens), size=n_normal)]
    lo, hi = us["amount_usd"]
    amount = np.round(np.exp(rng.uniform(np.log(lo), np.log(hi), size=n_normal)) / 5) * 5
    large = rng.random(n_normal) < LARGE_LEGIT_SHARE
    amount[large] = np.round(rng.uniform(*LARGE_LEGIT_USD, size=int(large.sum())), -2)
    normal = pd.DataFrame(
        {
            "sender_id": pick["sender_id"].to_numpy(),
            "beneficiary_id": pick["beneficiary_id"].to_numpy(),
            "beneficiary_name": pick["beneficiary_name"].to_numpy(),
            "beneficiary_country": pick["beneficiary_country"].to_numpy(),
            "amount_usd": amount,
            "send_date": random_timestamps(rng, win, n_normal, holiday_day_weights(cfg, win)),
            "ground_truth": NONE,
        }
    )

    df = pd.concat([normal, planted], ignore_index=True)
    df["send_date"] = pd.to_datetime(df["send_date"]).dt.round("s")
    df["amount_usd"] = df["amount_usd"].astype(float)
    df["ctr_flag"] = df["amount_usd"] >= threshold
    df = df.sort_values(["send_date", "sender_id", "amount_usd"], kind="mergesort").reset_index(drop=True)
    df["txn_id"] = zero_pad_ids("US", np.arange(1, len(df) + 1), 6)
    return df[
        [
            "txn_id",
            "sender_id",
            "beneficiary_id",
            "beneficiary_name",
            "beneficiary_country",
            "amount_usd",
            "send_date",
            "ctr_flag",
            "ground_truth",
        ]
    ]
