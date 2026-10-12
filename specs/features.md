# Risk features

Per customer, rolling 30 days, computed in a Foundry pipeline from the cleaned
datasets. Keep this file in sync with what is built. Record tuned thresholds in
the **Final** column.

> **Fairness rule:** no feature may use nationality, ethnicity, religion,
> region, branch, sender/beneficiary country, or any part of a name. Features
> describe *behaviour* only.

## Features

| Feature | Definition | Source | Flag | Points | Final |
|---|---|---|---|---:|---|
| `sender_fanin` | Distinct `sender_name` values paying this receiver | remittances | ≥ 10 | 25 | |
| `cashout_hours_p50` | Median hours from each inflow (`payout_date`) to the customer's next outbound wallet txn | remittances + wallet_txns | ≤ 6 | 20 | |
| `near_threshold_ct` | Payouts with `amount_usd` in [0.90, 1.00) × `reporting_threshold_usd` | remittances | ≥ 3 | 20 | |
| `shared_id_links` | Other customers sharing phone, address, or device (from `customer_links`) | customers | ≥ 2 | 15 | |
| `fx_rate_timing` | Share of the customer's FX conversions made within 24h after a day whose rate moved > 2% vs the previous day | fx_conversions | ≥ 50% | 10 | |
| `profile_mismatch` | 30-day inflow USD ÷ expected monthly inflow for the occupation's income band | remittances + fx_conversions + customers | ≥ 5× | 10 | |

Expected monthly inflow by band (from `config.yaml`): low 300 · medium 800 ·
high 2,500 USD. Occupation → band mapping also lives in `config.yaml`.

## Score and tiers

`risk_score` = sum of points for flagged features (0–100).

| Tier | Score | Routing |
|---|---|---|
| Low | < 30 | Not sent to the agent; stays in the legacy queue as low priority |
| Medium | 30–59 | Agent drafts a case |
| High | ≥ 60 | Agent drafts a case; top of the analyst queue |

## Which planted typology each feature should catch

| Typology | Primary features | Expected score |
|---|---|---|
| structuring | `near_threshold_ct`, `profile_mismatch` | 30 (Medium) |
| collector | `sender_fanin`, `profile_mismatch` | 35 (Medium) |
| pass_through | `cashout_hours_p50`, `profile_mismatch` (low/medium bands only) | 20–30 ⚠️ |
| mule_ring | `shared_id_links`, plus `profile_mismatch` on large P2P flows | 15–25 ⚠️ |
| fx_out_of_profile | `fx_rate_timing`, `profile_mismatch` | 20 ⚠️ |
| duplicate_profile | `shared_id_links` = 1 (below flag) | Low — routed to entity resolution, not AML |

⚠️ With the starting weights, mule rings, FX timing, and some pass-through customers can land in Low and never
reach the agent. `evaluate.py` reports these as "never reached the agent".
Options when tuning: lower `shared_id_links` to ≥ 1 *with* a wallet-cycle check,
or let any single 20+ point feature force Medium.

## Known legitimate look-alikes (should stay Low)

- Holiday spikes (Genna, Timket, Fasika, Eid, Enkutatash, Meskel): more and
  larger family payments in the 14 days before. Do not treat volume alone as risk.
- Families with 3 regular senders (siblings abroad): `sender_fanin` = 3.
- Normal cash-out 3–30 days after payout.
- Legitimate large US transfers (house/business) above the threshold, reported
  with `ctr_flag = True`.

## Tuning log

| Date | Change | Recall (synthetic) | Legit cleared (synthetic) |
|---|---|---:|---:|
| | Initial weights above | | |
