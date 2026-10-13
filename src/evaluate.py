"""Score AIP Logic agent output against hidden ground truth.

Usage::

    python -m src.evaluate                                    # data/exports/agent_results.csv
    python -m src.evaluate --results path/to/results.csv

Input: an export with ``alert_id, classification, typology`` (``confidence`` and
``case_note`` are used when present) plus ``ground_truth`` from ``alerts.csv``.

Alerts with no agent result were never sent to the agent (Low risk tier) and are
treated as ``likely_legitimate``; the report says how many.

Writes ``data/exports/eval_report.md``. All metrics are **on synthetic data**.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
from sklearn.metrics import precision_recall_fscore_support

from src.common import EXPORTS_DIR, LAUNDERING_TYPOLOGIES, NONE, RAW_DIR

CLASSIFICATIONS = ("likely_legitimate", "needs_review", "likely_suspicious")
TYPOLOGIES = (NONE, *LAUNDERING_TYPOLOGIES)
NOT_SENT = "not_sent_to_agent"
RECALL_TARGET = 0.90
LEGIT_CLEARED_TARGET = 0.80
WORST_N = 10


@dataclass
class Metrics:
    """Headline numbers for the report."""

    n_alerts: int
    n_scored: int
    precision: float
    recall: float
    f1: float
    caught_recall: float
    legit_cleared: float
    legacy_fp_share: float
    agent_fpr: float
    agent_review_rate: float
    flagged_fp_share: float
    typology_accuracy: float


def load_results(path: Path) -> pd.DataFrame:
    """Read and sanity-check the agent export."""
    return load_results_frame(pd.read_csv(path), source=str(path))


def load_results_frame(res: pd.DataFrame, source: str = "results") -> pd.DataFrame:
    """Sanity-check agent results already in memory (normalises case and blanks)."""
    res = res.copy()
    missing = {"alert_id", "classification", "typology"} - set(res.columns)
    if missing:
        raise ValueError(f"{source} is missing columns: {sorted(missing)}")
    res["classification"] = res["classification"].str.strip().str.lower()
    res["typology"] = res["typology"].fillna(NONE).str.strip().str.lower()
    bad_cls = set(res["classification"]) - set(CLASSIFICATIONS)
    bad_typ = set(res["typology"]) - set(TYPOLOGIES)
    if bad_cls or bad_typ:
        raise ValueError(f"Unexpected values — classification: {sorted(bad_cls)}, typology: {sorted(bad_typ)}")
    if res["alert_id"].duplicated().any():
        raise ValueError("Duplicate alert_id in agent results")
    return res


def join(alerts: pd.DataFrame, results: pd.DataFrame) -> pd.DataFrame:
    """Left-join results onto every alert; unsent alerts default to likely_legitimate / none."""
    unknown = set(results["alert_id"]) - set(alerts["alert_id"])
    if unknown:
        raise ValueError(f"{len(unknown)} result alert_ids not in alerts.csv, e.g. {sorted(unknown)[:3]}")
    df = alerts.merge(results, on="alert_id", how="left", suffixes=("", "_agent"))
    df["sent"] = df["classification"].notna()
    df["classification"] = df["classification"].fillna("likely_legitimate")
    df["typology"] = df["typology"].fillna(NONE)
    df["is_suspicious"] = df["ground_truth"].ne(NONE)
    return df


def compute(df: pd.DataFrame) -> Metrics:
    """Compute headline metrics."""
    truth = df["is_suspicious"]
    flagged = df["classification"].eq("likely_suspicious")
    caught = df["classification"].ne("likely_legitimate")
    p, r, f, _ = precision_recall_fscore_support(truth, flagged, average="binary", zero_division=0)
    legit = ~truth
    susp_caught = df[truth & caught]
    return Metrics(
        n_alerts=len(df),
        n_scored=int(df["sent"].sum()),
        precision=float(p),
        recall=float(r),
        f1=float(f),
        caught_recall=float(caught[truth].mean()) if truth.any() else 0.0,
        legit_cleared=float((~caught)[legit].mean()) if legit.any() else 0.0,
        legacy_fp_share=float(legit.mean()),
        agent_fpr=float(flagged[legit].mean()) if legit.any() else 0.0,
        agent_review_rate=float(caught[legit].mean()) if legit.any() else 0.0,
        flagged_fp_share=float(legit[flagged].mean()) if flagged.any() else 0.0,
        typology_accuracy=(
            float(susp_caught["typology"].eq(susp_caught["ground_truth"]).mean()) if len(susp_caught) else 0.0
        ),
    )


def confusion(df: pd.DataFrame) -> pd.DataFrame:
    """Ground-truth typology (rows) × agent typology (columns)."""
    return pd.crosstab(
        pd.Categorical(df["ground_truth"], categories=TYPOLOGIES),
        pd.Categorical(df["typology"], categories=TYPOLOGIES),
        rownames=["ground_truth"],
        colnames=["agent"],
        dropna=False,
    )


def worst_misses(df: pd.DataFrame, n: int = WORST_N) -> pd.DataFrame:
    """Most costly errors first: missed laundering, then false escalations, then wrong typology."""
    conf = df["confidence"] if "confidence" in df else pd.Series(0.0, index=df.index)
    missed = df["is_suspicious"] & df["classification"].eq("likely_legitimate")
    false_esc = ~df["is_suspicious"] & df["classification"].eq("likely_suspicious")
    wrong_typ = (
        df["is_suspicious"] & df["classification"].ne("likely_legitimate") & df["typology"].ne(df["ground_truth"])
    )

    out = df.assign(
        severity=missed * 3 + false_esc * 2 + wrong_typ * 1,
        conf=conf.fillna(0.0),
    )
    out = out[out["severity"] > 0].sort_values(["severity", "conf", "alert_id"], ascending=[False, False, True]).head(n)

    def reason(row: pd.Series) -> str:
        if not row["sent"]:
            return f"Missed {row['ground_truth']}: alert never reached the agent (Low tier) — check feature thresholds"
        if row["severity"] == 3:
            return f"Missed {row['ground_truth']}: agent said likely_legitimate (typology {row['typology']})"
        if row["severity"] == 2:
            return f"False escalation: legitimate alert marked likely_suspicious as {row['typology']}"
        return f"Right call, wrong typology: {row['ground_truth']} labelled {row['typology']}"

    out["reason"] = out.apply(reason, axis=1)
    cols = [
        "alert_id",
        "customer_id",
        "rule",
        "legacy_score",
        "ground_truth",
        "classification",
        "typology",
        "conf",
        "reason",
    ]
    return out[cols]


def _pct(x: float) -> str:
    return f"{x:.1%}"


def hit(ok: bool) -> str:
    """Target status label."""
    return "✅ met" if ok else "❌ not met"


def lookalikes(df: pd.DataFrame) -> pd.DataFrame | None:
    """How the agent handled legitimate look-alikes, per scenario (needs a ``scenario`` column)."""
    if "scenario" not in df.columns:
        return None
    lk = df[df["scenario"].fillna(NONE).ne(NONE)]
    if lk.empty:
        return None
    return lk.groupby("scenario").agg(
        alerts=("alert_id", "size"),
        sent=("sent", "sum"),
        cleared=("classification", lambda s: s.eq("likely_legitimate").mean()),
        flagged=("classification", lambda s: s.eq("likely_suspicious").mean()),
    )


def render(
    m: Metrics, conf: pd.DataFrame, misses: pd.DataFrame, results_path: Path, lk: pd.DataFrame | None = None
) -> str:
    """Render the Markdown report."""
    lines = [
        "# Agent evaluation report",
        "",
        f"Results: `{results_path}` · {m.n_alerts:,} alerts · {m.n_scored:,} scored by the agent · "
        f"{m.n_alerts - m.n_scored:,} not sent (Low tier, counted as likely_legitimate).",
        "",
        "> **All metrics are on synthetic data** with planted patterns. They are not real-world accuracy.",
        "",
        "## Targets",
        "",
        "| Target | Value | Status |",
        "|---|---:|---|",
        f"| Recall on planted cases (not marked likely_legitimate) ≥ {_pct(RECALL_TARGET)} | {_pct(m.caught_recall)} | {hit(m.caught_recall >= RECALL_TARGET)} |",
        f"| Legitimate alerts marked likely_legitimate ≥ {_pct(LEGIT_CLEARED_TARGET)} | {_pct(m.legit_cleared)} | {hit(m.legit_cleared >= LEGIT_CLEARED_TARGET)} |",
        "",
        "## `likely_suspicious` vs ground truth",
        "",
        "| Precision | Recall | F1 |",
        "|---:|---:|---:|",
        f"| {_pct(m.precision)} | {_pct(m.recall)} | {m.f1:.3f} |",
        "",
        "## False positives: legacy rules vs agent",
        "",
        "| | Legacy rules | Agent |",
        "|---|---:|---:|",
        f"| Legitimate alerts sent to an analyst | 100.0% | {_pct(m.agent_review_rate)} |",
        f"| Legitimate alerts flagged suspicious (FPR) | 100.0% | {_pct(m.agent_fpr)} |",
        f"| Share of flagged alerts that are legitimate | {_pct(m.legacy_fp_share)} | {_pct(m.flagged_fp_share)} |",
        "",
        f"Typology accuracy on caught suspicious alerts: **{_pct(m.typology_accuracy)}**.",
        "",
        "## Typology confusion matrix",
        "",
        "Rows = ground truth, columns = agent typology.",
        "",
        "| ground_truth \\ agent | " + " | ".join(conf.columns.astype(str)) + " |",
        "|---|" + "---:|" * len(conf.columns),
    ]
    lines += [f"| {idx} | " + " | ".join(str(v) for v in row) + " |" for idx, row in conf.iterrows()]
    if lk is not None:
        lines += [
            "",
            "## Legitimate look-alikes",
            "",
            "Legitimate behaviour that resembles a typology. All should end up likely_legitimate.",
            "",
            "| Scenario | Alerts | Sent to agent | Cleared | Flagged suspicious |",
            "|---|---:|---:|---:|---:|",
        ]
        lines += [
            f"| {idx} | {int(r.alerts)} | {int(r.sent)} | {_pct(r.cleared)} | {_pct(r.flagged)} |"
            for idx, r in zip(lk.index, lk.itertuples())
        ]
    lines += ["", f"## {WORST_N} worst misses", ""]
    if misses.empty:
        lines.append("None.")
    else:
        lines += [
            "| Alert | Rule | Legacy | Truth | Agent | Typology | Conf | Reason |",
            "|---|---|---:|---|---|---|---:|---|",
        ]
        lines += [
            f"| {r.alert_id} | {r.rule} | {r.legacy_score} | {r.ground_truth} | {r.classification} | "
            f"{r.typology} | {r.conf:.2f} | {r.reason} |"
            for r in misses.itertuples()
        ]
    return "\n".join(lines) + "\n"


def evaluate(results_path: Path, alerts_path: Path) -> tuple[Metrics, str]:
    """Run the full evaluation and return metrics plus the Markdown report."""
    alerts = pd.read_csv(alerts_path)
    df = join(alerts, load_results(results_path))
    m = compute(df)
    return m, render(m, confusion(df), worst_misses(df), results_path, lookalikes(df))


def main(argv: list[str] | None = None) -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--results", default=str(EXPORTS_DIR / "agent_results.csv"))
    parser.add_argument("--alerts", default=str(RAW_DIR / "alerts.csv"))
    parser.add_argument("--out", default=str(EXPORTS_DIR / "eval_report.md"))
    args = parser.parse_args(argv)

    results = Path(args.results)
    if not results.exists():
        parser.error(f"{results} not found — export the agent results from Foundry first (see README)")
    _, report = evaluate(results, Path(args.alerts))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")
    print(report)
    print(f"Report written to {out}")


if __name__ == "__main__":
    main()
