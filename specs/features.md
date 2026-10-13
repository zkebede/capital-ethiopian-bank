# Risk features

Computed **per alert**, over the 30 days ending at the alert's `created` time,
aggregated by `resolved_entity_id` (duplicate profiles roll up to one person).
Reference implementation: `src/features.py`. Thresholds and points live in
`config.yaml → features`; this file records what they mean and why they changed.

> **Fairness rule:** no feature may use nationality, ethnicity, religion,
> region, branch, sender/beneficiary country, or any part of a name. Features
> describe *behaviour* only.

## Features (final, tuned)

| Feature | Definition | Source | Flag | Points |
|---|---|---|---|---:|
| `sender_fanin` | Distinct `sender_name` values on remittances to this entity | customer_activity | ≥ 10 | 25 |
| `cashout_fast_share` | Share of remittance **USD** followed by the entity's next wallet outflow within 6h (outflow must precede the alert) | customer_activity | ≥ 0.5 | 30 |
| `near_threshold_ct` | Remittances with `amount_usd` in [0.90, 1.00) × `reporting_threshold_usd` | customer_activity | ≥ 3 | 20 |
| `shared_id_links` | Other *entities* sharing a phone or address (same-entity duplicates excluded) | customer_links | ≥ 2 | 30 |
| `fx_rate_timing` | Share of FX conversions made on a day the reference rate moved > 2% vs the previous day (null if no conversions) | customer_activity + fx_daily_rate | ≥ 0.5 | 20 |
| `profile_mismatch` | (remittance USD + FX `usd_in`) ÷ expected monthly inflow for the customer's income band | customer_activity + customers | ≥ 5× | 10 |

Unscored context columns kept for the agent: `cashout_hours_p50` (median hours
inflow → next outflow) and `inflow_usd_30d`.

Expected monthly inflow by band: low 300 · medium 800 · high 2,500 USD
(`config.yaml → expected_monthly_inflow_usd`).

`risk_score` = sum of points of flagged features. Null features never flag.

| Tier | Score | Routing |
|---|---|---|
| Low | < 30 | Not sent to the agent |
| Medium | 30–59 | Agent drafts a case |
| High | ≥ 60 | Agent drafts a case; top of the queue |

## Results (synthetic data, seed 42)

| Ground truth | Low | Medium | High | Sent to agent |
|---|---:|---:|---:|---:|
| none (ordinary + look-alikes) | 5,182 | 218 | 0 | 4% |
| structuring | 0 | 149 | 0 | 100% |
| collector | 0 | 48 | 0 | 100% |
| pass_through | 2 | 166 | 0 | 99% |
| mule_ring | 0 | 146 | 0 | 100% |
| fx_out_of_profile | 0 | 89 | 0 | 100% |

**99.7%** of planted alerts reach the agent (ceiling on recall; target ≥ 90%).
**96.0%** of legitimate alerts stay Low. Most legitimate alerts that reach the
agent are deliberate look-alikes (139 of 218), so the agent has real
borderline calls to make. Regenerate with `python -m src.features`
(writes `data/derived/_s2_expected.md`).

## Legitimate look-alikes

Planted on purpose (`config.yaml → lookalikes`, hidden `scenario` label). These
**should reach the agent**, and the agent should clear them using history:

| Scenario | Trips | Distinguishing facts |
|---|---|---|
| community_collection | `sender_fanin` (+ `profile_mismatch`) | One month only; small gifts (avg < $200) |
| steady_trader | `near_threshold_ct` + `profile_mismatch` | Every month of the year; some payouts at/above the threshold (reported) |
| family_shared_phone | `shared_id_links` | No transfers between the linked customers |

Ordinary behaviour that should stay Low:

- Holiday spikes (Genna, Timket, Fasika, Eid, Enkutatash, Meskel): more and
  larger family payments in the 14 days before. Volume alone is not risk.
- Families with 3 regular senders (siblings abroad): `sender_fanin` = 3.
- Normal cash-out 3–30 days after payout.
- Duplicate profiles share a phone, but resolve to one entity, so
  `shared_id_links` ignores them.
- Legitimate large US transfers above the threshold, reported with `ctr_flag = True`.

## Tuning log

| Date | Change | Why | Planted → agent | Legit kept Low |
|---|---|---|---:|---:|
| 2026-10-12 | Original spec weights (`cashout_hours_p50` ≤ 6h = 20, `shared_id_links` = 15, `fx_rate_timing` = 10) | Baseline | 38.8% | 100.0% |
| 2026-10-12 | `shared_id_links` 15 → 30; `fx_rate_timing` 10 → 20 | Mule rings (15) and FX timing (10 + 10) could never reach Medium alone | 90.3% | 99.8% |
| 2026-10-12 | Replace `cashout_hours_p50` (≤ 6h, 20) with `cashout_fast_share` (≥ 0.5, 30) | Ordinary family remittances in the same window pushed the median above 6h, hiding a third of pass-through cases; value-weighting is closer to the typology ("80%+ of each inflow out within 6h") | 99.3% | 99.9% |
| 2026-10-12 | Added legitimate look-alikes (no rule change) | Give the agent borderline legitimate cases | 99.7% | 96.0% |
