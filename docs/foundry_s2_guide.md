# Sprint 2 in Foundry — features and Ontology

Goal: S1 datasets → `customer_links` → `alert_features` (six features, score, tier)
→ Ontology with 6 object types, 6 links, 4 Actions.

| Step | Reference code | Foundry output |
|---|---|---|
| 1. Links | `src/features.py::customer_links` | `features/customer_links` |
| 2. Cash-out timing | `src/features.py::inflow_cashout` | `features/inflow_cashout` |
| 3. Rate-move days | `src/features.py::rate_move_days` | `features/rate_move_days` |
| 4. Alert features + score | `src/features.py::alert_features`, `score` | `features/alert_features` |
| 5. Ontology | `specs/ontology.md` | Object types, links, Actions |

Thresholds and points are in `config.yaml → features`; definitions and the
tuning log are in `specs/features.md`. Expected numbers:
`data/derived/_s2_expected.md` (run `make all`).

> Pipeline Builder transform names are descriptive. If one is labelled
> differently in your version, search the transform picker for the operation.

---

## 1. `customer_links`

Input: `customers_clean`, `customer_xref`.

1. Branch A: select `customer_id, phone`, drop null phone. **Join** to itself
   on `phone` (inner). **Filter** `customer_id_left < customer_id_right`.
   Add `shared_on = "phone"`.
2. Branch B: same on `address`, `shared_on = "address"`.
3. **Union** A and B. **Aggregate** by (`customer_a`, `customer_b`):
   `shared_on` = sorted concat of values with `+` (`address+phone` when both).
4. **Join** `customer_xref` twice to get each side's `resolved_entity_id`.
   Add `same_entity = entity_a == entity_b`.

**Check:** 457 pairs: 120 same-entity (duplicate profiles), 337 risk links (mule rings and relatives sharing a phone).

## 2. `inflow_cashout`

For each remittance, when did the same entity next move money out of its wallet?

Input: `customer_activity`.

1. Inflows = `source == "remittance"` (event_id, resolved_entity_id, event_ts).
2. Outflows = `source == "wallet"` (resolved_entity_id, event_ts as `out_ts`).
3. **Join** inflows → outflows on `resolved_entity_id`, then **filter**
   `out_ts >= event_ts AND out_ts <= event_ts + 720h`.
4. **Aggregate** by inflow `event_id`: `next_outflow_ts = min(out_ts)`.
5. **Left join** back onto all inflows (no outflow → null).

## 3. `rate_move_days`

Input: `fx_daily_rate`.

1. **Window** ordered by `day`: `prev = lag(etb_per_usd)`.
2. `pct = abs(etb_per_usd / prev - 1)`; **filter** `pct > 0.02`.

**Check:** 12 days (the planted jumps).

## 4. `alert_features`

Inputs: `alerts_clean`, `customer_xref`, `customer_activity`, `inflow_cashout`,
`rate_move_days`, `customer_links`, `customers_clean`.

1. Alerts + `resolved_entity_id` (join `customer_xref` on `customer_id`).
2. Activity + `next_outflow_ts` (left join `inflow_cashout` on `event_id`) +
   `is_move_day` (left join `rate_move_days` on date(`event_ts`) = `day`).
3. **Join** alerts → activity on `resolved_entity_id`; **filter**
   `event_ts > created - 30 days AND event_ts <= created`.
4. Add per-row helpers:
   - `cashout_hours = hours(next_outflow_ts - event_ts)`, **only if**
     `next_outflow_ts <= created` (no look-ahead), else null.
   - `fast_usd = amount_usd if source == "remittance" and cashout_hours <= 6 else 0`.
   - `near = source == "remittance" and 9000 <= amount_usd < 10000`.
5. **Aggregate** by `alert_id`:

   | Output | Expression |
   |---|---|
   | `sender_fanin` | count distinct `counterparty` where source = remittance |
   | `cashout_fast_share` | sum(`fast_usd`) ÷ sum(`amount_usd` where source = remittance) |
   | `near_threshold_ct` | count where `near` |
   | `fx_rate_timing` | avg(`is_move_day` as 0/1) where source = fx (null if none) |
   | `inflow_usd_30d` | sum(`amount_usd`) where source ∈ {remittance, fx} |
   | `cashout_hours_p50` | median(`cashout_hours`) where source = remittance (context) |

6. **Left join** back onto all alerts (alerts with no activity get 0 / null).
7. `shared_id_links`: from `customer_links` where `same_entity = false`, count
   distinct other entities per entity (count both directions); join on
   `resolved_entity_id`; null → 0.
8. `profile_mismatch = inflow_usd_30d / expected` where expected is
   300 / 800 / 2500 for `income_band` low / medium / high.
9. **Score**, one column per feature (null never flags):

   | Column | Rule |
   |---|---|
   | `pts_sender_fanin` | 25 if `sender_fanin >= 10` |
   | `pts_cashout_fast_share` | 30 if `cashout_fast_share >= 0.5` |
   | `pts_near_threshold_ct` | 20 if `near_threshold_ct >= 3` |
   | `pts_shared_id_links` | 30 if `shared_id_links >= 2` |
   | `pts_fx_rate_timing` | 20 if `fx_rate_timing >= 0.5` |
   | `pts_profile_mismatch` | 10 if `profile_mismatch >= 5` |

   `risk_score` = sum; `risk_tier` = High if ≥ 60, Medium if ≥ 30, else Low.

**Check against `_s2_expected.md`:** 6,000 rows; tiers Low 5,184 · Medium 816 ·
High 0; per-feature flag counts match. Join `eval/ground_truth` **only on an
evaluation page**, never in this pipeline, to confirm 99.7% of planted alerts
are Medium/High.

## 5. Ontology

Follow `specs/ontology.md` for every property. Order matters: objects, then links, then Actions.

### Object types

| Object | Backing dataset | Primary key | Title |
|---|---|---|---|
| Customer | `customers_clean` ⋈ `customer_xref` (+ `shared_id_links`) | customer_id | full_name |
| Remittance | `remittances_clean` | txn_id | txn_id |
| Wallet | aggregate of `wallet_txns_clean` by wallet_id | wallet_id | wallet_id |
| FX Conversion | `fx_conversions_clean` (+ `is_move_day`) | txn_id | txn_id |
| Alert | `alerts_clean` ⋈ `alert_features` | alert_id | alert_id |
| Case | new writeback dataset (empty to start) | case_id | case_id |

Mark `full_name` and `region` as display-only in their descriptions. Add the
`agent_*` properties to Alert now, empty; S3 writes them.

### Link types

| Link | From → To | Key |
|---|---|---|
| Customer → Remittance | Customer.customer_id = Remittance.receiver_id | 1 : many |
| Customer → Wallet | customer_id | 1 : 1 |
| Customer → FX Conversion | customer_id | 1 : many |
| Customer → Alert | customer_id | 1 : many |
| Alert → Case | alert_id | 1 : many |
| Customer ↔ Customer | backed by `customer_links` (customer_a, customer_b) | many : many |

### Actions

Every Action has a required **reason** parameter, and creates a Case with the
analyst and timestamp. There is no freeze Action.

| Action | Parameters | Edits |
|---|---|---|
| Clear and Release Payout | alert, reason | create Case (decision = cleared); Alert.status = closed |
| Escalate to Investigation | alert, reason, typology | create Case (decision = escalated); Alert.status = escalated |
| Request KYC Update | alert, reason, fields_to_update | create Case (decision = kyc_requested) |
| Draft SAR | alert, reason, narrative (default = `agent_case_note`) | create Case (decision = sar_drafted). Submission criterion: Alert.status = escalated |

On each Case also store `agent_classification_at_decision` and
`agent_overridden` (analyst's decision differs from the agent's recommendation).
S4's evaluation uses both.

## Done when

- [ ] `customer_links`: 457 pairs (120 same-entity)
- [ ] `rate_move_days`: 12 days
- [ ] `alert_features`: tier counts and per-feature flag counts match `_s2_expected.md`
- [ ] Ontology: 6 object types, 6 links, 4 Actions, each Action requires a reason
- [ ] The demo pair from `data/raw/_summary.md` opens in Object Explorer with features and tier
- [ ] `specs/features.md` and `specs/ontology.md` match what you built
