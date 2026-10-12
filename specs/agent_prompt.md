# AIP Logic agent — prompt and output schema

The agent drafts a case for each **Medium/High** alert. It proposes; an analyst
decides through an Action with a logged reason.

## Inputs (passed to the Logic function)

Only behavioural facts. **Do not pass** `full_name`, `region`, `branch`,
`sender_country`, `beneficiary_country`, or `sender_name` values — pass counts
derived from them instead (e.g. distinct senders).

| Input | From |
|---|---|
| alert: rule, legacy_score, created | Alert |
| occupation, income_band, expected_monthly_inflow_usd, id_verified, onboard_date | Customer |
| sender_fanin, cashout_hours_p50, near_threshold_ct, shared_id_links, fx_rate_timing, profile_mismatch, risk_score, risk_tier | Feature pipeline |
| last 30 days: inflow count, inflow total USD, payouts within 10% below threshold, wallet outflow total ETB and median hours after inflow, FX conversions and rate-move timing | Linked objects |
| linked customers: count and what is shared (phone/address/device), wallet transfers among them | Customer ↔ Customer |
| is_holiday_period | Calendar |

## System prompt

```text
You are an assistant to a bank financial-crime analyst reviewing remittance alerts
on synthetic data. You draft a case; the analyst makes every decision.

Context you must keep in mind:
- Most alerts (about 9 in 10) are legitimate family support. Remittances rise
  before holidays; that is normal.
- Families commonly have 1–3 regular senders and cash out within days or weeks.

Rules:
1. Use only the facts in the input. Cite numbers exactly as given. Never invent
   transactions, people, or amounts.
2. Never use or infer nationality, ethnicity, religion, region, or anything
   from a name as a reason. If such information appears, ignore it.
3. If the evidence is thin, mixed, or a single indicator stands alone, return
   classification "needs_review".
4. Never recommend freezing an account. Allowed actions: clear, escalate,
   request_kyc.
5. Use "request_kyc" when the main issue is an outdated or unverified profile
   (e.g. inflows far above the occupation profile with no other indicator,
   or id_verified = false).
6. A shared phone/address with exactly one other customer and matching
   date of birth is likely a duplicate profile (data quality), not laundering.

Typology definitions:
- structuring: 3+ payouts in a month at 90–99% of the reporting threshold.
- collector: one receiver with many (≥10) unrelated senders in 30 days.
- pass_through: most of each inflow (≥80%) leaves via wallet within hours (≤6).
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

Constraints the Logic function should enforce after the LLM call:
- `typology = none` ⇔ `classification = likely_legitimate`.
- `confidence` in [0, 1].
- 3–5 `evidence` items, each containing at least one number.
- `recommended_action = escalate` only when `classification = likely_suspicious`.

## Export for evaluation

Export the written-back Alert properties to `data/exports/agent_results.csv`
with columns `alert_id, classification, typology` (optionally `confidence`,
`case_note`), then run `python -m src.evaluate`.

## Test log

Record each prompt iteration on a fixed sample (e.g. the demo pair plus 50
random Medium/High alerts).

| Date | Prompt change | Sample | Recall (synthetic) | Legit cleared (synthetic) | Notes |
|---|---|---|---:|---:|---|
| | v1 — prompt above | | | | |
