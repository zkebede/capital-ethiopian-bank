"""Tests for the generator, each planted typology, validation, and evaluation.

Uses a scaled-down copy of config.yaml so the suite runs in seconds.
"""

from __future__ import annotations

import copy
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from src import evaluate, generate, typologies, validate
from src.common import LAUNDERING_TYPOLOGIES, NONE, load_config, rng_for
from src.gen_customers import generate as gen_customers
from src.gen_customers import occupation_bands
from src.gen_fx import daily_rates, rate_move_days


@pytest.fixture(scope="module")
def cfg() -> dict:
    """Small config: same parameters, fewer rows and planted cases."""
    c = copy.deepcopy(load_config())
    c["rows"] = {
        "customers": 3000,
        "remittances": 20000,
        "wallet_txns": 10000,
        "fx_conversions": 1000,
        "alerts": 1000,
        "us_outbound": 2000,
    }
    c["planted"] = {
        "structuring": 10,
        "collector": 5,
        "pass_through": 10,
        "mule_rings": 3,
        "fx_out_of_profile": 8,
        "duplicate_profile_pairs": 10,
    }
    c["us_outbound"]["senders"] = 600
    c["us_outbound"]["planted"] = {"structuring": 3, "collector": 2}
    return c


@pytest.fixture(scope="module")
def tables(cfg: dict) -> dict[str, pd.DataFrame]:
    return generate.build(cfg)


@pytest.fixture(scope="module")
def base(cfg: dict) -> tuple[pd.DataFrame, pd.Series]:
    return gen_customers(cfg), daily_rates(cfg)


# ---------------------------------------------------------------------------
# Whole-generator behaviour
# ---------------------------------------------------------------------------


def test_row_counts_match_config(cfg: dict, tables: dict) -> None:
    for name, df in tables.items():
        assert len(df) == cfg["rows"][name], name


def test_same_seed_is_identical(cfg: dict, tables: dict) -> None:
    again = generate.build(cfg)
    for name in tables:
        pd.testing.assert_frame_equal(tables[name], again[name], obj=name)


def test_different_seed_differs(cfg: dict, tables: dict) -> None:
    other = generate.build({**cfg, "seed": cfg["seed"] + 1})
    assert not tables["remittances"].equals(other["remittances"])


def test_every_file_has_ground_truth(tables: dict) -> None:
    for name, df in tables.items():
        assert "ground_truth" in df.columns, name


def test_ordinary_activity_dominates(tables: dict) -> None:
    """~97% of activity is ordinary family support."""
    assert tables["remittances"]["ground_truth"].eq(NONE).mean() >= 0.9


def test_upload_copies_drop_ground_truth(tables: dict, tmp_path: Path) -> None:
    generate.write(tables, tmp_path)
    for name in tables:
        assert "ground_truth" not in pd.read_csv(tmp_path / "upload" / f"{name}.csv", nrows=0).columns
    truth = pd.read_csv(tmp_path / "eval" / "ground_truth.csv")
    assert set(truth["file"]) == set(tables)


# ---------------------------------------------------------------------------
# One test per typology
# ---------------------------------------------------------------------------


def test_structuring(cfg: dict) -> None:
    rng = rng_for(cfg, "typologies")
    df = typologies.plant_structuring(cfg, rng, np.array(["C000001", "C000002"]))
    thr = cfg["reporting_threshold_usd"]
    assert df["amount_usd"].between(0.90 * thr, 0.99 * thr).all()
    per_month = df.groupby(["receiver_id", df["payout_date"].dt.to_period("M")])
    assert (per_month.size() >= 3).all()
    gaps = per_month["payout_date"].apply(lambda s: s.sort_values().diff().dt.days.dropna().min())
    assert (gaps >= 1).all(), "payouts should be a few days apart"


def test_collector(cfg: dict) -> None:
    rng = rng_for(cfg, "typologies")
    df = typologies.plant_collector(cfg, rng, np.array(["C000010"]))
    senders = df.groupby(df["payout_date"].dt.to_period("M"))["sender_name"].nunique()
    assert (senders >= 40).all()


def test_pass_through(cfg: dict, base: tuple) -> None:
    _, rates = base
    rng = rng_for(cfg, "typologies")
    remit, wallet = typologies.plant_pass_through(cfg, rng, np.array(["C000020"]), rates)
    remit = remit.sort_values("payout_date").reset_index(drop=True)
    for _, inflow in remit.iterrows():
        out = wallet[
            (wallet["timestamp"] >= inflow["payout_date"])
            & (wallet["timestamp"] <= inflow["payout_date"] + pd.Timedelta(hours=6))
        ]
        etb_in = inflow["amount_usd"] * rates[inflow["payout_date"].normalize()]
        assert out["amount_etb"].sum() >= 0.80 * etb_in


def test_mule_rings(cfg: dict, base: tuple) -> None:
    customers, _ = base
    rng = rng_for(cfg, "typologies")
    rings = [customers["customer_id"].to_numpy()[i : i + s] for i, s in ((0, 5), (10, 8))]
    out, wallet = typologies.plant_mule_rings(cfg, rng, customers, rings)
    for members in rings:
        ring = out[out["customer_id"].isin(members)]
        assert 5 <= len(ring) <= 8
        assert ring["phone"].nunique() == 1 or ring["address"].nunique() == 1
        txns = wallet[wallet["customer_id"].isin(members)]
        receivers = {w.replace("W", "C", 1) for w in txns["counterparty_wallet"]}
        assert receivers == set(members), "funds should cycle among ring members only"


def test_fx_out_of_profile(cfg: dict, base: tuple) -> None:
    customers, rates = base
    rng = rng_for(cfg, "typologies")
    picks = typologies.select_planted_customers(cfg, rng, customers)
    bands = occupation_bands(cfg)
    chosen = customers[customers["customer_id"].isin(picks["fx_out_of_profile"])]
    assert chosen["occupation"].map(bands).eq("low").all()

    df = typologies.plant_fx_out_of_profile(cfg, rng, picks["fx_out_of_profile"], rates)
    moves = rate_move_days(cfg, rates)
    assert len(moves) == cfg["fx"]["jump_days"]
    assert df["timestamp"].dt.normalize().isin(moves).all()
    assert (df["usd_in"] >= cfg["fx"]["planted_usd"][0]).all()


def test_duplicate_profiles(cfg: dict, base: tuple) -> None:
    customers, _ = base
    rng = rng_for(cfg, "typologies")
    pairs = customers["customer_id"].to_numpy()[:20].reshape(-1, 2)
    out = typologies.plant_duplicate_profiles(cfg, rng, customers, pairs).set_index("customer_id")
    for orig, dup in pairs:
        a, b = out.loc[orig], out.loc[dup]
        assert a["dob"] == b["dob"] and a["phone"] == b["phone"]
        assert a["branch"] != b["branch"]
        assert a["full_name"] != b["full_name"]
        assert a["full_name"].split(" ")[0] == b["full_name"].split(" ")[0]


def test_planted_sets_are_disjoint(cfg: dict, base: tuple) -> None:
    customers, _ = base
    picks = typologies.select_planted_customers(cfg, rng_for(cfg, "typologies"), customers)
    flat = np.concatenate(
        [picks[t] for t in ("structuring", "collector", "pass_through", "fx_out_of_profile")]
        + picks["mule_ring"]
        + [picks["duplicate_profile"].ravel()]
    )
    assert len(flat) == len(set(flat))


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------


def test_alert_legit_share(cfg: dict, tables: dict) -> None:
    share = tables["alerts"]["ground_truth"].eq(NONE).mean()
    assert abs(share - cfg["legacy_alert_false_positive_share"]) <= 0.02


def test_every_planted_customer_has_an_alert(tables: dict) -> None:
    c = tables["customers"]
    planted = set(c.loc[c["ground_truth"].isin(LAUNDERING_TYPOLOGIES), "customer_id"])
    alerted = set(tables["alerts"].loc[tables["alerts"]["ground_truth"].ne(NONE), "customer_id"])
    assert planted <= alerted


def test_alert_truth_matches_customer_truth(tables: dict) -> None:
    a = tables["alerts"].merge(
        tables["customers"][["customer_id", "ground_truth"]], on="customer_id", suffixes=("", "_c")
    )
    susp = a[a["ground_truth"].ne(NONE)]
    assert (susp["ground_truth"] == susp["ground_truth_c"]).all()


# ---------------------------------------------------------------------------
# Validation and evaluation end to end
# ---------------------------------------------------------------------------


def test_validate_passes(cfg: dict, tables: dict, tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    # Key order matters: weighted draws follow the config's order.
    config_path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    raw = tmp_path / "raw"
    generate.write(tables, raw)
    results = validate.run(raw, str(config_path))
    failed = [f"{r.name}: {r.detail}" for r in results if not r.passed]
    assert not failed, failed
    assert (raw / "_summary.md").exists()


def _results(alerts: pd.DataFrame, perfect: bool) -> pd.DataFrame:
    susp = alerts["ground_truth"].ne(NONE)
    if perfect:
        cls = np.where(susp, "likely_suspicious", "likely_legitimate")
        typ = alerts["ground_truth"]
    else:
        cls = np.full(len(alerts), "likely_legitimate")
        typ = np.full(len(alerts), NONE)
    return pd.DataFrame({"alert_id": alerts["alert_id"], "classification": cls, "typology": typ})


def test_evaluate_perfect_agent(tables: dict, tmp_path: Path) -> None:
    alerts = tables["alerts"]
    alerts.to_csv(tmp_path / "alerts.csv", index=False)
    _results(alerts, perfect=True).to_csv(tmp_path / "res.csv", index=False)
    m, report = evaluate.evaluate(tmp_path / "res.csv", tmp_path / "alerts.csv")
    assert m.precision == m.recall == m.caught_recall == m.legit_cleared == 1.0
    assert m.agent_fpr == 0.0 and m.typology_accuracy == 1.0
    assert "synthetic data" in report


def test_evaluate_unsent_alerts_count_as_legitimate(tables: dict, tmp_path: Path) -> None:
    alerts = tables["alerts"]
    alerts.to_csv(tmp_path / "alerts.csv", index=False)
    _results(alerts, perfect=True).iloc[:0].to_csv(tmp_path / "res.csv", index=False)
    m, _ = evaluate.evaluate(tmp_path / "res.csv", tmp_path / "alerts.csv")
    assert m.n_scored == 0 and m.caught_recall == 0.0 and m.legit_cleared == 1.0


def test_evaluate_rejects_bad_values(tables: dict, tmp_path: Path) -> None:
    alerts = tables["alerts"]
    alerts.to_csv(tmp_path / "alerts.csv", index=False)
    bad = _results(alerts, perfect=True).assign(classification="freeze")
    bad.to_csv(tmp_path / "res.csv", index=False)
    with pytest.raises(ValueError, match="Unexpected values"):
        evaluate.evaluate(tmp_path / "res.csv", tmp_path / "alerts.csv")
