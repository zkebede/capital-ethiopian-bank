"""Generate the legacy rule-based alert queue.

About 9 in 10 legacy alerts are legitimate. Legacy scores for legitimate and
suspicious alerts overlap heavily on purpose: the legacy queue cannot tell a
family sending rent money from a hawala settlement ring, which is the problem
the app solves.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from src.common import LAUNDERING_TYPOLOGIES, NONE, holiday_day_weights, rng_for, window, zero_pad_ids

# Legacy rule that a typology most often trips. Legacy rules are noisy, so a
# suspicious alert carries its "natural" rule only part of the time.
NATURAL_RULE = {
    "structuring": "NEAR_THRESHOLD_PAYOUT",
    "collector": "MULTIPLE_SENDERS",
    "pass_through": "RAPID_FUNDS_MOVEMENT",
    "mule_ring": "HIGH_VALUE_P2P",
    "fx_out_of_profile": "LARGE_FX_CONVERSION",
}
NATURAL_RULE_SHARE = 0.7
LEGIT_RULE_WEIGHTS = {
    "VOLUME_SPIKE": 0.35,
    "MULTIPLE_SENDERS": 0.20,
    "RAPID_FUNDS_MOVEMENT": 0.15,
    "HIGH_VALUE_P2P": 0.10,
    "LARGE_FX_CONVERSION": 0.10,
    "NEAR_THRESHOLD_PAYOUT": 0.10,
}
RULES = list(LEGIT_RULE_WEIGHTS)


def planted_events(remittances: pd.DataFrame, wallets: pd.DataFrame, fx: pd.DataFrame) -> pd.DataFrame:
    """Every planted transaction as ``(customer_id, timestamp, typology)``."""
    parts = [
        remittances.rename(columns={"receiver_id": "customer_id", "payout_date": "timestamp"}),
        wallets,
        fx,
    ]
    ev = pd.concat([p[["customer_id", "timestamp", "ground_truth"]] for p in parts], ignore_index=True)
    ev = ev[ev["ground_truth"].isin(LAUNDERING_TYPOLOGIES)].rename(columns={"ground_truth": "typology"})
    return ev.sort_values(["customer_id", "timestamp"], kind="mergesort").reset_index(drop=True)


def _scores(rng: np.random.Generator, mean_sd: list[float], n: int) -> np.ndarray:
    return np.clip(np.round(rng.normal(mean_sd[0], mean_sd[1], size=n)), 1, 99).astype(int)


def _suspicious(cfg: dict[str, Any], rng: np.random.Generator, events: pd.DataFrame, n: int) -> pd.DataFrame:
    """One alert per planted customer first, then extra alerts on random planted customers."""
    win = window(cfg)
    customers = events["customer_id"].unique()
    if n <= len(customers):
        chosen = rng.choice(customers, size=n, replace=False)
    else:
        chosen = np.concatenate([customers, rng.choice(customers, size=n - len(customers))])

    grouped = events.groupby("customer_id", sort=True)
    first = grouped.indices
    picks = np.array([rng.choice(first[c]) for c in chosen])
    ev = events.iloc[picks].reset_index(drop=True)

    natural = ev["typology"].map(NATURAL_RULE).to_numpy()
    rule = np.where(rng.random(n) < NATURAL_RULE_SHARE, natural, rng.choice(RULES, size=n))
    created = ev["timestamp"] + pd.to_timedelta(rng.uniform(1, 72, size=n), unit="h")
    created = created.where(created < win.end, win.end - pd.Timedelta(minutes=1))
    return pd.DataFrame(
        {
            "customer_id": ev["customer_id"],
            "rule": rule,
            "legacy_score": _scores(rng, cfg["alerts"]["legacy_score_suspicious"], n),
            "created": created,
            "ground_truth": ev["typology"],
        }
    )


def _legitimate(
    cfg: dict[str, Any], rng: np.random.Generator, customers: pd.DataFrame, remittances: pd.DataFrame, n: int
) -> pd.DataFrame:
    """Legacy false positives, anchored on ordinary remittances and heavier around holidays."""
    win = window(cfg)
    clean_ids = customers.loc[~customers["ground_truth"].isin(LAUNDERING_TYPOLOGIES), "customer_id"]
    ordinary = remittances[remittances["ground_truth"].eq(NONE) & remittances["receiver_id"].isin(clean_ids)]
    day_w = holiday_day_weights(cfg, win)
    w = day_w[(ordinary["payout_date"].dt.normalize() - win.start).dt.days.to_numpy()]
    src = ordinary.iloc[rng.choice(len(ordinary), size=n, replace=False, p=w / w.sum())]

    created = src["payout_date"] + pd.to_timedelta(rng.uniform(1, 72, size=n), unit="h")
    created = created.where(created < win.end, win.end - pd.Timedelta(minutes=1))
    keys = list(LEGIT_RULE_WEIGHTS)
    p = np.array([LEGIT_RULE_WEIGHTS[k] for k in keys])
    return pd.DataFrame(
        {
            "customer_id": src["receiver_id"].to_numpy(),
            "rule": rng.choice(keys, size=n, p=p / p.sum()),
            "legacy_score": _scores(rng, cfg["alerts"]["legacy_score_legit"], n),
            "created": created.to_numpy(),
            "ground_truth": NONE,
        }
    )


def generate(
    cfg: dict[str, Any],
    customers: pd.DataFrame,
    remittances: pd.DataFrame,
    wallets: pd.DataFrame,
    fx: pd.DataFrame,
) -> pd.DataFrame:
    """Return ``rows.alerts`` alerts with a ``legacy_alert_false_positive_share`` legitimate share."""
    rng = rng_for(cfg, "alerts")
    n = int(cfg["rows"]["alerts"])
    n_susp = int(round(n * (1 - float(cfg["legacy_alert_false_positive_share"]))))

    df = pd.concat(
        [
            _suspicious(cfg, rng, planted_events(remittances, wallets, fx), n_susp),
            _legitimate(cfg, rng, customers, remittances, n - n_susp),
        ],
        ignore_index=True,
    )
    statuses = cfg["alerts"]["statuses"]
    keys = list(statuses)
    p = np.array([statuses[k] for k in keys], dtype=float)
    df["status"] = rng.choice(keys, size=len(df), p=p / p.sum())  # independent of ground truth
    df["created"] = pd.to_datetime(df["created"]).dt.round("s")
    df = df.sort_values(["created", "customer_id", "rule"], kind="mergesort").reset_index(drop=True)
    df["alert_id"] = zero_pad_ids("A", np.arange(1, len(df) + 1), 6)
    return df[["alert_id", "customer_id", "rule", "legacy_score", "created", "status", "ground_truth"]]
