"""Local test harness for the case-drafting agent (S3 prompt iteration).

Usage::

    python -m src.agent --backend baseline              # offline rules baseline, no API calls
    python -m src.agent --backend claude --sample 50    # real prompt on Claude (needs credentials)
    python -m src.agent --backend claude --all          # every Medium/High alert

Reads the S1/S2 outputs in data/derived/ (run ``make all`` first). The system
prompt is read from ``specs/agent_prompt.md`` so the spec is the only copy.

Writes ``data/exports/agent_results_<backend>.csv`` (same columns as the Foundry
export, so ``src.evaluate`` can score it) and ``data/exports/agent_test_report.md``.
With ``--all`` both names get an ``_all`` suffix so the test sample is kept.
Ground truth is read from data/raw/alerts.csv for scoring only; it never reaches
the agent.
"""

from __future__ import annotations

import argparse
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import pandas as pd

from src import evaluate
from src.agent_inputs import build_inputs, to_payload
from src.common import DEFAULT_CONFIG, DERIVED_DIR, EXPORTS_DIR, LAUNDERING_TYPOLOGIES, NONE, RAW_DIR, ROOT, load_config
from src.features import customer_links, load_derived

PROMPT_PATH = ROOT / "specs" / "agent_prompt.md"
CLASSIFICATIONS = ["likely_legitimate", "needs_review", "likely_suspicious"]
TYPOLOGIES = [NONE, *LAUNDERING_TYPOLOGIES]
ACTIONS = ["clear", "escalate", "request_kyc"]

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "classification": {"type": "string", "enum": CLASSIFICATIONS},
        "typology": {"type": "string", "enum": TYPOLOGIES},
        "confidence": {"type": "number"},
        "evidence": {"type": "array", "items": {"type": "string"}},
        "recommended_action": {"type": "string", "enum": ACTIONS},
        "case_note": {"type": "string"},
    },
    "required": ["classification", "typology", "confidence", "evidence", "recommended_action", "case_note"],
    "additionalProperties": False,
}


def load_system_prompt(path: Path = PROMPT_PATH) -> str:
    """Extract the fenced block under ``## System prompt`` in the spec."""
    text = path.read_text(encoding="utf-8")
    match = re.search(r"## System prompt\s+```text\n(.*?)\n```", text, flags=re.S)
    if not match:
        raise ValueError(f"No ```text block under '## System prompt' in {path}")
    return match.group(1).strip()


def post_check(out: dict[str, Any]) -> list[str]:
    """Constraints from specs/agent_prompt.md that the schema alone can't enforce."""
    problems = []
    legit = out["classification"] == "likely_legitimate"
    if (out["typology"] == NONE) != legit:
        problems.append("typology must be 'none' exactly when classification is likely_legitimate")
    if not 0.0 <= float(out["confidence"]) <= 1.0:
        problems.append("confidence outside [0, 1]")
    if not 3 <= len(out["evidence"]) <= 5:
        problems.append(f"{len(out['evidence'])} evidence items (need 3–5)")
    if any(not re.search(r"\d", e) for e in out["evidence"]):
        problems.append("evidence item without a number")
    if out["recommended_action"] == "escalate" and out["classification"] != "likely_suspicious":
        problems.append("escalate recommended without likely_suspicious")
    if re.search(r"\bfreez", out["case_note"], flags=re.I) and not re.search(
        r"\b(no|not|never)\b[^.]*freez", out["case_note"], flags=re.I
    ):
        problems.append("case note suggests a freeze")
    return problems


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------


class Agent(Protocol):
    """Anything that turns one payload into one output dict."""

    name: str

    def review(self, payload: dict[str, Any]) -> dict[str, Any]: ...


FLAG_TYPOLOGY = {
    "near_threshold_ct": "structuring",
    "sender_fanin": "collector",
    "cashout_fast_share": "pass_through",
    "shared_id_links": "mule_ring",
    "fx_rate_timing": "fx_out_of_profile",
}


class BaselineAgent:
    """Hand-written rules: the bar the LLM prompt has to beat. No API calls."""

    name = "baseline"

    def review(self, p: dict[str, Any]) -> dict[str, Any]:
        """Map the strongest flag to a typology, then apply look-alike exceptions."""
        flags = [f for f in p["risk"]["flags"] if f in FLAG_TYPOLOGY]
        hist, link, last = p["last_12_months"], p["linked_customers"], p["last_30_days"]
        if not flags:
            return self._out("likely_legitimate", NONE, 0.7, p, "no behavioural indicator flagged")
        typ = FLAG_TYPOLOGY[flags[0]]
        if (
            typ == "structuring"
            and hist["payouts_at_or_above_threshold_12m"] > 0
            and hist["months_with_inflows_12m"] >= 10
        ):
            return self._out(
                "likely_legitimate", NONE, 0.6, p, "steady payouts all year, many reported above threshold"
            )
        if (
            typ == "collector"
            and hist["months_with_10plus_senders_12m"] <= 1
            and (last["avg_inflow_usd_30d"] or 0) < 200
        ):
            return self._out("needs_review", typ, 0.5, p, "one-off burst of small gifts")
        if typ == "mule_ring" and link["transfers_with_linked_90d"] == 0:
            return self._out(
                "likely_legitimate", NONE, 0.6, p, "shared identifier but no money moves between linked customers"
            )
        return self._out("likely_suspicious", typ, 0.8, p, f"{flags[0]} flagged")

    @staticmethod
    def _out(cls: str, typ: str, conf: float, p: dict[str, Any], why: str) -> dict[str, Any]:
        f = p["risk"]["features_30d"]
        evidence = [
            f"risk score {p['risk']['risk_score']} ({p['risk']['risk_tier']})",
            f"{f['sender_fanin']} distinct senders in 30 days",
            f"profile mismatch {f['profile_mismatch']}x expected inflow",
        ]
        action = {"likely_legitimate": "clear", "needs_review": "request_kyc", "likely_suspicious": "escalate"}[cls]
        note = f"Baseline rules: {why}. Analyst to confirm. Evidence: " + "; ".join(evidence) + "."
        return {
            "classification": cls,
            "typology": typ,
            "confidence": conf,
            "evidence": evidence,
            "recommended_action": action,
            "case_note": note,
        }


@dataclass
class ClaudeAgent:
    """The real prompt from specs/agent_prompt.md, on the Claude API with schema-constrained output."""

    model: str
    effort: str
    system: str = field(default_factory=load_system_prompt)
    name: str = "claude"
    usage: dict[str, int] = field(default_factory=lambda: {"input_tokens": 0, "output_tokens": 0})

    def __post_init__(self) -> None:
        import anthropic  # imported lazily so the baseline runs without the SDK

        self._anthropic = anthropic
        self._client = anthropic.Anthropic()

    def review(self, payload: dict[str, Any]) -> dict[str, Any]:
        """One API call per alert; raises on refusal or truncation."""
        response = self._client.beta.messages.create(
            model=self.model,
            max_tokens=8000,
            system=self.system,
            messages=[
                {
                    "role": "user",
                    "content": "Draft the case for this alert.\n\n```json\n" + json.dumps(payload, indent=2) + "\n```",
                }
            ],
            output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
            # Refusal fallback: if a safety classifier declines, the API retries on a fallback model.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        self.usage["input_tokens"] += response.usage.input_tokens
        self.usage["output_tokens"] += response.usage.output_tokens
        if response.stop_reason == "refusal":
            category = response.stop_details.category if response.stop_details else None
            raise RuntimeError(f"refused (category {category})")
        if response.stop_reason == "max_tokens":
            raise RuntimeError("hit max_tokens")
        text = next(b.text for b in response.content if b.type == "text")
        return json.loads(text)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def label_of(truth: pd.Series, scenario: pd.Series) -> pd.Series:
    """Evaluation stratum: typology, look-alike scenario, or ordinary legitimate."""
    return truth.where(truth.ne(NONE), scenario.where(scenario.ne(NONE), "ordinary_legit"))


def choose_sample(inputs: pd.DataFrame, labels: pd.Series, n: int | None, seed: int, always: list[str]) -> pd.DataFrame:
    """Medium/High alerts only; stratified evenly across labels, plus any ``always`` alert ids."""
    pool = inputs[inputs["risk_tier"].isin(["Medium", "High"])].assign(label=labels)
    if n is None:
        return pool
    per = max(1, n // pool["label"].nunique())
    picked = pool.sample(frac=1.0, random_state=seed).groupby("label").head(per)
    extra = pool[pool["alert_id"].isin(always) & ~pool["alert_id"].isin(picked["alert_id"])]
    return pd.concat([picked, extra]).sort_values("alert_id")


def run(agent: Agent, sample: pd.DataFrame, delay: float = 0.0) -> pd.DataFrame:
    """Review each sampled alert; record schema/post-check problems and errors instead of stopping."""
    rows = []
    for i, (_, row) in enumerate(sample.iterrows(), 1):
        payload = to_payload(row)
        try:
            out = agent.review(payload)
            problems = post_check(out)
            error = ""
        except Exception as exc:  # noqa: BLE001 — one bad alert must not stop a paid run
            out = {
                "classification": "needs_review",
                "typology": NONE,
                "confidence": 0.0,
                "evidence": [],
                "recommended_action": "request_kyc",
                "case_note": "",
            }
            problems, error = [], f"{type(exc).__name__}: {exc}"
        rows.append(
            {
                "alert_id": row["alert_id"],
                "classification": out["classification"],
                "typology": out["typology"],
                "confidence": out["confidence"],
                "recommended_action": out["recommended_action"],
                "evidence": " | ".join(out["evidence"]),
                "case_note": out["case_note"],
                "post_check": "; ".join(problems),
                "error": error,
            }
        )
        print(
            f"  [{i}/{len(sample)}] {row['alert_id']} → {out['classification']} / {out['typology']}"
            + (f"  ⚠ {error}" if error else "")
        )
        if delay:
            time.sleep(delay)
    return pd.DataFrame(rows)


def score_sample(
    results: pd.DataFrame, alerts: pd.DataFrame, sample: pd.DataFrame
) -> tuple[evaluate.Metrics, pd.DataFrame]:
    """Metrics on the sampled alerts only, plus accuracy per stratum."""
    subset = alerts[alerts["alert_id"].isin(sample["alert_id"])]
    df = evaluate.join(subset, evaluate.load_results_frame(results))
    m = evaluate.compute(df)
    df = df.merge(sample[["alert_id", "label"]], on="alert_id")
    correct = (df["is_suspicious"] & df["classification"].ne("likely_legitimate")) | (
        ~df["is_suspicious"] & df["classification"].eq("likely_legitimate")
    )
    by_label = df.assign(correct=correct).groupby("label").agg(n=("alert_id", "size"), correct=("correct", "mean"))
    return m, by_label


def report_md(
    agent: Agent,
    args: argparse.Namespace,
    m: evaluate.Metrics,
    by_label: pd.DataFrame,
    results: pd.DataFrame,
    cost: str,
) -> str:
    """Markdown report plus a ready-to-paste test-log row for specs/agent_prompt.md."""
    lines = [
        f"# Agent test run — {agent.name}",
        "",
        f"{len(results)} Medium/High alerts · sample {'all' if args.all else args.sample} · seed {args.seed}"
        + (f" · model `{args.model}` effort `{args.effort}`" if agent.name == "claude" else "")
        + f" · {cost} · **synthetic data**",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| Recall (not marked likely_legitimate) | {m.caught_recall:.1%} |",
        f"| Legitimate correctly marked likely_legitimate | {m.legit_cleared:.1%} |",
        f"| Precision / recall / F1 (likely_suspicious) | {m.precision:.1%} / {m.recall:.1%} / {m.f1:.3f} |",
        f"| Typology accuracy on caught | {m.typology_accuracy:.1%} |",
        f"| Outputs failing post-checks | {int(results['post_check'].ne('').sum())} |",
        f"| Errors (refusal, truncation, API) | {int(results['error'].ne('').sum())} |",
        "",
        "Correct = suspicious not marked likely_legitimate, or legitimate marked likely_legitimate.",
        "",
        "| Stratum | n | Correct |",
        "|---|---:|---:|",
    ]
    lines += [f"| {lab} | {int(r.n)} | {r.correct:.0%} |" for lab, r in zip(by_label.index, by_label.itertuples())]
    lines += [
        "",
        "Test-log row for `specs/agent_prompt.md`:",
        "",
        f"| {pd.Timestamp.now():%Y-%m-%d} | <prompt change> | {len(results)} ({agent.name}) | "
        f"{m.caught_recall:.1%} | {m.legit_cleared:.1%} | <notes> |",
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--backend", choices=["baseline", "claude"], default="baseline")
    parser.add_argument("--sample", type=int, default=50, help="stratified sample size (ignored with --all)")
    parser.add_argument("--all", action="store_true", help="every Medium/High alert")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--model", default=None, help="overrides config agent.model")
    parser.add_argument("--effort", default=None, help="overrides config agent.effort")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    args.model = args.model or cfg["agent"]["model"]
    args.effort = args.effort or cfg["agent"]["effort"]

    t = load_derived(DERIVED_DIR)
    scored = pd.read_csv(DERIVED_DIR / "alert_features.csv", parse_dates=["created"])
    links = customer_links(t["customers_clean"], t["customer_xref"])
    inputs = build_inputs(
        cfg,
        scored,
        t["alerts_clean"],
        t["customers_clean"],
        t["customer_xref"],
        t["customer_activity"],
        t["fx_daily_rate"],
        links,
    )

    alerts = pd.read_csv(RAW_DIR / "alerts.csv")  # labels for scoring only
    truth = inputs["alert_id"].map(alerts.set_index("alert_id")["ground_truth"])
    scenario = inputs["alert_id"].map(alerts.set_index("alert_id")["scenario"])
    demo = [a.strip() for a in cfg["agent"].get("always_include", [])]
    sample = choose_sample(inputs, label_of(truth, scenario), None if args.all else args.sample, args.seed, demo)

    agent: Agent = BaselineAgent() if args.backend == "baseline" else ClaudeAgent(model=args.model, effort=args.effort)
    print(f"Reviewing {len(sample)} alerts with {agent.name}…")
    results = run(agent, sample)

    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    suffix = "_all" if args.all else ""  # a full run never overwrites the test sample
    out = EXPORTS_DIR / f"agent_results_{agent.name}{suffix}.csv"
    results.to_csv(out, index=False)
    m, by_label = score_sample(results, alerts, sample)

    cost = "no API cost"
    if isinstance(agent, ClaudeAgent):
        price = cfg["agent"]["price_per_mtok"]
        usd = agent.usage["input_tokens"] / 1e6 * price["input"] + agent.usage["output_tokens"] / 1e6 * price["output"]
        cost = f"{agent.usage['input_tokens']:,} in / {agent.usage['output_tokens']:,} out tokens ≈ ${usd:.2f}"
    report = report_md(agent, args, m, by_label, results, cost)
    (EXPORTS_DIR / f"agent_test_report{suffix}.md").write_text(report, encoding="utf-8")
    print("\n" + report)
    print(f"Results: {out}")


if __name__ == "__main__":
    main()
