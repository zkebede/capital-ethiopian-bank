# Ontology

Six object types, six link types, four Actions. Built from the **upload**
copies in `data/raw/upload/` — `ground_truth` is never an Ontology property. It
lives only in the separate `eval` dataset (`data/raw/eval/ground_truth.csv`).

> Import note: set `phone`, all `*_id` columns, and `counterparty_wallet` to
> **String** in Foundry. Auto-inference reads `+2519…` phones as numbers and
> drops the `+`.

## Object types

### Customer
Backing dataset: `customers_clean` joined to `customer_xref` on `customer_id`
(S1; see `docs/foundry_s1_guide.md`). Primary key: `customer_id`. Title: `full_name`.
Duplicate profiles stay as separate Customer objects that share a
`resolved_entity_id`; features in S2 aggregate by entity, from `customer_activity`.

| Property | Type | Notes |
|---|---|---|
| customer_id | String | `C000123` |
| full_name | String | Display only — never a risk input |
| dob | Date | |
| phone | String | |
| address | String | |
| branch | String | Display only |
| region | String | Display only — never a risk input |
| id_type | String | |
| id_verified | Boolean | |
| occupation | String | |
| income_band | String | Derived from occupation (config) |
| onboard_date | Date | |
| diaspora_flag | Boolean | |
| resolved_entity_id | String | From `customer_xref`; duplicates share one (rule: phone + dob + given name) |
| cluster_size | Integer | From `customer_xref`; > 1 means a duplicate profile exists |
| risk_score | Integer | From the feature pipeline |
| risk_tier | String | Low / Medium / High |
| sender_fanin, cashout_hours_p50, near_threshold_ct, shared_id_links, fx_rate_timing, profile_mismatch | Double | Feature values (see `features.md`) |

### Remittance
Primary key: `txn_id`. Properties: receiver_id, sender_name, sender_country,
mto, amount_usd, payout_date (Timestamp), rate_applied.

### Wallet
One per customer. Primary key: `wallet_id`. Backed by a dataset aggregated from
`wallet_txns` (wallet_id, customer_id, txn_count_30d, outflow_etb_30d,
distinct_counterparties_30d). Individual rows stay in a **Wallet Transaction**
time series / dataset linked from the Wallet.

### FX Conversion
Primary key: `txn_id`. Properties: customer_id, usd_in, etb_out, rate,
timestamp, `within_rate_move_window` (Boolean, derived).

### Alert
Primary key: `alert_id`. Properties: customer_id, rule, legacy_score, created,
status, plus agent output written back by AIP Logic: `agent_classification`,
`agent_typology`, `agent_confidence`, `agent_evidence` (String array),
`agent_recommended_action`, `agent_case_note`.

### Case
Created by an Action. Primary key: `case_id`. Properties: alert_id, customer_id,
decision (cleared / escalated / kyc_requested / sar_drafted), decision_reason,
analyst (User), decided_at, agent_classification_at_decision,
agent_overridden (Boolean).

## Link types

| Link | Cardinality | Join |
|---|---|---|
| Customer → Remittance | 1 : many | customer_id = receiver_id |
| Customer → Wallet | 1 : 1 | customer_id |
| Customer → FX Conversion | 1 : many | customer_id |
| Customer → Alert | 1 : many | customer_id |
| Alert → Case | 1 : many | alert_id |
| Customer ↔ Customer (shares identifier) | many : many | `customer_links` dataset: (customer_a, customer_b, shared_on ∈ phone/address/device) |

## Actions

Every Action requires a free-text **reason** and logs analyst + timestamp on
the Case. The agent never executes an Action; it only proposes one.

| Action | Effect | Parameters | Guard |
|---|---|---|---|
| Clear and Release Payout | Create Case (decision = cleared); Alert.status = closed | alert, reason | — |
| Escalate to Investigation | Create Case (decision = escalated); Alert.status = escalated | alert, reason, typology | — |
| Request KYC Update | Create Case (decision = kyc_requested); notify branch | alert, reason, fields_to_update | — |
| Draft SAR | Create Case (decision = sar_drafted) with draft narrative from `agent_case_note` | alert, reason, narrative | Only on alerts already escalated |

There is deliberately **no freeze Action**.

## Workshop views (S3)

- **Queue** — alerts by risk tier, then agent classification; legacy score shown for contrast.
- **Customer 360** — profile, 30-day inflows, wallet cash-out timeline, FX vs rate chart, agent draft.
- **Network** — Customer ↔ Customer links and wallet flows (mule rings).
