"""S1 data foundation — reference run of the Foundry pipeline.

Usage::

    python -m src.foundation          # reads data/raw/upload/, writes data/derived/

Steps (each mirrors a Pipeline Builder board, see docs/foundry_s1_guide.md):

1. Clean the six upload files            → ``<table>_clean.csv``
2. Entity resolution                     → ``customer_xref.csv``
3. Daily reference rate                  → ``fx_daily_rate.csv``
4. One event table across systems        → ``customer_activity.csv``

Then it scores entity resolution against the hidden labels (read from
``data/raw/customers.csv`` for checking only — they never feed the pipeline) and
writes ``data/derived/_s1_expected.md``: the numbers your Foundry build should match.
Exits non-zero if entity resolution misses a duplicate pair or merges a mule ring.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from src.clean import CleanReport, clean_all
from src.common import DEFAULT_CONFIG, DERIVED_DIR, RAW_DIR, fmt_ts, load_config
from src.customer_activity import build_activity, fx_daily_rate
from src.entity_resolution import build_xref, matched_pairs

DATE_COLS = {"dob", "onboard_date", "day"}  # written as YYYY-MM-DD, not timestamps


@dataclass
class ResolutionScore:
    """Entity resolution vs planted duplicate pairs."""

    true_pairs: int
    found_pairs: int
    correct: int
    false_merges: int
    merged_ring_members: int

    @property
    def passed(self) -> bool:
        """All planted pairs found, nothing else merged."""
        return self.correct == self.true_pairs and self.false_merges == 0 and self.merged_ring_members == 0


def truth_pairs(labelled_customers: pd.DataFrame) -> set[frozenset[str]]:
    """Planted duplicate pairs: duplicate_profile customers grouped by their shared phone."""
    dups = labelled_customers[labelled_customers["ground_truth"].eq("duplicate_profile")]
    return {frozenset(g) for g in dups.groupby("phone")["customer_id"].apply(list) if len(g) == 2}


def score_resolution(xref: pd.DataFrame, labelled_customers: pd.DataFrame) -> ResolutionScore:
    """Compare ``customer_xref`` with the planted duplicate pairs."""
    truth = truth_pairs(labelled_customers)
    found = matched_pairs(xref)
    rings = set(labelled_customers.loc[labelled_customers["ground_truth"].eq("mule_ring"), "customer_id"])
    merged = xref[xref["cluster_size"].gt(1) & xref["customer_id"].isin(rings)]
    return ResolutionScore(
        true_pairs=len(truth),
        found_pairs=len(found),
        correct=len(found & truth),
        false_merges=len(found - truth),
        merged_ring_members=len(merged),
    )


def write_outputs(tables: dict[str, pd.DataFrame], out: Path) -> None:
    """Write every derived table as CSV with stable timestamp formatting."""
    out.mkdir(parents=True, exist_ok=True)
    for name, df in tables.items():
        df = df.copy()
        for col in df.columns:
            if col in DATE_COLS:
                df[col] = df[col].dt.strftime("%Y-%m-%d")
            elif pd.api.types.is_datetime64_any_dtype(df[col]):
                df[col] = fmt_ts(df[col]).to_numpy()
        df.to_csv(out / f"{name}.csv", index=False, lineterminator="\n")


def expected_md(
    cfg: dict[str, Any],
    reports: list[CleanReport],
    xref: pd.DataFrame,
    activity: pd.DataFrame,
    score: ResolutionScore,
) -> str:
    """Render the numbers a correct Foundry build should reproduce."""
    lines = [
        f"# S1 expected results — {cfg['bank_name']}",
        "",
        f"Seed `{cfg['seed']}`. Compare these with your Foundry datasets after each board. " "**Synthetic data.**",
        "",
        "## Cleaning",
        "",
        "| Dataset | Rows in | Dropped: null key | Dropped: duplicates | Rows out |",
        "|---|---:|---:|---:|---:|",
    ]
    lines += [
        f"| `{r.table}_clean` | {r.rows_in:,} | {r.dropped_null_key} | {r.dropped_duplicates} | {r.rows_out:,} |"
        for r in reports
    ]

    clusters = xref[xref["cluster_size"] > 1]
    examples = clusters.sort_values("resolved_entity_id").groupby("resolved_entity_id")["customer_id"].apply(", ".join)
    lines += [
        "",
        "## Entity resolution (`customer_xref`)",
        "",
        f"- Rows: {len(xref):,} (one per customer)",
        f"- Distinct `resolved_entity_id`: {xref['resolved_entity_id'].nunique():,}",
        f"- Matched clusters: {clusters['resolved_entity_id'].nunique():,} "
        f"({len(clusters):,} customer records, sizes {sorted(clusters['cluster_size'].unique().tolist())})",
        f"- Against planted duplicates: {score.correct}/{score.true_pairs} pairs found, "
        f"{score.false_merges} false merges, {score.merged_ring_members} mule-ring members merged "
        f"→ **{'PASS' if score.passed else 'FAIL'}**",
        "",
        "Spot-check these clusters in Foundry:",
        "",
    ]
    lines += [f"- `{eid}`: {members}" for eid, members in examples.head(5).items()]

    by_source = activity.groupby("source").agg(
        rows=("event_id", "size"), usd=("amount_usd", "sum"), etb=("amount_etb", "sum")
    )
    lines += [
        "",
        "## `customer_activity`",
        "",
        f"- Rows: {len(activity):,} · distinct customers: {activity['customer_id'].nunique():,} · "
        f"distinct entities: {activity['resolved_entity_id'].nunique():,}",
        f"- Rows with no `resolved_entity_id`: {int(activity['resolved_entity_id'].isna().sum())}",
        "",
        "| Source | Rows | Total USD | Total ETB |",
        "|---|---:|---:|---:|",
    ]
    lines += [f"| {r.Index} | {r.rows:,} | {r.usd:,.0f} | {r.etb:,.0f} |" for r in by_source.itertuples()]
    lines += ["", "USD for wallet rows uses `fx_daily_rate`; totals may differ by rounding (< 0.1%)."]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--raw", default=str(RAW_DIR))
    parser.add_argument("--out", default=str(DERIVED_DIR))
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    raw, out = Path(args.raw), Path(args.out)

    clean, reports = clean_all(raw / "upload", cfg)
    xref = build_xref(clean["customers"])
    daily = fx_daily_rate(clean["remittances"])
    activity = build_activity(clean["remittances"], clean["wallet_txns"], clean["fx_conversions"], xref, daily)

    write_outputs(
        {
            **{f"{k}_clean": v for k, v in clean.items()},
            "customer_xref": xref,
            "fx_daily_rate": daily,
            "customer_activity": activity,
        },
        out,
    )

    labelled = pd.read_csv(raw / "customers.csv", dtype={"phone": str})
    score = score_resolution(xref, labelled)
    (out / "_s1_expected.md").write_text(expected_md(cfg, reports, xref, activity, score), encoding="utf-8")

    for r in reports:
        print(f"{r.table + '_clean':<22} {r.rows_out:>8,} rows")
    print(f"{'customer_xref':<22} {len(xref):>8,} rows · {xref['resolved_entity_id'].nunique():,} entities")
    print(f"{'customer_activity':<22} {len(activity):>8,} rows")
    status = "PASS" if score.passed else "FAIL"
    print(
        f"\n{status}  Entity resolution: {score.correct}/{score.true_pairs} planted pairs, "
        f"{score.false_merges} false merges, {score.merged_ring_members} mule-ring members merged"
    )
    print(f"Expected results written to {out / '_s1_expected.md'}")
    sys.exit(0 if score.passed else 1)


if __name__ == "__main__":
    main()
