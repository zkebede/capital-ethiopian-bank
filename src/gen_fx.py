"""Daily ETB/USD rate series and the FX conversions file."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from src.common import NONE, daily_rate_lookup, random_timestamps, rng_for, window, zero_pad_ids


def daily_rates(cfg: dict[str, Any]) -> pd.Series:
    """Return the official daily rate (ETB per USD), indexed by calendar day.

    Ordinary days move by a small random walk that never exceeds
    ``fx.rate_move_pct``; exactly ``fx.jump_days`` planted days move by more.
    That keeps "days with a >2% rate move" a known, countable set.
    """
    fx = cfg["fx"]
    rng = rng_for(cfg, "fx_rates")
    win = window(cfg)
    n = win.days

    # Cap ordinary moves safely below the move threshold (in log space).
    cap = np.log1p(float(fx["rate_move_pct"])) * 0.75
    log_ret = np.clip(rng.normal(fx["daily_drift"], fx["daily_vol"], size=n), -cap, cap)
    log_ret[0] = 0.0

    jump_idx = rng.choice(np.arange(1, n), size=int(fx["jump_days"]), replace=False)
    lo, hi = fx["jump_size"]
    sign = rng.choice([-1.0, 1.0], size=len(jump_idx), p=[0.3, 0.7])
    log_ret[jump_idx] = sign * np.log1p(rng.uniform(lo, hi, size=len(jump_idx)))

    rate = float(cfg["fx_official_rate"]) * np.exp(np.cumsum(log_ret))
    days = pd.date_range(win.start, periods=n, freq="D")
    return pd.Series(np.round(rate, 4), index=days, name="rate")


def rate_move_days(cfg: dict[str, Any], rates: pd.Series) -> pd.DatetimeIndex:
    """Days whose rate moved more than ``fx.rate_move_pct`` vs the previous day."""
    change = rates.pct_change().abs()
    return rates.index[change > float(cfg["fx"]["rate_move_pct"])]


def generate(cfg: dict[str, Any], customers: pd.DataFrame, rates: pd.Series, planted: pd.DataFrame) -> pd.DataFrame:
    """Return ``rows.fx_conversions`` conversions: ordinary ones plus planted rows."""
    rng = rng_for(cfg, "fx")
    win = window(cfg)
    n_normal = int(cfg["rows"]["fx_conversions"]) - len(planted)
    if n_normal < 0:
        raise ValueError("Planted FX rows exceed rows.fx_conversions")

    lo, hi = cfg["fx"]["normal_usd"]
    usd = np.round(np.exp(rng.uniform(np.log(lo), np.log(hi), size=n_normal)), 2)
    normal = pd.DataFrame(
        {
            "customer_id": rng.choice(customers["customer_id"].to_numpy(), size=n_normal),
            "usd_in": usd,
            "timestamp": random_timestamps(rng, win, n_normal),
            "ground_truth": NONE,
        }
    )

    df = pd.concat([normal, planted[normal.columns]], ignore_index=True)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df["rate"] = daily_rate_lookup(rates, df["timestamp"])
    df["etb_out"] = np.round(df["usd_in"] * df["rate"], 2)
    df = df.sort_values(["timestamp", "customer_id", "usd_in"], kind="mergesort").reset_index(drop=True)
    df["txn_id"] = zero_pad_ids("FX", np.arange(1, len(df) + 1), 6)
    return df[["txn_id", "customer_id", "usd_in", "etb_out", "rate", "timestamp", "ground_truth"]]
