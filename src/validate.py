"""Data-quality checks to run before uploading to Foundry.

Usage::

    python -m src.validate            # checks data/raw/, writes data/raw/_summary.md
    python -m src.validate --skip-repro

Exits non-zero if any check fails.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from src import generate
from src.common import DEFAULT_CONFIG, LAUNDERING_TYPOLOGIES, NONE, RAW_DIR, load_config, window

AMOUNT_COLS = {
    "remittances": ["amount_usd", "rate_applied"],
    "wallet_txns": ["amount_etb"],
    "fx_conversions": ["usd_in", "etb_out", "rate"],
    "us_outbound": ["amount_usd"],
}
TOLERANCE = 0.02  # allowed deviation of the legitimate alert share


@dataclass
class Result:
    """Outcome of one check."""

    name: str
    passed: bool
    detail: str


def load(raw: Path) -> dict[str, pd.DataFrame]:
    """Read every generated CSV, parsing timestamps and keeping identifiers as text."""
    tables = {}
    for name, (_, ts_cols) in generate.FILES.items():
        tables[name] = pd.read_csv(raw / f"{name}.csv", dtype={"phone": str}, parse_dates=list(ts_cols))
    return tables


# ---------------------------------------------------------------------------
# Checks — each returns a Result
# ---------------------------------------------------------------------------


def check_primary_keys(t: dict[str, pd.DataFrame]) -> Result:
    """No nulls in primary keys; all keys unique."""
    bad = []
    for name, (key, _) in generate.FILES.items():
        col = t[name][key]
        if col.isna().any() or col.duplicated().any():
            bad.append(f"{name}.{key}: {int(col.isna().sum())} null, {int(col.duplicated().sum())} dup")
    return Result("Primary keys present and unique", not bad, "; ".join(bad) or "6 files")


def check_foreign_keys(t: dict[str, pd.DataFrame]) -> Result:
    """Every transaction's customer / receiver / wallet id exists in customers.csv."""
    ids = set(t["customers"]["customer_id"])
    wallets = {"W" + c[1:] for c in ids}
    refs = {
        "remittances.receiver_id": t["remittances"]["receiver_id"],
        "wallet_txns.customer_id": t["wallet_txns"]["customer_id"],
        "fx_conversions.customer_id": t["fx_conversions"]["customer_id"],
        "alerts.customer_id": t["alerts"]["customer_id"],
    }
    bad = {k: int((~v.isin(ids)).sum()) for k, v in refs.items()}
    cp = t["wallet_txns"]["counterparty_wallet"]
    internal = cp[cp.str.startswith("W")]
    bad["wallet_txns.counterparty_wallet"] = int((~internal.isin(wallets)).sum())
    bad["wallet_txns.wallet_id"] = int((~t["wallet_txns"]["wallet_id"].isin(wallets)).sum())
    bad = {k: v for k, v in bad.items() if v}
    return Result("Foreign keys resolve to customers", not bad, str(bad) if bad else "all ids resolve")


def check_amounts(t: dict[str, pd.DataFrame]) -> Result:
    """All monetary amounts and rates are strictly positive."""
    bad = {f"{f}.{c}": int((t[f][c] <= 0).sum()) for f, cols in AMOUNT_COLS.items() for c in cols}
    bad = {k: v for k, v in bad.items() if v}
    return Result("Amounts > 0", not bad, str(bad) if bad else "all positive")


def check_dates(t: dict[str, pd.DataFrame], cfg: dict[str, Any], today: pd.Timestamp) -> Result:
    """Transaction dates fall inside the config window and none are in the future."""
    win = window(cfg)
    bad = []
    for name, (_, ts_cols) in generate.FILES.items():
        for col in ts_cols:
            s = t[name][col]
            outside = int(((s < win.start) | (s >= win.end)).sum())
            future = int((s > today).sum())
            if outside or future or s.isna().any():
                bad.append(f"{name}.{col}: {outside} outside, {future} future")
    for col in ("dob", "onboard_date"):
        future = int((pd.to_datetime(t["customers"][col]) > today).sum())
        if future:
            bad.append(f"customers.{col}: {future} future")
    detail = "; ".join(bad) or f"{win.start.date()} → {(win.end - pd.Timedelta(days=1)).date()}"
    return Result("Dates within window, none in future", not bad, detail)


def mule_ring_groups(customers: pd.DataFrame) -> list[set[str]]:
    """Connected components of mule-ring customers linked by a shared phone or address."""
    ring = customers[customers["ground_truth"].eq("mule_ring")]
    parent = {c: c for c in ring["customer_id"]}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for col in ("phone", "address"):
        for members in ring.groupby(col)["customer_id"].apply(list):
            for other in members[1:]:
                parent[find(other)] = find(members[0])
    groups: dict[str, set[str]] = {}
    for c in parent:
        groups.setdefault(find(c), set()).add(c)
    return list(groups.values())


def planted_counts(t: dict[str, pd.DataFrame]) -> dict[str, int]:
    """Planted counts as they appear in the generated files."""
    c = t["customers"]["ground_truth"].value_counts()
    us = t["us_outbound"]
    return {
        "structuring": int(c.get("structuring", 0)),
        "collector": int(c.get("collector", 0)),
        "pass_through": int(c.get("pass_through", 0)),
        "mule_rings": len(mule_ring_groups(t["customers"])),
        "fx_out_of_profile": int(c.get("fx_out_of_profile", 0)),
        "duplicate_profile_pairs": int(c.get("duplicate_profile", 0)) // 2,
        "us_structuring": us.loc[us["ground_truth"].eq("structuring"), "sender_id"].nunique(),
        "us_collector": us.loc[us["ground_truth"].eq("collector"), "beneficiary_id"].nunique(),
    }


def check_planted_counts(t: dict[str, pd.DataFrame], cfg: dict[str, Any]) -> Result:
    """Planted counts match config.yaml."""
    expected = dict(cfg["planted"])
    expected.update({f"us_{k}": v for k, v in cfg["us_outbound"]["planted"].items()})
    actual = planted_counts(t)
    bad = {k: f"{actual[k]} != {v}" for k, v in expected.items() if actual[k] != int(v)}
    return Result(
        "Planted counts match config", not bad, str(bad) if bad else ", ".join(f"{k}={v}" for k, v in actual.items())
    )


def check_typology_shapes(t: dict[str, pd.DataFrame], cfg: dict[str, Any]) -> Result:
    """Planted rows actually exhibit their defining behaviour."""
    bad = []
    thr = float(cfg["reporting_threshold_usd"])
    lo, hi = cfg["typology_params"]["structuring"]["band"]
    r = t["remittances"]

    s = r[r["ground_truth"].eq("structuring")]
    if not s["amount_usd"].between(lo * thr, hi * thr).all():
        bad.append("structuring amounts outside band")
    per_month = s.groupby([s["receiver_id"], s["payout_date"].dt.to_period("M")]).size()
    if (per_month < 3).any():
        bad.append("structuring month with <3 payouts")

    col = r[r["ground_truth"].eq("collector")]
    senders = col.groupby([col["receiver_id"], col["payout_date"].dt.to_period("M")])["sender_name"].nunique()
    if (senders < cfg["typology_params"]["collector"]["senders_per_month"][0]).any():
        bad.append("collector month with too few senders")

    w = t["wallet_txns"]
    pt_out = w[w["ground_truth"].eq("pass_through")]
    pt_in = r[r["ground_truth"].eq("pass_through")]
    max_h = pd.Timedelta(hours=cfg["typology_params"]["pass_through"]["max_hours"])
    merged = pd.merge_asof(
        pt_out.sort_values("timestamp"),
        pt_in.sort_values("payout_date")[["receiver_id", "payout_date", "amount_usd", "rate_applied", "txn_id"]],
        left_on="timestamp",
        right_on="payout_date",
        left_by="customer_id",
        right_by="receiver_id",
    )
    if ((merged["timestamp"] - merged["payout_date"]) > max_h).any():
        bad.append("pass_through outflow later than max_hours")
    moved = merged.groupby("txn_id")["amount_etb"].sum() / merged.groupby("txn_id").apply(
        lambda g: g["amount_usd"].iloc[0] * g["rate_applied"].iloc[0], include_groups=False
    )
    if (moved < cfg["typology_params"]["pass_through"]["moved_share"][0] * 0.97).any():
        bad.append("pass_through moved <80% of inflow")

    return Result(
        "Typology rows match their definitions", not bad, "; ".join(bad) or "structuring, collector, pass_through"
    )


def lookalike_counts(t: dict[str, pd.DataFrame]) -> dict[str, int]:
    """Legitimate look-alikes as they appear in the generated files."""
    c = t["customers"]
    fam = c[c["scenario"].eq("family_shared_phone")]
    return {
        "community_collection": int(c["scenario"].eq("community_collection").sum()),
        "steady_trader": int(c["scenario"].eq("steady_trader").sum()),
        "family_shared_phone_groups": int(fam["phone"].nunique()),
    }


def check_lookalikes(t: dict[str, pd.DataFrame], cfg: dict[str, Any]) -> Result:
    """Look-alike counts match config, all are labelled legitimate, and each has an alert."""
    lk = cfg["lookalikes"]
    expected = {
        "community_collection": int(lk["community_collection"]["customers"]),
        "steady_trader": int(lk["steady_trader"]["customers"]),
        "family_shared_phone_groups": int(lk["family_shared_phone"]["groups"]),
    }
    actual = lookalike_counts(t)
    bad = [f"{k}: {actual[k]} != {v}" for k, v in expected.items() if actual[k] != v]
    c = t["customers"]
    flagged = c[c["scenario"].ne(NONE)]
    if flagged["ground_truth"].ne(NONE).any():
        bad.append("look-alike customer has a laundering label")
    a = t["alerts"]
    lk_alerts = a[a["scenario"].ne(NONE)]
    if lk_alerts["ground_truth"].ne(NONE).any():
        bad.append("look-alike alert not labelled none")
    missing = set(flagged["customer_id"]) - set(lk_alerts["customer_id"])
    if missing:
        bad.append(f"{len(missing)} look-alikes without an alert")
    detail = "; ".join(bad) or ", ".join(f"{k}={v}" for k, v in actual.items())
    return Result("Legitimate look-alikes planted as configured", not bad, detail)


def check_alert_share(t: dict[str, pd.DataFrame], cfg: dict[str, Any]) -> Result:
    """Legitimate share of legacy alerts ≈ configured share (±2%)."""
    share = float(t["alerts"]["ground_truth"].eq(NONE).mean())
    target = float(cfg["legacy_alert_false_positive_share"])
    ok = abs(share - target) <= TOLERANCE
    return Result("Legitimate alert share ≈ 90% (±2%)", ok, f"{share:.1%} legitimate (target {target:.0%})")


def check_mule_rings(t: dict[str, pd.DataFrame], cfg: dict[str, Any]) -> Result:
    """Each mule ring has 5–8 members sharing ≥1 identifier."""
    lo, hi = cfg["typology_params"]["mule_ring"]["size"]
    sizes = sorted(len(g) for g in mule_ring_groups(t["customers"]))
    ok = bool(sizes) and all(lo <= s <= hi for s in sizes)
    return Result(f"Mule rings have {lo}–{hi} members sharing an identifier", ok, f"ring sizes {sizes}")


def check_no_ground_truth_in_upload(raw: Path) -> Result:
    """Upload copies (for the Ontology) carry no hidden label columns."""
    leaked = [
        p.name
        for p in sorted((raw / "upload").glob("*.csv"))
        if set(generate.HIDDEN_COLS) & set(pd.read_csv(p, nrows=0).columns)
    ]
    count = len(list((raw / "upload").glob("*.csv")))
    ok = not leaked and count == len(generate.FILES)
    return Result(
        "Upload files exclude hidden labels", ok, f"leaked: {leaked}" if leaked else f"{count} upload files clean"
    )


def file_hashes(directory: Path) -> dict[str, str]:
    """SHA-256 of every generated CSV under ``directory`` (summary excluded)."""
    return {
        str(p.relative_to(directory)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(directory.rglob("*.csv"))
    }


def check_reproducible(raw: Path, config_path: str) -> Result:
    """Re-running with the same seed produces identical file hashes."""
    with tempfile.TemporaryDirectory() as tmp:
        generate.write(generate.build(load_config(config_path)), Path(tmp))
        fresh = file_hashes(Path(tmp))
    current = file_hashes(raw)
    # Only the files the generator writes: stray copies (e.g. "alerts 4.csv" from a sync tool) are ignored.
    diff = sorted(k for k in fresh if fresh[k] != current.get(k))
    return Result(
        "Same seed → identical file hashes", not diff, f"differs: {diff}" if diff else f"{len(fresh)} files match"
    )


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


def demo_pair(t: dict[str, pd.DataFrame]) -> pd.DataFrame | None:
    """Suggest two look-alike MULTIPLE_SENDERS alerts: one family, one collector.

    Takes the collector alert with the most senders in the 30 days before it,
    then the family alert (≤3 senders) with the closest legacy score, so the
    legacy queue genuinely cannot tell them apart but the evidence can.
    """
    a, r = t["alerts"], t["remittances"]

    def facts(al: pd.Series) -> tuple[int, float]:
        recent = r[
            r["receiver_id"].eq(al["customer_id"])
            & r["payout_date"].between(al["created"] - pd.Timedelta(days=30), al["created"])
        ]
        return recent["sender_name"].nunique(), round(float(recent["amount_usd"].sum()), 2)

    ms = a[a["rule"].eq("MULTIPLE_SENDERS")]
    bad = ms[ms["ground_truth"].eq("collector")]
    legit = ms[ms["ground_truth"].eq(NONE)]
    if bad.empty or legit.empty:
        return None
    bad_facts = bad.apply(facts, axis=1)
    best_bad = bad.loc[bad_facts.map(lambda f: f[0]).idxmax()]

    legit = legit.assign(gap=(legit["legacy_score"] - best_bad["legacy_score"]).abs()).sort_values(["gap", "alert_id"])
    best_legit = next((al for _, al in legit.head(50).iterrows() if facts(al)[0] <= 3), legit.iloc[0])

    pair = pd.DataFrame([best_bad, best_legit])[a.columns]
    pair_facts = [facts(al) for _, al in pair.iterrows()]
    pair["senders_30d"] = [f[0] for f in pair_facts]
    pair["inflow_usd_30d"] = [f[1] for f in pair_facts]
    return pair


def write_summary(t: dict[str, pd.DataFrame], cfg: dict[str, Any], results: list[Result], raw: Path) -> Path:
    """Write ``_summary.md`` with row counts, planted counts, and check results."""
    lines = [
        f"# Synthetic data summary — {cfg['bank_name']}",
        "",
        f"Seed `{cfg['seed']}` · window {cfg['start_date']} + {cfg['months']} months · "
        "**all figures are on synthetic data**.",
        "",
        "## Row counts",
        "",
        "| File | Rows | Planted rows |",
        "|---|---:|---:|",
    ]
    for name, df in t.items():
        lines.append(f"| `{name}.csv` | {len(df):,} | {int((df['ground_truth'] != NONE).sum()):,} |")

    lines += ["", "## Planted counts", "", "| Typology | Count |", "|---|---:|"]
    lines += [f"| {k} | {v} |" for k, v in planted_counts(t).items()]
    lines += ["", "## Legitimate look-alikes (ground truth = none)", "", "| Scenario | Count |", "|---|---:|"]
    lines += [f"| {k} | {v} |" for k, v in lookalike_counts(t).items()]

    alerts = t["alerts"]
    lines += [
        "",
        "## Legacy alert queue",
        "",
        f"- {len(alerts):,} alerts, {alerts['ground_truth'].eq(NONE).mean():.1%} legitimate.",
        "- Suspicious alerts by typology: "
        + ", ".join(
            f"{k} {v}"
            for k, v in alerts.loc[alerts["ground_truth"].isin(LAUNDERING_TYPOLOGIES), "ground_truth"]
            .value_counts()
            .items()
        ),
    ]

    pair = demo_pair(t)
    if pair is not None:
        lines += [
            "",
            "## Suggested demo pair (look-alike alerts)",
            "",
            "| Alert | Customer | Rule | Legacy score | Senders (30d) | Inflow USD (30d) | Ground truth |",
            "|---|---|---|---:|---:|---:|---|",
        ]
        for _, p in pair.iterrows():
            lines.append(
                f"| {p['alert_id']} | {p['customer_id']} | {p['rule']} | {p['legacy_score']} | "
                f"{p['senders_30d']} | {p['inflow_usd_30d']:,.0f} | {p['ground_truth']} |"
            )

    lines += ["", "## Validation", "", "| Check | Result | Detail |", "|---|---|---|"]
    lines += [f"| {r.name} | {'PASS' if r.passed else 'FAIL'} | {r.detail} |" for r in results]
    path = raw / "_summary.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def run(raw: Path, config_path: str, skip_repro: bool = False) -> list[Result]:
    """Run every check against ``raw`` and return the results."""
    cfg = load_config(config_path)
    t = load(raw)
    today = pd.Timestamp.now().normalize() + pd.Timedelta(days=1)
    checks: list[Callable[[], Result]] = [
        lambda: check_primary_keys(t),
        lambda: check_foreign_keys(t),
        lambda: check_amounts(t),
        lambda: check_dates(t, cfg, today),
        lambda: check_planted_counts(t, cfg),
        lambda: check_typology_shapes(t, cfg),
        lambda: check_lookalikes(t, cfg),
        lambda: check_alert_share(t, cfg),
        lambda: check_mule_rings(t, cfg),
        lambda: check_no_ground_truth_in_upload(raw),
    ]
    if not skip_repro:
        checks.append(lambda: check_reproducible(raw, config_path))
    results = [c() for c in checks]
    write_summary(t, cfg, results, raw)
    return results


def main(argv: list[str] | None = None) -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--raw", default=str(RAW_DIR))
    parser.add_argument("--skip-repro", action="store_true", help="skip the regenerate-and-hash check")
    args = parser.parse_args(argv)

    results = run(Path(args.raw), args.config, args.skip_repro)
    width = max(len(r.name) for r in results)
    for r in results:
        print(f"{'PASS' if r.passed else 'FAIL'}  {r.name:<{width}}  {r.detail}")
    print(f"\nSummary written to {Path(args.raw) / '_summary.md'}")
    sys.exit(0 if all(r.passed for r in results) else 1)


if __name__ == "__main__":
    main()
