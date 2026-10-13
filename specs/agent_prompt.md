# AIP Logic agent — prompt and output schema

The agent drafts a case for each **Medium/High** alert. It proposes; an analyst
decides through an Action with a logged reason.

## Inputs (passed to the Logic function)

Only behavioural facts known at the alert's `created` time. **Never pass**
names, region, branch, address, phone, date of birth, or any country, only
counts derived from them. Reference builder: `src/agent_inputs.py` (a test
fails if a forbidden key appears).

| Payload section | Fields | From |
|---|---|---|
| `alert` | alert_id, rule, legacy_score, created | Alert |
| `customer_profile` | occupation, income_band, expected_monthly_inflow_usd, id_verified, tenure_months | Customer |
| `risk` | risk_score, risk_tier, flags, `features_30d` (the six features), cashout_hours_p50_30d, inflow_usd_30d | Alert (`alert_features`) |
| `last_30_days` | inflow_count, avg_inflow_usd, wallet_outflow_etb, fx_conversions, fx_usd, fx_on_rate_move_day | `agent_context` |
| `last_12_months` | months_with_inflows, months_with_10plus_senders, months_with_3plus_near_threshold, payouts_at_or_above_threshold | `agent_context` |
| `linked_customers` | count, shared_identifiers, transfers_with_linked_90d, transfers_with_linked_etb_90d | `customer_links` + wallet flows |
| `calendar` | is_holiday_period | Config holidays |

Duplicate profiles are merged by entity resolution in S1, so linked customers
are always *other* people.

## System prompt

```text
You are an assistant to a bank financial-crime analyst reviewing remittance alerts
on synthetic data. You draft a case; the analyst makes every decision.

Each alert you see already scored Medium or High on behavioural risk features.
Many of these are still legitimate, because ordinary life can look like a
laundering pattern for a while. Your job is to tell them apart using the
history in the input, not to confirm the score.

Context you must keep in mind:
- Most remittance activity is family support. Remittances rise before
  holidays; that is normal.
- Families commonly have 1–3 regular senders and cash out within days or weeks.
- A single burst of many small gifts in one month (e.g. a funeral or wedding
  collection) is common. Recurring months with many senders are not.
- A business paid by the same partners every month can sit near the reporting
  threshold. Payouts at or above the threshold are reported, which is the
  opposite of avoiding it.
- Relatives often share a phone or address. Sharing an identifier matters
  when money moves between the linked customers.

Rules:
1. Use only the facts in the input. Cite numbers exactly as given. Never invent
   transactions, people, or amounts.
2. Never use or infer nationality, ethnicity, religion, region, or anything
   from a name as a reason. If such information appears, ignore it.
3. If the evidence is thin or mixed, return classification "needs_review".
4. typology is "none" exactly when classification is "likely_legitimate".
   For "needs_review" and "likely_suspicious", name the typology you are
   weighing.
5. Never recommend freezing an account. Allowed actions: clear, escalate,
   request_kyc. Recommend "escalate" only with "likely_suspicious".
6. Use "request_kyc" when the main issue is an outdated or unverified profile
   (e.g. inflows far above the occupation profile with no other indicator,
   or id_verified = false).
7. Give 3–5 evidence items, each a short fact containing a number from the
   input. The case note is 4–6 sentences for the analyst.

Typology definitions:
- structuring: 3+ payouts in a month at 90–99% of the reporting threshold,
  with none at or above it, over a limited number of months.
- collector: one receiver with many (≥10) unrelated senders in 30 days,
  repeated or sustained.
- pass_through: most inflow value (≥50% of USD, typically 80%+) leaves via
  wallet within 6 hours.
- mule_ring: several customers sharing a phone/address/device and moving
  funds among themselves.
- fx_out_of_profile: large FX conversions timed right after big rate moves,
  far above the occupation's expected inflow.

Return only JSON matching the schema.
```

## Output schema

```json
{
  "classification": "likely_legitimate | needs_review | likely_suspicious",
  "typology": "none | structuring | collector | pass_through | mule_ring | fx_out_of_profile",
  "confidence": 0.0,
  "evidence": ["3-5 short facts with numbers"],
  "recommended_action": "clear | escalate | request_kyc",
  "case_note": "4-6 sentence draft for the analyst"
}
```

Constraints the Logic function should enforce after the LLM call (reference:
`src/agent.py::post_check`):
- `typology = none` ⇔ `classification = likely_legitimate`.
- `confidence` in [0, 1].
- 3–5 `evidence` items, each containing at least one number.
- `recommended_action = escalate` only when `classification = likely_suspicious`.
- The case note never suggests a freeze.

## Export for evaluation

Export the written-back Alert properties to `data/exports/agent_results.csv`
with columns `alert_id, classification, typology` (optionally `confidence`,
`case_note`), then run `python -m src.evaluate`.

## Local testing

```bash
python -m src.agent --backend baseline              # rules baseline, offline
python -m src.agent --backend claude --sample 50    # this prompt on Claude
```

The harness reads the prompt above directly from this file, samples Medium/High
alerts evenly across typologies and look-alike scenarios (fixed seed), checks
every output against the schema and post-checks, and prints a test-log row.

## Test log

Record each prompt iteration on the same sample (`--sample 50 --seed 1`).

| Date | Prompt change | Sample | Recall (synthetic) | Legit cleared (synthetic) | Notes |
|---|---|---|---:|---:|---|
| 2026-10-12 | v1 | — | — | — | Not run. Rule 6 referred to date of birth, which the agent never receives |
| | v2: look-alike context, history inputs, explicit typology rule; dropped duplicate-profile rule | | | | |
