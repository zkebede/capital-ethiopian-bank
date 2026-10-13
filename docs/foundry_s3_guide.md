# Sprint 3 in Foundry — app and agent

Goal: an analyst opens a queue ranked by behavioural risk, sees an AI-drafted
case for each Medium/High alert, and clears or escalates it with a logged reason.

| Step | Reference | Foundry output |
|---|---|---|
| 1. Agent context | `src/agent_inputs.py` | `features/agent_context` dataset → Alert properties |
| 2. AIP Logic agent | `specs/agent_prompt.md`, `src/agent.py` | Logic function `draftCase(alert)` |
| 3. Write-back | `specs/ontology.md` (Alert `agent_*`) | Action **Save Agent Draft** |
| 4. Workshop | — | Queue, Customer 360, Network pages |
| 5. Human decision | `specs/ontology.md` Actions | Approve/override flow on Case |
| 6. Automate | — | Run the agent on new Medium/High alerts |

Test the prompt locally first (`python -m src.agent`) so you arrive in Foundry
with a prompt that already passes its post-checks.

> Transform and widget names below are descriptive. If one is labelled
> differently in your version, search for the operation.

---

## 1. `agent_context`

Same pattern as `alert_features`: join alerts → `customer_activity` on
`resolved_entity_id`, filter by time relative to `created`, aggregate by `alert_id`.

| Column | Window | Definition |
|---|---|---|
| inflow_count_30d | 30d | count of remittances |
| avg_inflow_usd_30d | 30d | mean remittance `amount_usd` |
| wallet_outflow_etb_30d | 30d | sum wallet `amount_etb` |
| fx_conversions_30d, fx_usd_30d | 30d | count / sum `amount_usd` of FX rows |
| fx_on_rate_move_day_30d | 30d | FX rows on a `rate_move_days` day |
| months_with_inflows_12m | 365d | distinct months with a remittance |
| months_with_10plus_senders_12m | 365d | months where distinct `counterparty` ≥ 10 |
| months_with_3plus_near_threshold_12m | 365d | months with ≥ 3 remittances in [9,000, 10,000) |
| payouts_at_or_above_threshold_12m | 365d | remittances ≥ 10,000 |
| transfers_with_linked_90d, transfers_with_linked_etb_90d | 90d | wallet transfers (either direction) between the entity and entities in `customer_links` with `same_entity = false` |
| linked_on | — | identifiers shared (`phone`, `address`) |
| tenure_months | — | months from `onboard_date` to `created` |
| is_holiday_period | — | `created` within 14 days before a holiday in `config.yaml` |

Join onto `alert_features` and add the columns to the **Alert** object type.

**Never** add name, region, branch, address, phone, dob, or country to the agent's inputs.

## 2. AIP Logic function `draftCase`

1. Create a Logic function with input **Alert** (object).
2. Use an LLM block. **System prompt**: paste the ```text block from
   `specs/agent_prompt.md` exactly.
3. **User prompt**: the alert's properties as JSON, grouped like the payload
   built by `src/agent_inputs.py::to_payload` (alert, customer_profile, risk,
   last_30_days, last_12_months, linked_customers, calendar).
4. **Output**: a struct with `classification`, `typology`, `confidence`,
   `evidence` (string list), `recommended_action`, `case_note`. Use enums where
   Logic supports them (values in `specs/agent_prompt.md → Output schema`).
5. **Post-checks** (after the LLM block, same rules as `src/agent.py::post_check`).
   On failure, set `classification = needs_review` and prepend the problem to
   `case_note`:
   - typology = none ⇔ classification = likely_legitimate
   - confidence in [0, 1]; 3–5 evidence items, each with a number
   - escalate only with likely_suspicious; no freeze wording
6. Test in the Logic debugger on the demo pair from `data/raw/_summary.md` and on
   a few alerts per look-alike scenario.

## 3. Write-back: **Save Agent Draft**

An Action that edits one Alert: sets `agent_classification`, `agent_typology`,
`agent_confidence`, `agent_evidence`, `agent_recommended_action`,
`agent_case_note`, and `agent_drafted_at`. The Logic function calls it.
It never changes `status` or creates a Case. Only an analyst does that.

## 4. Workshop

### Queue (landing page)

- Object table of **Alert**, filtered to `status != closed`, sorted by
  `risk_tier` (High, Medium, Low) then `risk_score` desc.
- Columns: alert_id, risk_tier, risk_score, flags, agent_classification,
  agent_typology, legacy_score (labelled "legacy score — for contrast"), created.
- Filters: risk_tier, agent_classification, rule.
- Selecting a row opens Customer 360 for that alert.

### Customer 360

- Header: customer occupation, income band, tenure, id_verified, cluster_size
  (duplicate profile badge if > 1). Show `full_name` for identification only.
- **Agent draft** panel: classification, typology, confidence, evidence list,
  case note. Label it "Draft — analyst decides".
- Charts: 12-month inflows by month (from Remittance), wallet cash-out timeline
  overlaid on inflows, FX conversions on the rate chart.
- Buttons: the four Actions (Clear and Release Payout, Escalate to
  Investigation, Request KYC Update, Draft SAR).

### Network

- Graph of Customer ↔ Customer links from the selected customer (2 hops), edge
  label = shared identifier, plus wallet transfers between them.
- This is where a mule ring (shared phone + money cycling) visibly differs from
  relatives sharing a phone (no transfers).

## 5. Human decision (approve / override)

Each decision Action (see `docs/foundry_s2_guide.md`) creates a Case and stores:

- `agent_classification_at_decision` = the Alert's `agent_classification`
- `agent_overridden` = true when the analyst's decision differs from
  `agent_recommended_action` (clear ↔ Clear and Release; escalate ↔ Escalate;
  request_kyc ↔ Request KYC)
- the required **reason**

The agent never executes a decision Action.

## 6. Automate

Rule: when an Alert is created or its `risk_tier` becomes Medium/High, and
`agent_drafted_at` is empty → run `draftCase`. Low-tier alerts are not sent.

For the first run, trigger it once over all existing Medium/High alerts (816
on seed 42; see `data/derived/_s2_expected.md`).

## 7. Export for evaluation (S4)

Export Alert `alert_id, agent_classification → classification,
agent_typology → typology, agent_confidence → confidence, agent_case_note →
case_note` to `data/exports/agent_results.csv`, then:

```bash
python -m src.evaluate
```

## Done when

- [ ] `agent_context` columns on Alert match `src/agent_inputs.py` for a few spot-checked alerts
- [ ] `draftCase` returns valid output for the demo pair and passes the post-checks
- [ ] Queue, Customer 360, Network pages work for the demo pair
- [ ] Every decision creates a Case with a reason and `agent_overridden`
- [ ] Automate drafted every Medium/High alert
- [ ] Prompt changes are in `specs/agent_prompt.md` with a test-log row
