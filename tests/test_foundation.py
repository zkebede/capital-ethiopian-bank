"""Tests for the S1 reference pipeline: cleaning, entity resolution, customer_activity."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from src import foundation
from src.clean import SCHEMA, cast, clean_table, normalise_phone, read_raw
from src.customer_activity import build_activity, fx_daily_rate
from src.entity_resolution import RULE_MATCHED, build_xref, matched_pairs

# ---------------------------------------------------------------------------
# Cleaning
# ---------------------------------------------------------------------------


def test_schema_covers_every_upload_column(raw_dir: Path) -> None:
    for name, cols in SCHEMA.items():
        assert list(read_raw(raw_dir / "upload" / f"{name}.csv").columns) == list(cols), name


def test_normalise_phone() -> None:
    s = pd.Series(["+251 91-234-5678", "251912345678", " +251912345678 ", "", None])
    out = normalise_phone(s.str.strip().replace("", None))
    assert out.iloc[:3].tolist() == ["+251912345678"] * 3
    assert out.iloc[3:].isna().all()


def test_cast_types() -> None:
    raw = pd.DataFrame({"a": [" 1.5 "], "b": ["TRUE"], "c": ["2026-01-02"], "d": ["2026-01-02 03:04:05"], "e": ["7"]})
    out = cast(raw, {"a": "Double", "b": "Boolean", "c": "Date", "d": "Timestamp", "e": "Integer"})
    assert out.loc[0, "a"] == 1.5
    assert bool(out.loc[0, "b"]) is True
    assert out.loc[0, "c"] == pd.Timestamp("2026-01-02")
    assert out.loc[0, "d"] == pd.Timestamp("2026-01-02 03:04:05")
    assert out.loc[0, "e"] == 7


def test_clean_drops_null_keys_and_exact_duplicates(raw_dir: Path, cfg: dict) -> None:
    raw = read_raw(raw_dir / "upload" / "alerts.csv").head(5)
    dirty = pd.concat([raw, raw.iloc[[0]], raw.iloc[[1]].assign(alert_id=None)], ignore_index=True)
    df, report = clean_table("alerts", dirty, cfg)
    assert (report.dropped_null_key, report.dropped_duplicates, report.rows_out) == (1, 1, 5)
    assert df["alert_id"].is_unique


def test_clean_adds_income_band(cleaned: dict) -> None:
    assert cleaned["customers"]["income_band"].isin(["low", "medium", "high"]).all()


def test_cleaning_keeps_every_row(cleaned: dict, tables: dict) -> None:
    for name, df in cleaned.items():
        assert len(df) == len(tables[name]), name


# ---------------------------------------------------------------------------
# Entity resolution
# ---------------------------------------------------------------------------


def _customers(rows: list[tuple[str, str, str, str]]) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=["customer_id", "full_name", "phone", "dob"])
    df["dob"] = pd.to_datetime(df["dob"])
    return df


def test_xref_merges_name_variants() -> None:
    xref = build_xref(
        _customers(
            [
                ("C000001", "Abebe Kebede", "+251900000001", "1990-01-01"),
                ("C000002", "Abebe K.", "+251900000001", "1990-01-01"),
                ("C000003", "Hana Girma", "+251900000002", "1985-05-05"),
                ("C000004", "Hana", "+251900000002", "1985-05-05"),
                ("C000005", "Selam Dawit", "+251900000003", "1970-07-07"),
                ("C000006", "Selam Dawit Haile", "+251900000003", "1970-07-07"),
            ]
        )
    ).set_index("customer_id")
    assert xref.loc["C000002", "resolved_entity_id"] == "E000001"
    assert xref.loc["C000004", "resolved_entity_id"] == "E000003"
    assert xref.loc["C000006", "resolved_entity_id"] == "E000005"
    assert (xref["match_rule"] == RULE_MATCHED).all()


def test_xref_keeps_shared_phone_strangers_apart() -> None:
    """Mule-ring style: same phone, different people → no merge."""
    xref = build_xref(
        _customers(
            [
                ("C000001", "Abebe Kebede", "+251900000001", "1990-01-01"),
                ("C000002", "Tigist Alemu", "+251900000001", "1992-02-02"),
                ("C000003", "Abebe Tadesse", "+251900000001", "1988-03-03"),
            ]
        )
    )
    assert xref["resolved_entity_id"].nunique() == 3
    assert not matched_pairs(xref)


def test_xref_missing_phone_is_single_record() -> None:
    df = _customers(
        [
            ("C000001", "Abebe Kebede", None, "1990-01-01"),
            ("C000002", "Abebe K.", None, "1990-01-01"),
        ]
    )
    assert build_xref(df)["cluster_size"].eq(1).all()


def test_xref_recovers_planted_pairs(cleaned: dict, tables: dict, cfg: dict) -> None:
    score = foundation.score_resolution(build_xref(cleaned["customers"]), tables["customers"])
    assert score.true_pairs == cfg["planted"]["duplicate_profile_pairs"]
    assert score.passed, score


# ---------------------------------------------------------------------------
# customer_activity
# ---------------------------------------------------------------------------


def test_activity_unions_all_sources(cleaned: dict) -> None:
    xref = build_xref(cleaned["customers"])
    daily = fx_daily_rate(cleaned["remittances"])
    act = build_activity(cleaned["remittances"], cleaned["wallet_txns"], cleaned["fx_conversions"], xref, daily)

    expected = len(cleaned["remittances"]) + len(cleaned["wallet_txns"]) + len(cleaned["fx_conversions"])
    assert len(act) == expected
    assert act["event_id"].is_unique
    assert act["resolved_entity_id"].notna().all()
    assert act["amount_usd"].notna().all() and (act["amount_usd"] > 0).all()
    assert set(act["source"]) == {"remittance", "wallet", "fx"}


def test_daily_rate_covers_every_wallet_day(cleaned: dict) -> None:
    days = set(fx_daily_rate(cleaned["remittances"])["day"])
    assert set(cleaned["wallet_txns"]["timestamp"].dt.normalize()) <= days
