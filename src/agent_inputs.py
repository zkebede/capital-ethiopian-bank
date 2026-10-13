"""Agent inputs — exactly what the AIP Logic agent sees for each alert.

One JSON payload per alert, built only from behavioural facts known at the
alert's ``created`` time. This module is the contract for the Foundry
``agent_context`` dataset (see docs/foundry_s3_guide.md).

Fairness: payloads never contain names, region, branch, or any country. A test
enforces this against ``FORBIDDEN_KEYS``.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from src.features import FEATURES

FORBIDDEN_KEYS = {
    "full_name",
    "sender_name",
    "beneficiary_name",
    "counterparty",
    "region",
    "branch",
    "address",
    "phone",
    "dob",
    "sender_country",
    "beneficiary_country",
    "nationality",
    "ethnicity",
    "religion",
}
HISTORY_DAYS = 365
LINK_FLOW_DAYS = 90


def _window(joined: pd.DataFrame, days: int) -> pd.DataFrame:
    span = pd.Timedelta(days=days)
    return joined[(joined["event_ts"] > joined["created"] - span) & (joined["event_ts"] <= joined["created"])]


def history_context(
    cfg: dict[str, Any], alerts: pd.DataFrame, activity: pd.DataFrame, daily_rate: pd.DataFrame
) -> pd.DataFrame:
    """Per-alert history facts that separate look-alikes from typologies.

    ``alerts`` needs ``alert_id, resolved_entity_id, created``.
    """
    thr = float(cfg["reporting_threshold_usd"])
    lo = cfg["features"]["near_threshold_band"][0] * thr
    moves = daily_rate.sort_values("day").set_index("day")["etb_per_usd"].pct_change().abs()
    move_days = set(moves.index[moves > float(cfg["fx"]["rate_move_pct"])])

    joined = alerts[["alert_id", "resolved_entity_id", "created"]].merge(activity, on="resolved_entity_id")
    year = _window(joined, HISTORY_DAYS)
    month = _window(joined, int(cfg["features"]["window_days"]))

    remit_y = year[year["source"].eq("remittance")].assign(month=lambda d: d["event_ts"].dt.to_period("M"))
    per_month = remit_y.groupby(["alert_id", "month"]).agg(
        senders=("counterparty", "nunique"),
        near=("amount_usd", lambda s: int(((s >= lo) & (s < thr)).sum())),
    )
    remit_m = month[month["source"].eq("remittance")]
    fx_m = month[month["source"].eq("fx")]

    out = pd.DataFrame(index=alerts["alert_id"])
    out["inflow_count_30d"] = remit_m.groupby("alert_id").size()
    out["avg_inflow_usd_30d"] = remit_m.groupby("alert_id")["amount_usd"].mean().round(2)
    out["months_with_inflows_12m"] = remit_y.groupby("alert_id")["month"].nunique()
    out["months_with_10plus_senders_12m"] = (per_month["senders"] >= 10).groupby("alert_id").sum()
    out["months_with_3plus_near_threshold_12m"] = (per_month["near"] >= 3).groupby("alert_id").sum()
    out["payouts_at_or_above_threshold_12m"] = remit_y[remit_y["amount_usd"] >= thr].groupby("alert_id").size()
    out["fx_conversions_30d"] = fx_m.groupby("alert_id").size()
    out["fx_usd_30d"] = fx_m.groupby("alert_id")["amount_usd"].sum().round(2)
    out["fx_on_rate_move_day_30d"] = fx_m[fx_m["event_ts"].dt.normalize().isin(move_days)].groupby("alert_id").size()
    wallet_m = month[month["source"].eq("wallet")]
    out["wallet_outflow_etb_30d"] = wallet_m.groupby("alert_id")["amount_etb"].sum().round(2)
    counts = [c for c in out.columns if c not in ("avg_inflow_usd_30d", "fx_usd_30d", "wallet_outflow_etb_30d")]
    out[counts] = out[counts].fillna(0).astype(int)
    out[["fx_usd_30d", "wallet_outflow_etb_30d"]] = out[["fx_usd_30d", "wallet_outflow_etb_30d"]].fillna(0.0)
    return out.reset_index()


def link_flows(alerts: pd.DataFrame, activity: pd.DataFrame, links: pd.DataFrame, xref: pd.DataFrame) -> pd.DataFrame:
    """Wallet transfers between the alert's entity and entities it shares an identifier with (90 days)."""
    entity = xref.set_index("customer_id")["resolved_entity_id"]
    risk = links[~links["same_entity"].astype(bool)]
    a, b = risk["customer_a"].map(entity), risk["customer_b"].map(entity)
    pairs = pd.concat([pd.DataFrame({"e": a, "other": b}), pd.DataFrame({"e": b, "other": a})]).drop_duplicates()
    shared_on = pd.concat(
        [
            pd.DataFrame({"e": a, "shared_on": risk["shared_on"]}),
            pd.DataFrame({"e": b, "shared_on": risk["shared_on"]}),
        ]
    )

    wallets = activity[activity["source"].eq("wallet") & activity["counterparty"].astype(str).str.startswith("W")]
    wallets = wallets.assign(other=("C" + wallets["counterparty"].str[1:]).map(entity))
    linked = wallets.merge(pairs, left_on=["resolved_entity_id", "other"], right_on=["e", "other"])
    # Count transfers in both directions: sent by the alert's entity, or received from a linked entity.
    sent = linked[["resolved_entity_id", "event_ts", "amount_etb"]]
    received = linked[["other", "event_ts", "amount_etb"]].rename(columns={"other": "resolved_entity_id"})
    flows = pd.concat([sent, received], ignore_index=True)

    joined = alerts[["alert_id", "resolved_entity_id", "created"]].merge(flows, on="resolved_entity_id")
    recent = _window(joined, LINK_FLOW_DAYS)
    out = pd.DataFrame(index=alerts["alert_id"])
    out["transfers_with_linked_90d"] = recent.groupby("alert_id").size()
    out["transfers_with_linked_etb_90d"] = recent.groupby("alert_id")["amount_etb"].sum().round(2)
    kinds = shared_on.groupby("e")["shared_on"].agg(lambda s: "+".join(sorted(set("+".join(s).split("+")))))
    out["linked_on"] = alerts.set_index("alert_id")["resolved_entity_id"].map(kinds)
    out["transfers_with_linked_90d"] = out["transfers_with_linked_90d"].fillna(0).astype(int)
    out["transfers_with_linked_etb_90d"] = out["transfers_with_linked_etb_90d"].fillna(0.0)
    out["linked_on"] = out["linked_on"].fillna("none")
    return out.reset_index()


def is_holiday_period(cfg: dict[str, Any], ts: pd.Series) -> pd.Series:
    """True when ``ts`` falls within the lead-up to a configured holiday."""
    lead = pd.Timedelta(days=int(cfg["holiday_lead_days"]))
    days = ts.dt.normalize()
    hit = pd.Series(False, index=ts.index)
    for h in pd.to_datetime(cfg["holidays"]):
        hit |= (days >= h - lead) & (days <= h)
    return hit


def build_inputs(
    cfg: dict[str, Any],
    scored: pd.DataFrame,
    alerts: pd.DataFrame,
    customers: pd.DataFrame,
    xref: pd.DataFrame,
    activity: pd.DataFrame,
    daily_rate: pd.DataFrame,
    links: pd.DataFrame,
) -> pd.DataFrame:
    """One row per alert with every agent input column (flat; see ``to_payload``)."""
    base = scored.merge(alerts[["alert_id", "rule", "legacy_score"]], on="alert_id", how="left")
    base["created"] = pd.to_datetime(base["created"])
    cust = customers.set_index("customer_id")
    base["occupation"] = base["customer_id"].map(cust["occupation"])
    base["income_band"] = base["customer_id"].map(cust["income_band"])
    base["id_verified"] = base["customer_id"].map(cust["id_verified"]).astype(bool)
    onboard = pd.to_datetime(base["customer_id"].map(cust["onboard_date"]))
    base["tenure_months"] = ((base["created"] - onboard).dt.days // 30).astype(int)
    expected = {k: float(v) for k, v in cfg["expected_monthly_inflow_usd"].items()}
    base["expected_monthly_inflow_usd"] = base["income_band"].map(expected)
    base["is_holiday_period"] = is_holiday_period(cfg, base["created"])

    ctx = history_context(cfg, base, activity, daily_rate)
    flows = link_flows(base, activity, links, xref)
    out = base.merge(ctx, on="alert_id", how="left").merge(flows, on="alert_id", how="left")
    return out


def to_payload(row: pd.Series) -> dict[str, Any]:
    """The JSON object sent to the agent for one alert."""

    def clean(v: Any) -> Any:
        if isinstance(v, (np.integer,)):
            return int(v)
        if isinstance(v, (np.floating, float)):
            return None if pd.isna(v) else round(float(v), 3)
        if isinstance(v, (np.bool_,)):
            return bool(v)
        if isinstance(v, pd.Timestamp):
            return v.strftime("%Y-%m-%d %H:%M")
        return v

    payload = {
        "alert": {k: clean(row[k]) for k in ("alert_id", "rule", "legacy_score", "created")},
        "customer_profile": {
            k: clean(row[k])
            for k in ("occupation", "income_band", "expected_monthly_inflow_usd", "id_verified", "tenure_months")
        },
        "risk": {
            "risk_score": clean(row["risk_score"]),
            "risk_tier": row["risk_tier"],
            "flags": [] if pd.isna(row["flags"]) or row["flags"] == "" else str(row["flags"]).split(";"),
            "features_30d": {f: clean(row[f]) for f in FEATURES},
            "cashout_hours_p50_30d": clean(row["cashout_hours_p50"]),
            "inflow_usd_30d": clean(row["inflow_usd_30d"]),
        },
        "last_30_days": {
            k: clean(row[k])
            for k in (
                "inflow_count_30d",
                "avg_inflow_usd_30d",
                "wallet_outflow_etb_30d",
                "fx_conversions_30d",
                "fx_usd_30d",
                "fx_on_rate_move_day_30d",
            )
        },
        "last_12_months": {
            k: clean(row[k])
            for k in (
                "months_with_inflows_12m",
                "months_with_10plus_senders_12m",
                "months_with_3plus_near_threshold_12m",
                "payouts_at_or_above_threshold_12m",
            )
        },
        "linked_customers": {
            "count": clean(row["shared_id_links"]),
            "shared_identifiers": row["linked_on"],
            "transfers_with_linked_90d": clean(row["transfers_with_linked_90d"]),
            "transfers_with_linked_etb_90d": clean(row["transfers_with_linked_etb_90d"]),
        },
        "calendar": {"is_holiday_period": bool(row["is_holiday_period"])},
    }
    return payload


def payload_keys(payload: Any) -> set[str]:
    """Every key at every depth of a payload (for the fairness check)."""
    if isinstance(payload, dict):
        return set(payload) | set().union(*(payload_keys(v) for v in payload.values()))
    if isinstance(payload, list):
        return set().union(*(payload_keys(v) for v in payload)) if payload else set()
    return set()
