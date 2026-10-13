"""Tests for the S3 agent harness: inputs, fairness, prompt, schema, post-checks, backends.

The Claude backend is exercised with a stub client — no network calls.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pandas as pd
import pytest

from src import agent, evaluate
from src.agent_inputs import FORBIDDEN_KEYS, build_inputs, payload_keys, to_payload
from src.common import NONE
from src.customer_activity import build_activity, fx_daily_rate
from src.entity_resolution import build_xref
from src.features import alert_features, customer_links, score


@pytest.fixture(scope="module")
def inputs(cfg: dict, cleaned: dict) -> pd.DataFrame:
    xref = build_xref(cleaned["customers"])
    daily = fx_daily_rate(cleaned["remittances"])
    activity = build_activity(cleaned["remittances"], cleaned["wallet_txns"], cleaned["fx_conversions"], xref, daily)
    links = customer_links(cleaned["customers"], xref)
    scored = score(alert_features(cfg, cleaned["alerts"], cleaned["customers"], xref, activity, daily, links), cfg)
    return build_inputs(cfg, scored, cleaned["alerts"], cleaned["customers"], xref, activity, daily, links)


@pytest.fixture(scope="module")
def labelled(inputs: pd.DataFrame, tables: dict) -> pd.DataFrame:
    a = tables["alerts"].set_index("alert_id")
    return inputs.assign(
        ground_truth=inputs["alert_id"].map(a["ground_truth"]), scenario=inputs["alert_id"].map(a["scenario"])
    )


def _valid() -> dict:
    return {
        "classification": "likely_suspicious",
        "typology": "collector",
        "confidence": 0.8,
        "evidence": ["71 senders in 30 days", "profile mismatch 120x", "1 month with 10+ senders"],
        "recommended_action": "escalate",
        "case_note": "Many unrelated senders. No freeze recommended.",
    }


# ---------------------------------------------------------------------------
# Inputs and fairness
# ---------------------------------------------------------------------------


def test_one_input_row_per_alert(inputs: pd.DataFrame, cleaned: dict) -> None:
    assert len(inputs) == len(cleaned["alerts"])
    assert inputs["alert_id"].is_unique


def test_payloads_never_contain_protected_or_identifying_fields(inputs: pd.DataFrame) -> None:
    for _, row in inputs.head(200).iterrows():
        payload = to_payload(row)
        assert not payload_keys(payload) & FORBIDDEN_KEYS
        json.dumps(payload)  # serialisable


def test_payload_never_contains_ground_truth(inputs: pd.DataFrame) -> None:
    text = json.dumps(to_payload(inputs.iloc[0]))
    assert "ground_truth" not in text and "scenario" not in text


def test_history_separates_lookalikes(labelled: pd.DataFrame) -> None:
    """The facts the prompt relies on actually differ between look-alikes and typologies."""
    trader = labelled[labelled["scenario"].eq("steady_trader")]
    struct = labelled[labelled["ground_truth"].eq("structuring")]
    assert (trader["payouts_at_or_above_threshold_12m"] > 0).mean() > 0.9
    assert (struct["payouts_at_or_above_threshold_12m"] == 0).all()

    family = labelled[labelled["scenario"].eq("family_shared_phone")]
    ring = labelled[labelled["ground_truth"].eq("mule_ring")]
    assert (family["transfers_with_linked_90d"] == 0).all()
    assert (ring["transfers_with_linked_90d"] > 0).mean() > 0.9


# ---------------------------------------------------------------------------
# Prompt, schema, post-checks
# ---------------------------------------------------------------------------


def test_prompt_loads_from_spec() -> None:
    text = agent.load_system_prompt()
    assert "Return only JSON" in text and "Never recommend freezing" in text


def test_schema_enums_match_evaluator() -> None:
    props = agent.OUTPUT_SCHEMA["properties"]
    assert props["classification"]["enum"] == list(evaluate.CLASSIFICATIONS)
    assert props["typology"]["enum"] == list(evaluate.TYPOLOGIES)


def test_post_check_accepts_valid_output() -> None:
    assert agent.post_check(_valid()) == []


@pytest.mark.parametrize(
    "change, expected",
    [
        ({"typology": NONE}, "typology must be 'none'"),
        ({"confidence": 1.5}, "confidence outside"),
        ({"evidence": ["only one fact 1"]}, "evidence items"),
        ({"evidence": ["no number", "still none", "nope"]}, "without a number"),
        ({"classification": "needs_review"}, "escalate recommended"),
        ({"case_note": "Freeze the account immediately."}, "freeze"),
    ],
)
def test_post_check_catches_violations(change: dict, expected: str) -> None:
    problems = agent.post_check({**_valid(), **change})
    assert any(expected in p for p in problems), problems


# ---------------------------------------------------------------------------
# Backends and runner
# ---------------------------------------------------------------------------


def test_baseline_outputs_pass_post_checks(inputs: pd.DataFrame, labelled: pd.DataFrame) -> None:
    sample = agent.choose_sample(
        inputs, agent.label_of(labelled["ground_truth"], labelled["scenario"]), 30, seed=1, always=[]
    )
    results = agent.run(agent.BaselineAgent(), sample)
    assert results["error"].eq("").all()
    assert results["post_check"].eq("").all()


def test_sample_is_stratified_and_reproducible(inputs: pd.DataFrame, labelled: pd.DataFrame) -> None:
    labels = agent.label_of(labelled["ground_truth"], labelled["scenario"])
    a = agent.choose_sample(inputs, labels, 20, seed=3, always=[])
    b = agent.choose_sample(inputs, labels, 20, seed=3, always=[])
    assert a["alert_id"].tolist() == b["alert_id"].tolist()
    assert a["risk_tier"].isin(["Medium", "High"]).all()
    assert a["label"].nunique() > 3


class _StubMessages:
    def __init__(self, text: str, stop_reason: str = "end_turn") -> None:
        self.text, self.stop_reason, self.calls = text, stop_reason, []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=self.text)],
            stop_reason=self.stop_reason,
            stop_details=SimpleNamespace(category="cyber") if self.stop_reason == "refusal" else None,
            usage=SimpleNamespace(input_tokens=1000, output_tokens=200),
        )


def _claude_with(stub: _StubMessages) -> agent.ClaudeAgent:
    a = agent.ClaudeAgent.__new__(agent.ClaudeAgent)
    a.model, a.effort, a.system, a.name = "claude-opus-5-5", "medium", "sys", "claude"
    a.usage = {"input_tokens": 0, "output_tokens": 0}
    a._client = SimpleNamespace(beta=SimpleNamespace(messages=stub))
    return a


def test_claude_agent_request_shape_and_parse(inputs: pd.DataFrame) -> None:
    stub = _StubMessages(json.dumps(_valid()))
    out = _claude_with(stub).review(to_payload(inputs.iloc[0]))
    assert out["typology"] == "collector"
    call = stub.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["output_config"]["format"]["schema"] == agent.OUTPUT_SCHEMA
    assert call["output_config"]["effort"] == "medium"
    assert call["fallbacks"] == "default"


def test_claude_refusal_is_recorded_not_fatal(inputs: pd.DataFrame, labelled: pd.DataFrame) -> None:
    sample = agent.choose_sample(
        inputs, agent.label_of(labelled["ground_truth"], labelled["scenario"]), 3, seed=1, always=[]
    )
    results = agent.run(_claude_with(_StubMessages("", stop_reason="refusal")), sample)
    assert results["error"].str.contains("refused").all()
