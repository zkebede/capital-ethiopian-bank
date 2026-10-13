# Sprint 4 in Foundry: evaluate, re-point, record, submit

Goal: prove the app works with numbers, show it re-points to a second bank,
and record a demo under 5 minutes.

| Step | Reference | Foundry output |
|---|---|---|
| 1. Export agent results | `src/evaluate.py` | `data/exports/agent_results.csv` → `eval_report.md` |
| 2. Evaluation page | `data/exports/eval_report_baseline.md` | Workshop **Evaluation** page |
| 3. Re-point to US outbound | `src/repoint.py`, `data/derived/_s4_expected.md` | `us_party_flags` + US objects + Queue |
| 4. Polish | checklist below | — |
| 5. Record | demo script below | ≤ 5 min video |
| 6. Submit | checklist below | — |

Run `make all` first. It writes every reference number used below.

> Transform and widget names are descriptive. If one is labelled differently
> in your version, search for the operation.

---

## 1. Export agent results and score them

1. In Object Explorer, filter **Alert** to `agent_drafted_at` is not empty.
2. Export these properties to CSV:

   | Alert property | CSV column |
   |---|---|
   | alert_id | alert_id |
   | agent_classification | classification |
   | agent_typology | typology |
   | agent_confidence | confidence |
   | agent_case_note | case_note |

3. Save it as `data/exports/agent_results.csv` and run:

   ```bash
   make evaluate          # → data/exports/eval_report.md
   ```

4. Compare with the rules baseline in `data/exports/eval_report_baseline.md`.
   The agent should beat it in the **agent alone** section, mainly on
   *Legitimate alerts cleared* (baseline 57.8%) and on the look-alike table
   (baseline clears 27.5% of community collections and 43.3% of steady traders).
5. Add a row to the test log in `specs/agent_prompt.md` with the Foundry numbers.

## 2. Evaluation page (Workshop)

The hidden answers stay out of the app. Keep them in their own folder and use
them only on this page.

1. Upload `data/raw/eval/ground_truth.csv` to a separate folder, `eval/`, that
   the Queue, Customer 360 and Network pages never read.
2. Pipeline `eval/alert_eval`: filter `file = "alerts"`, join to the Alert
   backing dataset on `key = alert_id`, and keep `alert_id, ground_truth,
   scenario, risk_tier, agent_classification, agent_typology`. Alerts with no
   draft get `agent_classification = likely_legitimate` (Low tier, closed by
   the risk tier).
3. Back an **Alert Evaluation** object type with it (read-only).
4. Workshop page **Evaluation**, with a banner: *"All metrics are on synthetic
   data with planted patterns."*

   | Widget | Shows | Should match |
   |---|---|---|
   | Metric card | Recall: suspicious not marked likely_legitimate | `eval_report.md` Targets |
   | Metric card | Legitimate alerts cleared | `eval_report.md` Targets |
   | Bar chart | Legitimate alerts sent to an analyst: legacy 100% vs agent | False positives table |
   | Pivot table | ground_truth × agent_typology | Typology confusion matrix |
   | Table | scenario → share cleared | Legitimate look-alikes table |
   | Metric card | Override rate: Cases with `agent_overridden = true` ÷ all Cases | (new; from your own decisions) |

## 3. Re-point to the US bank

The story: the same Ontology and agent work for a fictional US bank's
outbound transfers to East Africa. Only two of the six features exist on this
data, so parties are **flagged**, not scored.

1. Upload `data/raw/upload/us_outbound.csv` to `raw/us/` (no labels).
2. Pipeline `features/us_party_flags`, with the S2 thresholds:
   - **Senders**: keep transfers with `amount_usd` in [9,000, 10,000). Self-join on
     `sender_id` where the earlier transfer is in the 30 days up to each transfer,
     count per transfer, then take the max per sender → `near_threshold_ct`. Flag if ≥ 3.
   - **Beneficiaries**: self-join all transfers on `beneficiary_id` the same way,
     count distinct `sender_id` per transfer, then take the max per beneficiary
     → `sender_fanin`. Flag if ≥ 10.
   - Union to `party_type, party_id, feature, value, threshold, flagged,
     suggested_typology`.
3. **Check against `_s4_expected.md`:** 15 senders flagged of 2,834; 8
   beneficiaries flagged of 4,008.
4. Ontology: **Outbound Transfer** (txn_id), **US Party** (party_type +
   party_id), and a link US Party → Outbound Transfer. Never use
   `beneficiary_country` in a feature or the agent's inputs.
5. Duplicate the Queue page as **US Queue** over US Party, filtered to
   `flagged = true`. Reuse the same decision Actions, with the same required reason.

## 4. Polish checklist

- [ ] Bank names are fictional: **Capital Ethiopian Bank** and **Hudson Demo Bank**
      (checked 2026-10-13; check again before recording)
- [ ] A "synthetic data" banner on every page
- [ ] No page except Evaluation reads `eval/`
- [ ] `legacy_score` is labelled "legacy score, for contrast"
- [ ] The agent panel says "Draft, analyst decides"
- [ ] Every Action requires a reason, and there is no freeze Action
- [ ] Empty states read well (an alert with no draft, a customer with no links)

## 5. Demo script (≤ 5 min)

Before recording, run `draftCase` once by hand on **A000174**. It is Low tier,
so Automate skips it, and the demo needs its draft.

| Time | Screen | Say / do |
|---|---|---|
| 0:00–0:30 | Title | Problem: the bank either freezes too broadly and loses diaspora customers, or misses laundering. Ninety percent of legacy alerts are legitimate. |
| 0:30–1:00 | Legacy view | **A002896** and **A000174**: both `MULTIPLE_SENDERS`, both legacy score 83. The old queue can't tell them apart. |
| 1:00–1:45 | Queue → A000174 | Risk tier Low: 1 sender, $810 in 30 days, in line with the profile. The agent's draft says likely_legitimate. **Clear and Release Payout** with a reason. A family paid in seconds. |
| 1:45–3:00 | Queue → A002896 | Medium: 70 senders in 30 days, $37,895, 47× the expected inflow. Read the agent's evidence. **Escalate to Investigation** with a reason. The Case records whether the analyst followed the agent. |
| 3:00–3:30 | Network | A mule ring (shared phone, money moving between members) next to relatives sharing a phone (no transfers). Same flag, different story. |
| 3:30–4:15 | Evaluation | Recall, legitimate alerts cleared, legacy vs agent false positives, look-alikes. Say "on synthetic data". |
| 4:15–4:50 | US Queue | Same features on Hudson Demo Bank's outbound data: 15 structuring senders, 8 collector beneficiaries, no ordinary parties flagged. |
| 4:50–5:00 | Close | The agent proposes; a human decides with a logged reason. Behaviour only, never nationality, region or name. |

## 6. Submit

- [ ] Video ≤ 5 min, recorded at 1080p with system notifications off
- [ ] Repo public, with no `project_instruction.md` and no data files
      (`git ls-files data` prints nothing)
- [ ] README results match `eval_report.md` from Foundry, not just the baseline
- [ ] `specs/` match what you built (features, Ontology, prompt and test log)
- [ ] CI green on `main`

## Done when

- [ ] `eval_report.md` from the Foundry export meets both targets
- [ ] The Evaluation page matches `eval_report.md`
- [ ] US re-point flags match `_s4_expected.md`
- [ ] Demo recorded and submitted
