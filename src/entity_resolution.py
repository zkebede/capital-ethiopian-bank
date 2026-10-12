"""S1 entity resolution — builds ``customer_xref`` (customer_id → resolved_entity_id).

Rule (deliberately simple so Pipeline Builder can reproduce it exactly):

    Two customer records are the same person when they share
    **phone + date of birth + given name** (first token of the name, case-insensitive).

Every customer in a match group gets ``resolved_entity_id`` = ``E`` + the
smallest customer number in the group. In Pipeline Builder this is a window
``min(customer_id)`` partitioned by those three keys.

Why these keys: a duplicate profile keeps the person's phone, dob, and given name
while the father's name is abbreviated, dropped, or extended ("Abebe Kebede" →
"Abebe K."). Mule-ring members also share a phone or address, but they are
different people with different dobs and names, so they must **not** merge —
they become Customer ↔ Customer links in S2 instead.

Name is used here only to decide whether two records are the same person. It is
never a risk input.
"""

from __future__ import annotations

import pandas as pd

MATCH_KEYS = ["phone", "dob", "given_name"]
RULE_MATCHED = "phone+dob+given_name"
RULE_SINGLE = "single_record"


def given_name(full_name: pd.Series) -> pd.Series:
    """First token of the name, lower-cased."""
    return full_name.str.split(" ").str[0].str.lower()


def build_xref(customers: pd.DataFrame) -> pd.DataFrame:
    """Return ``customer_xref``: one row per customer with its resolved entity."""
    df = customers[["customer_id", "full_name", "phone", "dob"]].copy()
    df["given_name"] = given_name(df["full_name"])
    complete = df[MATCH_KEYS].notna().all(axis=1)

    group_min = df[complete].groupby(MATCH_KEYS)["customer_id"].transform("min")
    group_size = df[complete].groupby(MATCH_KEYS)["customer_id"].transform("size")
    df["anchor"] = group_min.reindex(df.index).fillna(df["customer_id"])
    df["cluster_size"] = group_size.reindex(df.index).fillna(1).astype(int)

    return pd.DataFrame(
        {
            "customer_id": df["customer_id"],
            "resolved_entity_id": "E" + df["anchor"].str[1:],
            "cluster_size": df["cluster_size"],
            "match_rule": df["cluster_size"].gt(1).map({True: RULE_MATCHED, False: RULE_SINGLE}),
        }
    )


def matched_pairs(xref: pd.DataFrame) -> set[frozenset[str]]:
    """All unordered customer pairs that resolve to the same entity."""
    pairs: set[frozenset[str]] = set()
    for members in xref[xref["cluster_size"] > 1].groupby("resolved_entity_id")["customer_id"].apply(list):
        for i, a in enumerate(members):
            for b in members[i + 1 :]:
                pairs.add(frozenset((a, b)))
    return pairs
