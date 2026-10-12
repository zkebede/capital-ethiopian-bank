"""S2 risk features — reference version of the Foundry feature pipeline.

Usage::

    python -m src.features        # reads data/derived/ (run src.foundation first)

Builds, from the S1 outputs:

1. ``customer_links``   — pairs of customers sharing a phone or address
2. ``inflow_cashout``   — when each remittance was followed by the entity's next wallet outflow
3. ``rate_move_days``   — days the reference rate moved more than ``fx.rate_move_pct``
4. ``alert_features``   — the six features over the 30 days before each alert,
                          plus points, ``risk_score`` and ``risk_tier``

Features aggregate by ``resolved_entity_id``, so duplicate profiles roll up to one
person. Every threshold and weight comes from ``config.yaml → features``.

Fairness: no feature reads name, region, branch, or country. ``full_name`` is
used only in S1 to decide whether two records are the same person.
"""

from __future__ import annotations

import argparse
import operator
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from src.common import DEFAULT_CONFIG, DERIVED_DIR, LAUNDERING_TYPOLOGIES, NONE, RAW_DIR, load_config
from src.foundation import DATE_COLS, write_outputs

FEATURES = [
    "sender_fanin",
    "cashout_fast_share",
    "near_threshold_ct",
    "shared_id_links",
    "fx_rate_timing",
    "profile_mismatch",
]
OPS = {">=": operator.ge, "<=": operator.le, ">": operator.gt, "<": operator.lt}
TIERS = ("Low", "Medium", "High")


# ---------------------------------------------------------------------------
# Building blocks
# ---------------------------------------------------------------------------


def customer_links(customers: pd.DataFrame, xref: pd.DataFrame) -> pd.DataFrame:
    """Customer pairs sharing a phone or address: ``(customer_a, customer_b, shared_on, same_entity)``.

    ``same_entity`` marks pairs that S1 resolved to one person (duplicate profiles);
    those are a data-quality fact, not a risk link.
    """
    frames = []
    for col in ("phone", "address"):
        c = customers[["customer_id", col]].dropna()
        pairs = c.merge(c, on=col, suffixes=("_a", "_b"))
        pairs = pairs[pairs["customer_id_a"] < pairs["customer_id_b"]]
        frames.append(pairs[["customer_id_a", "customer_id_b"]].assign(shared_on=col))
    links = pd.concat(frames, ignore_index=True)
    if links.empty:
        return pd.DataFrame(
            {"customer_a": pd.Series(dtype=str), "customer_b": pd.Series(dtype=str), "shared_on": pd.Series(dtype=str)}
        ).assign(same_entity=pd.Series(dtype=bool))
    links = (
        links.groupby(["customer_id_a", "customer_id_b"])["shared_on"]
        .agg(lambda s: "+".join(sorted(s)))
        .reset_index()
        .rename(columns={"customer_id_a": "customer_a", "customer_id_b": "customer_b"})
    )
    entity = xref.set_index("customer_id")["resolved_entity_id"]
    links["same_entity"] = links["customer_a"].map(entity).eq(links["customer_b"].map(entity))
    return links


def shared_id_links(links: pd.DataFrame, xref: pd.DataFrame) -> pd.Series:
    """Per entity: number of *other* entities it shares a phone or address with."""
    entity = xref.set_index("customer_id")["resolved_entity_id"]
    risk = links[~links["same_entity"]]
    a, b = risk["customer_a"].map(entity), risk["customer_b"].map(entity)
    both = pd.concat([pd.DataFrame({"e": a, "other": b}), pd.DataFrame({"e": b, "other": a})])
    return both.drop_duplicates().groupby("e")["other"].size().rename("shared_id_links")


def inflow_cashout(activity: pd.DataFrame, max_hours: float) -> pd.DataFrame:
    """For each remittance inflow: when the same entity's next wallet outflow happened.

    Returns ``event_id, resolved_entity_id, event_ts, next_outflow_ts`` (null if none
    within ``max_hours``).
    """
    inflows = activity[activity["source"].eq("remittance")][["event_id", "resolved_entity_id", "event_ts"]]
    outflows = activity[activity["source"].eq("wallet")][["resolved_entity_id", "event_ts"]]
    outflows = outflows.rename(columns={"event_ts": "next_outflow_ts"})
    merged = pd.merge_asof(
        inflows.sort_values("event_ts"),
        outflows.sort_values("next_outflow_ts"),
        left_on="event_ts",
        right_on="next_outflow_ts",
        by="resolved_entity_id",
        direction="forward",
        tolerance=pd.Timedelta(hours=max_hours),
    )
    return merged


def rate_move_days(daily_rate: pd.DataFrame, pct: float) -> pd.DatetimeIndex:
    """Days whose reference rate changed by more than ``pct`` versus the previous day."""
    rate = daily_rate.sort_values("day").set_index("day")["etb_per_usd"]
    return rate.index[rate.pct_change().abs() > pct]


# ---------------------------------------------------------------------------
# Alert-level features
# ---------------------------------------------------------------------------


def alert_features(
    cfg: dict[str, Any],
    alerts: pd.DataFrame,
    customers: pd.DataFrame,
    xref: pd.DataFrame,
    activity: pd.DataFrame,
    daily_rate: pd.DataFrame,
    links: pd.DataFrame,
) -> pd.DataFrame:
    """The six features for each alert, over the window ending at ``created``."""
    fcfg = cfg["features"]
    win = pd.Timedelta(days=int(fcfg["window_days"]))
    lo, hi = (b * float(cfg["reporting_threshold_usd"]) for b in fcfg["near_threshold_band"])

    entity = xref.set_index("customer_id")["resolved_entity_id"]
    a = alerts[["alert_id", "customer_id", "created"]].assign(resolved_entity_id=lambda d: d["customer_id"].map(entity))

    cash = inflow_cashout(activity, float(fcfg["cashout_max_hours"]))[["event_id", "next_outflow_ts"]]
    moves = set(rate_move_days(daily_rate, float(cfg["fx"]["rate_move_pct"])))
    ev = activity.merge(cash, on="event_id", how="left")
    ev["is_move_day"] = ev["event_ts"].dt.normalize().isin(moves)

    joined = a.merge(ev, on="resolved_entity_id", how="inner")
    joined = joined[(joined["event_ts"] > joined["created"] - win) & (joined["event_ts"] <= joined["created"])]

    remit = joined[joined["source"].eq("remittance")].copy()
    # Only count cash-outs that had already happened when the alert was created.
    seen = remit["next_outflow_ts"].le(remit["created"])
    remit["cashout_hours"] = ((remit["next_outflow_ts"] - remit["event_ts"]).dt.total_seconds() / 3600).where(seen)
    fast = remit["cashout_hours"].le(float(fcfg["cashout_fast_hours"]))
    remit["fast_usd"] = remit["amount_usd"].where(fast, 0.0)
    fx = joined[joined["source"].eq("fx")]
    inflow = joined[joined["source"].isin(["remittance", "fx"])]

    out = a.set_index("alert_id")
    by_alert = remit.groupby("alert_id")
    out["sender_fanin"] = by_alert["counterparty"].nunique()
    # Value-weighted: ordinary family inflows in the same window can't dilute it.
    out["cashout_fast_share"] = (by_alert["fast_usd"].sum() / by_alert["amount_usd"].sum()).round(3)
    out["cashout_hours_p50"] = by_alert["cashout_hours"].median()  # context for the agent, not scored
    out["near_threshold_ct"] = remit[remit["amount_usd"].ge(lo) & remit["amount_usd"].lt(hi)].groupby("alert_id").size()
    out["fx_rate_timing"] = fx.groupby("alert_id")["is_move_day"].mean()
    out["inflow_usd_30d"] = inflow.groupby("alert_id")["amount_usd"].sum()
    out["shared_id_links"] = out["resolved_entity_id"].map(shared_id_links(links, xref))

    band = out["customer_id"].map(customers.set_index("customer_id")["income_band"])
    expected = band.map({k: float(v) for k, v in cfg["expected_monthly_inflow_usd"].items()})
    for col in ("sender_fanin", "near_threshold_ct", "shared_id_links", "inflow_usd_30d"):
        out[col] = out[col].fillna(0)
    out["profile_mismatch"] = (out["inflow_usd_30d"] / expected).round(2)
    out["cashout_hours_p50"] = out["cashout_hours_p50"].round(2)
    out["fx_rate_timing"] = out["fx_rate_timing"].astype(float).round(3)
    for col in ("sender_fanin", "near_threshold_ct", "shared_id_links"):
        out[col] = out[col].astype(int)
    return out.reset_index()


def score(features: pd.DataFrame, cfg: dict[str, Any]) -> pd.DataFrame:
    """Add ``pts_<feature>``, ``risk_score``, ``risk_tier`` and ``flags``. Null features never flag."""
    rules = cfg["features"]["rules"]
    out = features.copy()
    flagged = {}
    for feat in FEATURES:
        r = rules[feat]
        hit = OPS[r["op"]](out[feat], float(r["threshold"])).fillna(False).astype(bool)
        flagged[feat] = hit
        out[f"pts_{feat}"] = np.where(hit, int(r["points"]), 0)
    out["risk_score"] = sum(out[f"pts_{f}"] for f in FEATURES)
    tiers = cfg["features"]["tiers"]
    out["risk_tier"] = np.select(
        [out["risk_score"] >= tiers["High"], out["risk_score"] >= tiers["Medium"]], ["High", "Medium"], "Low"
    )
    flags = pd.DataFrame(flagged)
    out["flags"] = flags.apply(lambda row: ";".join(f for f in FEATURES if row[f]), axis=1)
    return out


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def reach_table(scored: pd.DataFrame, truth: pd.Series) -> pd.DataFrame:
    """Rows = ground-truth typology; columns = tier counts and share sent to the agent."""
    df = scored.assign(ground_truth=scored["alert_id"].map(truth))
    t = pd.crosstab(df["ground_truth"], df["risk_tier"]).reindex(columns=list(TIERS), fill_value=0)
    t = t.reindex([NONE, *LAUNDERING_TYPOLOGIES]).fillna(0).astype(int)
    t["to_agent"] = (t["Medium"] + t["High"]) / t[list(TIERS)].sum(axis=1)
    return t


def expected_md(cfg: dict[str, Any], scored: pd.DataFrame, links: pd.DataFrame, reach: pd.DataFrame) -> str:
    """Numbers the Foundry feature pipeline should match, plus tier reach by typology."""
    rules = cfg["features"]["rules"]
    susp = reach.drop(index=NONE)
    planted_reach = (susp["Medium"].sum() + susp["High"].sum()) / susp[list(TIERS)].sum().sum()
    legit_low = reach.loc[NONE, "Low"] / reach.loc[NONE, list(TIERS)].sum()
    lines = [
        f"# S2 expected results — {cfg['bank_name']}",
        "",
        f"Seed `{cfg['seed']}` · window {cfg['features']['window_days']} days before each alert · **synthetic data**.",
        "",
        "## Rules in effect",
        "",
        "| Feature | Flag | Points |",
        "|---|---|---:|",
    ]
    lines += [f"| `{f}` | {rules[f]['op']} {rules[f]['threshold']} | {rules[f]['points']} |" for f in FEATURES]
    lines += [
        "",
        f"Tiers: Low < {cfg['features']['tiers']['Medium']} · Medium < {cfg['features']['tiers']['High']} · High.",
        "",
        "## `customer_links`",
        "",
        f"- {len(links):,} pairs · {int(links['same_entity'].sum())} same-entity (duplicate profiles) · "
        f"{int((~links['same_entity']).sum())} risk links",
        f"- By identifier: " + ", ".join(f"{k} {v}" for k, v in links["shared_on"].value_counts().items()),
        "",
        "## `alert_features`",
        "",
        f"- {len(scored):,} alerts · tiers: "
        + ", ".join(f"{t} {int((scored['risk_tier'] == t).sum()):,}" for t in TIERS),
        "",
        "| Feature | Alerts flagged | Mean (non-null) |",
        "|---|---:|---:|",
    ]
    lines += [f"| `{f}` | {int((scored[f'pts_{f}'] > 0).sum()):,} | {scored[f].mean():,.2f} |" for f in FEATURES]
    lines += [
        "",
        "## Tier reach by ground truth",
        "",
        "Medium/High alerts go to the agent; Low alerts never do.",
        "",
        "| Ground truth | Low | Medium | High | Sent to agent |",
        "|---|---:|---:|---:|---:|",
    ]
    lines += [
        f"| {idx} | {r.Low} | {r.Medium} | {r.High} | {r.to_agent:.0%} |"
        for idx, r in zip(reach.index, reach.itertuples())
    ]
    lines += [
        "",
        f"- **Planted alerts sent to the agent: {planted_reach:.1%}** — the ceiling on agent recall (target ≥ 90%).",
        f"- **Legitimate alerts kept in Low: {legit_low:.1%}** — cleared without agent work.",
    ]
    return "\n".join(lines) + "\n"


def load_derived(derived: Path) -> dict[str, pd.DataFrame]:
    """Read the S1 outputs this step needs, with dates parsed."""
    specs = {
        "customers_clean": ["dob", "onboard_date"],
        "customer_xref": [],
        "customer_activity": ["event_ts"],
        "fx_daily_rate": ["day"],
        "alerts_clean": ["created"],
    }
    tables = {}
    for name, dates in specs.items():
        path = derived / f"{name}.csv"
        if not path.exists():
            raise SystemExit(f"{path} not found — run `python -m src.foundation` first")
        tables[name] = pd.read_csv(path, dtype={"phone": str}, parse_dates=dates)
    return tables


def main(argv: list[str] | None = None) -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--derived", default=str(DERIVED_DIR))
    parser.add_argument("--raw", default=str(RAW_DIR))
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    derived = Path(args.derived)
    t = load_derived(derived)
    links = customer_links(t["customers_clean"], t["customer_xref"])
    feats = alert_features(
        cfg,
        t["alerts_clean"],
        t["customers_clean"],
        t["customer_xref"],
        t["customer_activity"],
        t["fx_daily_rate"],
        links,
    )
    scored = score(feats, cfg)
    write_outputs({"customer_links": links, "alert_features": scored}, derived)

    truth = pd.read_csv(Path(args.raw) / "alerts.csv").set_index("alert_id")["ground_truth"]  # checking only
    reach = reach_table(scored, truth)
    report = expected_md(cfg, scored, links, reach)
    (derived / "_s2_expected.md").write_text(report, encoding="utf-8")
    print(report)
    print(f"Written to {derived / '_s2_expected.md'}")


if __name__ == "__main__":
    main()
