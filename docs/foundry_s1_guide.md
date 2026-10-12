# Sprint 1 in Foundry — data foundation

Goal: raw CSVs in Foundry → cleaned datasets → `customer_xref` → `customer_activity`,
with numbers that match the local reference run.

Each step below mirrors a function in this repo. If Foundry and the reference
disagree, the reference code is the spec: fix the board, or fix the code and the
spec together.

| Step | Reference code | Foundry output |
|---|---|---|
| 1. Upload | `src/generate.py` (upload split) | `raw/*` datasets, `eval/ground_truth` |
| 2. Clean | `src/clean.py` | `clean/<table>_clean` |
| 3. Entity resolution | `src/entity_resolution.py` | `clean/customer_xref` |
| 4. Daily rate | `src/customer_activity.py::fx_daily_rate` | `clean/fx_daily_rate` |
| 5. Activity | `src/customer_activity.py::build_activity` | `clean/customer_activity` |

> Pipeline Builder transform names below are descriptive. If one is labelled
> differently in your version, search the transform picker for the operation.

---

## 0. Before you start

```bash
make all      # generate → validate → foundation → test
```

Keep `data/derived/_s1_expected.md` open. It holds every number you'll check against.

## 1. Project and upload

1. Create a Foundry project, e.g. **Remittance Integrity**, with folders
   `raw/`, `clean/`, `eval/`.
2. Upload the **six files in `data/raw/upload/`** to `raw/` (not the files in
   `data/raw/`, which still contain `ground_truth`).
3. Upload `data/raw/eval/ground_truth.csv` to `eval/`. Nothing in the
   pipeline or Ontology should read from `eval/`. Only the S4 evaluation page uses it.
   If your tier supports it, restrict who can view that folder.
4. When applying a schema to each raw dataset, keep everything as **String**.
   Typing happens in step 2, so `+2519…` phones and zero-padded IDs survive.

**Check:** row counts on each raw dataset match the "Rows in" column of
`_s1_expected.md`.

## 2. Clean (one Pipeline Builder pipeline, six inputs)

For each raw dataset:

1. **Trim / collapse whitespace** on every string column.
2. **Cast** to the types below.
3. `customers` only:
   - **Regex replace** `phone`: remove `[^0-9]`, then prefix `+` (E.164).
   - **Add column** `income_band` from `occupation` with a case/when (or join to
     a small mapping table). The mapping is in `config.yaml → occupations`:
     low = farmer, student, homemaker, daily_laborer, retired · medium =
     small_trader, driver, teacher, civil_servant, nurse · high =
     business_owner, engineer, physician.
4. **Filter** out rows where the primary key is null.
5. **Drop duplicates** across all columns.
6. Output to `clean/<table>_clean`.

### Column types

| Dataset | Primary key | Date | Timestamp | Double | Integer | Boolean |
|---|---|---|---|---|---|---|
| customers | customer_id | dob, onboard_date | | | | id_verified, diaspora_flag |
| remittances | txn_id | | payout_date | amount_usd, rate_applied | | |
| wallet_txns | wallet_txn_id | | timestamp | amount_etb | | |
| fx_conversions | txn_id | | timestamp | usd_in, etb_out, rate | | |
| alerts | alert_id | | created | | legacy_score | |
| us_outbound | txn_id | | send_date | amount_usd | | ctr_flag |

Everything else stays **String**. Timestamps are `yyyy-MM-dd HH:mm:ss`;
dates are `yyyy-MM-dd`. Booleans are the text `True` / `False`.

**Check:** "Rows out" matches `_s1_expected.md`. The synthetic data is generated
clean, so nothing is dropped; the steps exist for real feeds. Spot-check
that `phone` still starts with `+251`.

## 3. Entity resolution → `customer_xref`

Rule: **same phone + same dob + same given name** = same person.

1. Input `customers_clean`.
2. **Add column** `given_name` = lower(first token of `full_name`)
   (split on space, take element 0, lower-case).
3. **Window**: partition by `phone, dob, given_name`:
   - `anchor = min(customer_id)`
   - `cluster_size = count(*)`
4. Rows with null phone/dob/given_name: `anchor = customer_id`, `cluster_size = 1`.
5. **Add column** `resolved_entity_id` = `"E"` + substring(`anchor`, 2) (drop the `C`).
6. **Add column** `match_rule` = `phone+dob+given_name` if `cluster_size > 1`,
   else `single_record`.
7. Keep `customer_id, resolved_entity_id, cluster_size, match_rule` → `clean/customer_xref`.

**Check against `_s1_expected.md`:**
- 20,000 rows; 19,880 distinct `resolved_entity_id`; 120 clusters of size 2.
- The five spot-check clusters listed there resolve to the same entity.
- Mule-ring members share a phone or address but **must not** merge (different
  dob and name). They become Customer ↔ Customer links in S2.

Why not fuzzy matching? It would also find these pairs, but it is harder to
reproduce exactly and to explain in a demo. Note it as a "next step" instead.

## 4. Daily reference rate → `fx_daily_rate`

1. Input `remittances_clean`.
2. **Add column** `day` = date(`payout_date`).
3. **Aggregate** by `day`: `etb_per_usd = median(rate_applied)`.

(If median isn't available, use `percentile_approx(rate_applied, 0.5)`.
Values may differ in the 4th decimal; that's fine.)

## 5. `customer_activity`

Build three branches and **union by name**:

| Column | remittances_clean | wallet_txns_clean | fx_conversions_clean |
|---|---|---|---|
| event_id | txn_id | wallet_txn_id | txn_id |
| source | `remittance` | `wallet` | `fx` |
| direction | `inflow` | `outflow` | `conversion` |
| customer_id | receiver_id | customer_id | customer_id |
| event_ts | payout_date | timestamp | timestamp |
| amount_usd | amount_usd | amount_etb ÷ `fx_daily_rate` of that day (round 2) | usd_in |
| amount_etb | amount_usd × rate_applied (round 2) | amount_etb | etb_out |
| counterparty | sender_name | counterparty_wallet | null |
| detail | mto | channel | null |

Then **left join** `customer_xref` on `customer_id` to add `resolved_entity_id`.
The wallet branch needs a left join to `fx_daily_rate` on date(`timestamp`) = `day`.

**Check:** 184,000 rows; zero null `resolved_entity_id`; per-source row
counts and USD/ETB totals match `_s1_expected.md` (within 0.1%).

## Done when

- [ ] All six `*_clean` datasets match the expected row counts
- [ ] `customer_xref` has 19,880 entities and the spot-check clusters resolve
- [ ] `customer_activity` per-source totals match
- [ ] `eval/ground_truth` exists and nothing in `clean/` reads it
- [ ] Any generator bug found on upload is fixed here, and `make all` still passes
