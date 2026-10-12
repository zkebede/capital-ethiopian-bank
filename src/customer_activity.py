"""S1 ``customer_activity`` — one event table across remittances, wallets, and FX.

Each row is one money movement for one customer, tagged with the customer's
``resolved_entity_id`` so duplicate profiles roll up to one person. Amounts are
given in both USD and ETB using ``fx_daily_rate``, a daily reference rate built
from the bank's own payouts (median ``rate_applied`` per day).

S2 features (``sender_fanin``, ``cashout_hours_p50``, …) aggregate this table.
"""

from __future__ import annotations

import pandas as pd

COLUMNS = [
    "event_id",
    "source",
    "direction",
    "customer_id",
    "resolved_entity_id",
    "event_ts",
    "amount_usd",
    "amount_etb",
    "counterparty",
    "detail",
]


def fx_daily_rate(remittances: pd.DataFrame) -> pd.DataFrame:
    """Median payout rate per calendar day: ``(day, etb_per_usd)``."""
    day = remittances["payout_date"].dt.normalize()
    rate = remittances.groupby(day)["rate_applied"].median()
    return rate.rename("etb_per_usd").rename_axis("day").reset_index()


def build_activity(
    remittances: pd.DataFrame,
    wallets: pd.DataFrame,
    fx: pd.DataFrame,
    xref: pd.DataFrame,
    daily_rate: pd.DataFrame,
) -> pd.DataFrame:
    """Union the three activity sources into one customer-level event table."""
    inflows = pd.DataFrame(
        {
            "event_id": remittances["txn_id"],
            "source": "remittance",
            "direction": "inflow",
            "customer_id": remittances["receiver_id"],
            "event_ts": remittances["payout_date"],
            "amount_usd": remittances["amount_usd"],
            "amount_etb": (remittances["amount_usd"] * remittances["rate_applied"]).round(2),
            "counterparty": remittances["sender_name"],
            "detail": remittances["mto"],
        }
    )

    rate = daily_rate.set_index("day")["etb_per_usd"]
    wallet_rate = wallets["timestamp"].dt.normalize().map(rate)
    outflows = pd.DataFrame(
        {
            "event_id": wallets["wallet_txn_id"],
            "source": "wallet",
            "direction": "outflow",
            "customer_id": wallets["customer_id"],
            "event_ts": wallets["timestamp"],
            "amount_usd": (wallets["amount_etb"] / wallet_rate).round(2),
            "amount_etb": wallets["amount_etb"],
            "counterparty": wallets["counterparty_wallet"],
            "detail": wallets["channel"],
        }
    )

    conversions = pd.DataFrame(
        {
            "event_id": fx["txn_id"],
            "source": "fx",
            "direction": "conversion",
            "customer_id": fx["customer_id"],
            "event_ts": fx["timestamp"],
            "amount_usd": fx["usd_in"],
            "amount_etb": fx["etb_out"],
            "counterparty": pd.NA,
            "detail": pd.NA,
        }
    )

    events = pd.concat([inflows, outflows, conversions], ignore_index=True)
    events = events.merge(xref[["customer_id", "resolved_entity_id"]], on="customer_id", how="left")
    events = events.sort_values(["event_ts", "event_id"], kind="mergesort").reset_index(drop=True)
    return events[COLUMNS]
