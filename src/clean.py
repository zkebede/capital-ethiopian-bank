"""S1 cleaning — reference version of the Pipeline Builder cleaning step.

Reads the upload copies (no ground truth) exactly as Foundry sees them: every
column as text. Each table is cast to the types in ``SCHEMA``, strings are
trimmed and whitespace-collapsed, phones are normalised to E.164, rows with a
missing primary key or exact duplicates are dropped, and ``customers`` gains
``income_band`` from the occupation mapping in ``config.yaml``.

Keep this file and ``docs/foundry_s1_guide.md`` in step: every transform here
should have a matching Pipeline Builder board.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from src.gen_customers import occupation_bands

# Column → Foundry type. String ids/phones must stay String (inference reads
# "+2519…" as a number and drops the "+").
SCHEMA: dict[str, dict[str, str]] = {
    "customers": {
        "customer_id": "String",
        "full_name": "String",
        "dob": "Date",
        "phone": "String",
        "address": "String",
        "branch": "String",
        "region": "String",
        "id_type": "String",
        "id_verified": "Boolean",
        "occupation": "String",
        "onboard_date": "Date",
        "diaspora_flag": "Boolean",
    },
    "remittances": {
        "txn_id": "String",
        "receiver_id": "String",
        "sender_name": "String",
        "sender_country": "String",
        "mto": "String",
        "amount_usd": "Double",
        "payout_date": "Timestamp",
        "rate_applied": "Double",
    },
    "wallet_txns": {
        "wallet_txn_id": "String",
        "wallet_id": "String",
        "customer_id": "String",
        "counterparty_wallet": "String",
        "amount_etb": "Double",
        "timestamp": "Timestamp",
        "channel": "String",
    },
    "fx_conversions": {
        "txn_id": "String",
        "customer_id": "String",
        "usd_in": "Double",
        "etb_out": "Double",
        "rate": "Double",
        "timestamp": "Timestamp",
    },
    "alerts": {
        "alert_id": "String",
        "customer_id": "String",
        "rule": "String",
        "legacy_score": "Integer",
        "created": "Timestamp",
        "status": "String",
    },
    "us_outbound": {
        "txn_id": "String",
        "sender_id": "String",
        "beneficiary_id": "String",
        "beneficiary_name": "String",
        "beneficiary_country": "String",
        "amount_usd": "Double",
        "send_date": "Timestamp",
        "ctr_flag": "Boolean",
    },
}

PRIMARY_KEYS = {
    "customers": "customer_id",
    "remittances": "txn_id",
    "wallet_txns": "wallet_txn_id",
    "fx_conversions": "txn_id",
    "alerts": "alert_id",
    "us_outbound": "txn_id",
}


@dataclass
class CleanReport:
    """Row accounting for one table."""

    table: str
    rows_in: int
    dropped_null_key: int
    dropped_duplicates: int
    rows_out: int


def read_raw(path: Path) -> pd.DataFrame:
    """Read a CSV the way Foundry's raw dataset holds it: all text, empty → null."""
    return pd.read_csv(path, dtype=str, keep_default_na=False, na_values=[""])


def normalise_phone(s: pd.Series) -> pd.Series:
    """Keep digits only and return E.164 (``+2519…``); null if nothing is left."""
    digits = s.str.replace(r"\D", "", regex=True)
    return ("+" + digits).where(digits.str.len() > 0)


def cast(df: pd.DataFrame, schema: dict[str, str]) -> pd.DataFrame:
    """Trim strings and cast every column to its Foundry type."""
    out = pd.DataFrame(index=df.index)
    for col, typ in schema.items():
        s = df[col].str.strip().str.replace(r"\s+", " ", regex=True)
        if typ == "String":
            out[col] = s
        elif typ == "Double":
            out[col] = pd.to_numeric(s, errors="coerce").astype("float64")
        elif typ == "Integer":
            out[col] = pd.to_numeric(s, errors="coerce").astype("Int64")
        elif typ == "Boolean":
            out[col] = s.str.lower().map({"true": True, "false": False}).astype("boolean")
        elif typ == "Date":
            out[col] = pd.to_datetime(s, format="%Y-%m-%d", errors="coerce")
        elif typ == "Timestamp":
            out[col] = pd.to_datetime(s, format="%Y-%m-%d %H:%M:%S", errors="coerce")
        else:
            raise ValueError(f"Unknown type {typ} for {col}")
    return out


def clean_table(name: str, raw: pd.DataFrame, cfg: dict[str, Any]) -> tuple[pd.DataFrame, CleanReport]:
    """Clean one table and report how many rows were dropped and why."""
    missing = set(SCHEMA[name]) - set(raw.columns)
    if missing:
        raise ValueError(f"{name}: missing columns {sorted(missing)}")
    df = cast(raw, SCHEMA[name])
    if name == "customers":
        df["phone"] = normalise_phone(df["phone"])
        df["income_band"] = df["occupation"].map(occupation_bands(cfg))

    key = PRIMARY_KEYS[name]
    null_key = df[key].isna()
    df = df[~null_key]
    dupes = df.duplicated()
    df = df[~dupes].reset_index(drop=True)
    return df, CleanReport(name, len(raw), int(null_key.sum()), int(dupes.sum()), len(df))


def clean_all(upload_dir: Path, cfg: dict[str, Any]) -> tuple[dict[str, pd.DataFrame], list[CleanReport]]:
    """Clean every upload file."""
    tables, reports = {}, []
    for name in SCHEMA:
        tables[name], report = clean_table(name, read_raw(upload_dir / f"{name}.csv"), cfg)
        reports.append(report)
    return tables, reports
