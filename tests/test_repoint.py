"""Tests for the S4 re-point reference (US outbound) and the agent-only evaluation section."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from src import evaluate, repoint


@pytest.fixture(scope="module")
def us(raw_dir: Path) -> pd.DataFrame:
    return repoint.load_us(raw_dir)


@pytest.fixture(scope="module")
def flags(cfg: dict, us: pd.DataFrame) -> pd.DataFrame:
    return repoint.party_flags(cfg, us)


def test_uses_label_free_upload(us: pd.DataFrame) -> None:
    assert "ground_truth" not in us.columns


def test_one_row_per_party(flags: pd.DataFrame, us: pd.DataFrame) -> None:
    counts = flags["party_type"].value_counts()
    assert counts["sender"] == us["sender_id"].nunique()
    assert counts["beneficiary"] == us["beneficiary_id"].nunique()
    assert not flags.duplicated(["party_type", "party_id"]).any()


def test_never_uses_country(flags: pd.DataFrame) -> None:
    assert "beneficiary_country" not in flags.columns


def test_planted_parties_flagged_ordinary_not(cfg: dict, us: pd.DataFrame, flags: pd.DataFrame, raw_dir: Path) -> None:
    truth = pd.read_csv(raw_dir / "eval" / "ground_truth.csv")
    det = repoint.detection_table(flags, repoint.party_truth(us, truth))
    planted = cfg["us_outbound"]["planted"]
    assert det.loc["sender", "planted"] == planted["structuring"]
    assert det.loc["beneficiary", "planted"] == planted["collector"]
    assert (det["planted_flagged"] == det["planted"]).all()
    assert (det["ordinary_flagged"] <= 0.01 * det["ordinary"]).all()


def test_window_is_30_days(cfg: dict) -> None:
    """Three near-threshold transfers flag only when they fall inside one 30-day window."""
    near = 0.95 * cfg["reporting_threshold_usd"]

    def frame(days: list[int]) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "txn_id": [f"T{i}" for i in range(len(days))],
                "sender_id": "U1",
                "beneficiary_id": "B1",
                "amount_usd": near,
                "send_date": pd.Timestamp("2026-01-01") + pd.to_timedelta(days, unit="D"),
            }
        )

    assert repoint.sender_near_threshold(cfg, frame([0, 10, 20])).loc["U1"] == 3
    assert repoint.sender_near_threshold(cfg, frame([0, 20, 40])).loc["U1"] == 2


def test_evaluate_reports_agent_alone(tables: dict, tmp_path: Path) -> None:
    alerts = tables["alerts"]
    alerts.to_csv(tmp_path / "alerts.csv", index=False)
    sent = alerts.head(50)
    legit = sent["ground_truth"].eq("none")
    results = pd.DataFrame(
        {
            "alert_id": sent["alert_id"],
            "classification": legit.map({True: "likely_legitimate", False: "needs_review"}),
            "typology": sent["ground_truth"],
        }
    )
    results.to_csv(tmp_path / "res.csv", index=False)
    _, report = evaluate.evaluate(tmp_path / "res.csv", tmp_path / "alerts.csv")
    assert "The agent alone (Medium/High alerts only)" in report
    assert "The 50 alerts the agent actually reviewed" in report
