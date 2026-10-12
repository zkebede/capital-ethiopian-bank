"""Entry point: generate every synthetic CSV from ``config.yaml``.

Usage::

    python -m src.generate                     # writes data/raw/
    python -m src.generate --out /tmp/x        # alternate output dir
    python -m src.generate --config other.yaml

Output layout::

    data/raw/<file>.csv              full files, including hidden ground_truth
    data/raw/upload/<file>.csv       same files with ground_truth removed → Foundry raw/
    data/raw/eval/ground_truth.csv   (file, key, ground_truth) → separate Foundry eval dataset
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path
from typing import Any

import pandas as pd

from src import gen_alerts, gen_customers, gen_fx, gen_remittances, gen_us_outbound, gen_wallets, typologies
from src.common import DEFAULT_CONFIG, RAW_DIR, fmt_ts, load_config

# file name → (primary key, timestamp columns)
FILES: dict[str, tuple[str, tuple[str, ...]]] = {
    "customers": ("customer_id", ()),
    "remittances": ("txn_id", ("payout_date",)),
    "wallet_txns": ("wallet_txn_id", ("timestamp",)),
    "fx_conversions": ("txn_id", ("timestamp",)),
    "alerts": ("alert_id", ("created",)),
    "us_outbound": ("txn_id", ("send_date",)),
}


def build(cfg: dict[str, Any]) -> dict[str, pd.DataFrame]:
    """Generate all tables in memory, in dependency order."""
    base = gen_customers.generate(cfg)
    rates = gen_fx.daily_rates(cfg)
    planted = typologies.plant_all(cfg, base, rates)
    customers = planted.customers

    remittances = gen_remittances.generate(cfg, customers, rates, planted.remittances)
    wallets = gen_wallets.generate(cfg, customers, remittances, planted.wallet_txns)
    fx = gen_fx.generate(cfg, customers, rates, planted.fx_conversions)
    alerts = gen_alerts.generate(cfg, customers, remittances, wallets, fx)
    us = gen_us_outbound.generate(cfg)
    return {
        "customers": customers,
        "remittances": remittances,
        "wallet_txns": wallets,
        "fx_conversions": fx,
        "alerts": alerts,
        "us_outbound": us,
    }


def write(tables: dict[str, pd.DataFrame], out: Path) -> None:
    """Write full, upload (no ground truth), and eval copies with byte-stable formatting."""
    (out / "upload").mkdir(parents=True, exist_ok=True)
    (out / "eval").mkdir(parents=True, exist_ok=True)
    truth = []
    for name, df in tables.items():
        key, ts_cols = FILES[name]
        df = df.copy()
        for col in ts_cols:
            df[col] = fmt_ts(df[col]).to_numpy()
        df.to_csv(out / f"{name}.csv", index=False, lineterminator="\n")
        df.drop(columns="ground_truth").to_csv(out / "upload" / f"{name}.csv", index=False, lineterminator="\n")
        truth.append(pd.DataFrame({"file": name, "key": df[key], "ground_truth": df["ground_truth"]}))
    pd.concat(truth, ignore_index=True).to_csv(out / "eval" / "ground_truth.csv", index=False, lineterminator="\n")


def main(argv: list[str] | None = None) -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--out", default=str(RAW_DIR))
    args = parser.parse_args(argv)

    started = time.perf_counter()
    cfg = load_config(args.config)
    tables = build(cfg)
    write(tables, Path(args.out))
    for name, df in tables.items():
        print(f"{name:<16} {len(df):>8,} rows")
    print(f"Wrote {args.out} in {time.perf_counter() - started:.1f}s (seed {cfg['seed']}, {cfg['bank_name']})")


if __name__ == "__main__":
    main()
