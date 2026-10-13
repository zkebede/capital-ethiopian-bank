"""Tests for the S2 feature pipeline: links, cash-out timing, rate moves, scoring, tier reach."""

from __future__ import annotations

import copy

import pandas as pd
import pytest

from src.customer_activity import build_activity, fx_daily_rate
from src.entity_resolution import build_xref
from src.features import (
    FEATURES,
    alert_features,
    customer_links,
    inflow_cashout,
    rate_move_days,
    reach_table,
    score,
    shared_id_links,
)
from src.gen_fx import daily_rates
from src.gen_fx import rate_move_days as planted_move_days


@pytest.fixture(scope="module")
def scored(cfg: dict, cleaned: dict) -> pd.DataFrame:
    xref = build_xref(cleaned["customers"])
    daily = fx_daily_rate(cleaned["remittances"])
    activity = build_activity(cleaned["remittances"], cleaned["wallet_txns"], cleaned["fx_conversions"], xref, daily)
    links = customer_links(cleaned["customers"], xref)
    feats = alert_features(cfg, cleaned["alerts"], cleaned["customers"], xref, activity, daily, links)
    return score(feats, cfg)


def _xref(ids: list[str], entities: list[str] | None = None) -> pd.DataFrame:
    return pd.DataFrame({"customer_id": ids, "resolved_entity_id": entities or ["E" + i[1:] for i in ids]})


# ---------------------------------------------------------------------------
# Building blocks
# ---------------------------------------------------------------------------


def test_customer_links_and_same_entity() -> None:
    customers = pd.DataFrame(
        {
            "customer_id": ["C1", "C2", "C3", "C4"],
            "phone": ["+1", "+1", "+2", "+2"],
            "address": ["a", "b", "c", "c"],
        }
    )
    links = customer_links(customers, _xref(["C1", "C2", "C3", "C4"], ["E1", "E1", "E3", "E4"]))
    by_pair = links.set_index(["customer_a", "customer_b"])
    assert bool(by_pair.loc[("C1", "C2"), "same_entity"]) is True
    assert by_pair.loc[("C3", "C4"), "shared_on"] == "address+phone"
    assert bool(by_pair.loc[("C3", "C4"), "same_entity"]) is False


def test_shared_id_links_ignores_duplicate_profiles() -> None:
    ids = ["C1", "C2", "C3", "C4"]
    customers = pd.DataFrame({"customer_id": ids, "phone": ["+9"] * 4, "address": list("abcd")})
    xref = _xref(ids, ["E1", "E1", "E3", "E4"])  # C1/C2 are one person
    counts = shared_id_links(customer_links(customers, xref), xref)
    assert counts["E1"] == 2 and counts["E3"] == 2 and counts["E4"] == 2


def test_inflow_cashout_finds_next_outflow_within_tolerance() -> None:
    t0 = pd.Timestamp("2026-01-01 10:00")
    activity = pd.DataFrame(
        {
            "event_id": ["R1", "W1", "R2"],
            "source": ["remittance", "wallet", "remittance"],
            "resolved_entity_id": ["E1", "E1", "E2"],
            "event_ts": [t0, t0 + pd.Timedelta(hours=3), t0],
        }
    )
    out = inflow_cashout(activity, max_hours=720).set_index("event_id")
    assert out.loc["R1", "next_outflow_ts"] == t0 + pd.Timedelta(hours=3)
    assert pd.isna(out.loc["R2", "next_outflow_ts"]), "different entity's outflow must not match"


def test_reference_rate_recovers_planted_move_days(cfg: dict, cleaned: dict) -> None:
    found = rate_move_days(fx_daily_rate(cleaned["remittances"]), cfg["fx"]["rate_move_pct"])
    assert set(found) == set(planted_move_days(cfg, daily_rates(cfg)))


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def test_score_thresholds_nulls_and_tiers(cfg: dict) -> None:
    c = copy.deepcopy(cfg)
    rules = c["features"]["rules"]
    base = {f: 0.0 for f in FEATURES}
    rows = pd.DataFrame(
        [
            base,  # nothing
            {**base, "sender_fanin": rules["sender_fanin"]["threshold"]},  # exactly at threshold
            {**base, "fx_rate_timing": None},  # null never flags
            {f: 10**6 for f in FEATURES},  # everything
        ]
    )
    out = score(rows, c)
    assert out.loc[0, "risk_score"] == 0 and out.loc[0, "risk_tier"] == "Low"
    assert out.loc[1, "risk_score"] == rules["sender_fanin"]["points"]
    assert out.loc[2, "risk_score"] == 0
    assert out.loc[3, "risk_tier"] == "High"
    assert out.loc[3, "flags"] == ";".join(FEATURES)


def test_cashout_after_alert_is_not_counted(cfg: dict) -> None:
    """A cash-out that happens after the alert was created must not count (no look-ahead)."""
    t0 = pd.Timestamp("2026-02-01 09:00")
    customers = pd.DataFrame({"customer_id": ["C000001"], "phone": ["+1"], "address": ["a"], "income_band": ["medium"]})
    xref = _xref(["C000001"])
    activity = pd.DataFrame(
        {
            "event_id": ["R1", "W1"],
            "source": ["remittance", "wallet"],
            "direction": ["inflow", "outflow"],
            "customer_id": ["C000001"] * 2,
            "resolved_entity_id": ["E000001"] * 2,
            "event_ts": [t0, t0 + pd.Timedelta(hours=2)],
            "amount_usd": [1000.0, 900.0],
            "amount_etb": [155000.0, 139500.0],
            "counterparty": ["Sender A", "EXT-1"],
            "detail": ["Sheba Send", "p2p"],
        }
    )
    daily = pd.DataFrame({"day": [t0.normalize()], "etb_per_usd": [155.0]})
    links = customer_links(customers, xref)

    def share(created: pd.Timestamp) -> float:
        alerts = pd.DataFrame({"alert_id": ["A1"], "customer_id": ["C000001"], "created": [created]})
        return alert_features(cfg, alerts, customers, xref, activity, daily, links).loc[0, "cashout_fast_share"]

    assert share(t0 + pd.Timedelta(hours=1)) == 0.0
    assert share(t0 + pd.Timedelta(hours=3)) == 1.0


# ---------------------------------------------------------------------------
# End to end on the small config
# ---------------------------------------------------------------------------


def test_planted_alerts_reach_the_agent(scored: pd.DataFrame, tables: dict) -> None:
    truth = tables["alerts"].set_index("alert_id")["ground_truth"]
    reach = reach_table(scored, truth)
    planted = reach.drop(index="none")
    share = (planted["Medium"].sum() + planted["High"].sum()) / planted[["Low", "Medium", "High"]].sum().sum()
    assert share >= 0.90, reach

    # Ordinary legitimate alerts stay Low; look-alikes are meant to reach the agent.
    alerts = tables["alerts"].set_index("alert_id")
    tier = scored.set_index("alert_id")["risk_tier"]
    ordinary = alerts["ground_truth"].eq("none") & alerts["scenario"].eq("none")
    lookalike = alerts["scenario"].ne("none")
    assert tier[ordinary[ordinary].index].eq("Low").mean() >= 0.95
    assert tier[lookalike[lookalike].index].ne("Low").mean() >= 0.5


@pytest.mark.parametrize(
    "typology, feature",
    [
        ("structuring", "near_threshold_ct"),
        ("collector", "sender_fanin"),
        ("pass_through", "cashout_fast_share"),
        ("mule_ring", "shared_id_links"),
        ("fx_out_of_profile", "fx_rate_timing"),
    ],
)
def test_each_typology_trips_its_primary_feature(
    scored: pd.DataFrame, tables: dict, typology: str, feature: str
) -> None:
    truth = scored["alert_id"].map(tables["alerts"].set_index("alert_id")["ground_truth"])
    hits = scored.loc[truth.eq(typology), f"pts_{feature}"] > 0
    assert hits.mean() >= 0.9, f"{typology}: {feature} flagged on {hits.mean():.0%}"


def test_features_never_read_protected_attributes(scored: pd.DataFrame) -> None:
    for col in ("full_name", "region", "branch", "sender_country", "beneficiary_country"):
        assert col not in scored.columns
