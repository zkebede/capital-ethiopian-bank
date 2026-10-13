# Remittance Integrity Command Center

A Palantir AIP Developer Tier project. A fictional Ethiopian bank (**Capital
Ethiopian Bank**) wants to win diaspora remittances back from the informal
market without taking on money-laundering risk. Today it either freezes
accounts too broadly and loses good customers, or misses real laundering.

The app joins four bank systems into one customer view, ranks alerts by
behavioural risk, and has an AIP Logic agent draft each case. A human analyst
always makes the decision and logs a reason.

This repo holds everything **outside Foundry**: the synthetic data generator,
pre-upload validation, version-controlled specs, reference builds for each
sprint, and the agent evaluation.

> All data is synthetic. No real people, banks, or employer data. All metrics
> are **on synthetic data**, not real-world accuracy.

## Results (synthetic data)

| | Legacy rules | This app |
|---|---:|---:|
| Legitimate alerts sent to an analyst | 100% | 1.7% |
| Planted laundering cases kept for review | — | 99.7% |
| Flagged alerts that turn out legitimate | 90% | 7.7% |
| Typology named correctly | — | 100% |

These numbers come from the offline rules baseline on all 6,000 alerts
(`make evaluate-baseline`). Replace them with the Foundry agent's numbers from
`make evaluate` before submitting.

The same features re-point to a fictional US bank's outbound transfers with no
changes: 15 of 15 structuring senders and 8 of 8 collector beneficiaries are
flagged, and no ordinary parties are.

**Demo:** two alerts with the same legacy rule and score (83). One is a family
receiving $810 from one sender: cleared in seconds. The other has 70 senders
and $37,895 in 30 days: escalated with evidence.

## Quick start

```bash
make setup      # python3.11 venv + requirements
make all        # generate → validate → S1 → S2 → S3 baseline agent → S4 re-point + evaluation → test
```

Or step by step:

```bash
python -m src.generate     # data/raw/*.csv in ~3s
python -m src.validate     # every check must print PASS; writes data/raw/_summary.md
python -m src.foundation   # S1 reference: clean → customer_xref → customer_activity
python -m src.features     # S2 reference: customer_links → alert_features → risk tiers
python -m src.agent        # S3 prompt harness (offline baseline; --backend claude for the real prompt)
python -m src.repoint      # S4 re-point: the same features on the US bank's outbound data
pytest                     # unit tests (typologies, S1–S4)
```

## Pipeline

The work happens in three phases. You build and check everything locally,
rebuild it in Foundry, and then bring the agent's results back to score them.

```
  LOCAL                          FOUNDRY                         LOCAL
  ─────                          ───────                         ─────

  config.yaml                    upload/*.csv
      │                              │
      ▼                              ▼
  1. generate ──── upload ────►  features + risk tier
      │                              │
      ▼                              ▼
  2. validate                    AIP Logic agent
      │                              │
      ▼                              ▼
  3. foundation (S1)             analyst Actions
      │                              │
      ▼                              ▼
  4. features (S2)               export agent_results.csv ──►  7. evaluate
      │                                                            │
      ▼                                                            ▼
  5. agent (S3)                                                eval_report.md
      │
      ▼
  6. repoint (S4)
```

### Local steps

Run them all with `make all`. Each step reads the previous step's output.

| # | Command | What it does | Writes |
|---|---|---|---|
| 1 | `python -m src.generate` | Builds the synthetic bank data from `config.yaml` | `data/raw/` (see below) |
| 2 | `python -m src.validate` | Checks the data. Every line must print **PASS** | `data/raw/_summary.md`: row counts, planted cases, demo pair |
| 3 | `python -m src.foundation` | **S1:** cleans the data and links duplicate profiles | `data/derived/`: `*_clean`, `customer_xref`, `fx_daily_rate`, `customer_activity` |
| 4 | `python -m src.features` | **S2:** computes risk features and tiers | `data/derived/`: `customer_links`, `alert_features` |
| 5 | `python -m src.agent` | **S3:** tests the agent prompt | `data/exports/agent_results_<backend>.csv`, `agent_test_report.md` |
| 6 | `python -m src.repoint` | **S4:** runs the same features on the US bank's data | `data/derived/us_party_flags.csv` |

Steps 3, 4 and 6 also write `_s1_expected.md`, `_s2_expected.md` and
`_s4_expected.md`. These are the numbers your Foundry build should match.

`make all` also runs `make evaluate-baseline`: the rules baseline on every
Medium/High alert, scored like a Foundry export. It writes
`data/exports/eval_report_baseline.md`, the number the real agent has to beat.

### What `generate` writes

| Folder | Contents | Where it goes |
|---|---|---|
| `data/raw/*.csv` | Full data, **with** the hidden answers | Stays local |
| `data/raw/upload/*.csv` | Same data, answers removed | Upload to Foundry `raw/` |
| `data/raw/eval/ground_truth.csv` | Only the answers | Foundry eval dataset (scoring only) |

### Foundry and back

1. Upload `data/raw/upload/` and build the features, risk tiers, AIP Logic
   agent and analyst Actions. The sprint guides below walk through each step.
2. Export the agent's drafts to `data/exports/agent_results.csv`.
3. Run `make evaluate` (step 7). It compares the drafts with the hidden answers
   and writes `data/exports/eval_report.md`.

CI (`.github/workflows/ci.yml`) runs formatting, tests, generation,
validation (including the same-seed hash check), the S1 and S2 reference
builds, the offline S3 baseline agent, the S4 re-point and the baseline
evaluation on every push. It never calls a paid API.

## Building it in Foundry

Each sprint has a step-by-step guide. The local reference build is the spec, and
writes the numbers your Foundry datasets should match.

| Sprint | Guide | Reference | Result (synthetic data) |
|---|---|---|---|
| S1 data foundation | [`docs/foundry_s1_guide.md`](docs/foundry_s1_guide.md) | `src.foundation` | 120/120 duplicate profiles resolved, 0 false merges |
| S2 features + Ontology | [`docs/foundry_s2_guide.md`](docs/foundry_s2_guide.md) | `src.features` | 99.7% of planted alerts reach the agent; 96.0% of legitimate alerts stay Low |
| S3 app + agent | [`docs/foundry_s3_guide.md`](docs/foundry_s3_guide.md) | `src.agent` | Prompt harness; rules baseline catches 100% of suspicious, clears 35% of legitimate on the 45-alert test sample |
| S4 evaluate + submit | [`docs/foundry_s4_guide.md`](docs/foundry_s4_guide.md) | `src.evaluate`, `src.repoint` | Evaluation page, US re-point, demo script, submission checklist |

## Testing the agent prompt locally

The prompt lives in `specs/agent_prompt.md`; the harness reads it from there.

```bash
python -m src.agent --backend baseline            # offline rules baseline
python -m src.agent --backend claude --sample 50  # the real prompt (Claude API, paid)
```

The Claude backend needs credentials: `export ANTHROPIC_API_KEY=...`. A
45-alert run on `claude-opus-5-5` costs roughly $1–2. Model and effort are
in `config.yaml → agent`.

## Legitimate look-alikes

Besides the planted laundering patterns, the data contains legitimate
behaviour that looks suspicious: one-off community collections, steady
traders paid near the threshold, and relatives sharing a phone. They are
labelled legitimate (hidden `scenario` column) and reach the agent as Medium,
so the evaluation measures judgement, not just pattern matching.

## Data

| File | Rows | Planted patterns |
|---|---:|---|
| customers.csv | 20,000 | every typology (customer-level label) + duplicate profiles |
| remittances.csv | 120,000 | structuring, collector, pass_through |
| wallet_txns.csv | 60,000 | pass_through, mule_ring |
| fx_conversions.csv | 4,000 | fx_out_of_profile |
| alerts.csv | 6,000 | legacy queue, 90% legitimate |
| us_outbound.csv | 10,000 | structuring, collector (fictional US bank, re-point demo) |

Two ID columns support joins: `wallet_txns.wallet_txn_id` (primary
key) and `us_outbound.beneficiary_id` (names alone can't identify a beneficiary).

## Evaluation

```bash
make evaluate            # scores the Foundry export, data/exports/agent_results.csv
make evaluate-baseline   # same report for the offline rules baseline
```

The report covers:

- the two targets: recall ≥ 90% on planted cases, and ≥ 80% of legitimate alerts cleared
- precision, recall and F1 for `likely_suspicious`
- false positives, legacy rules vs agent
- **the agent alone** on the Medium/High alerts it actually reviewed
- a typology confusion matrix, the look-alike results, and the 10 worst misses

Alerts the agent never saw (Low tier) count as `likely_legitimate`. That is why
the system totals are higher than the agent-alone numbers.

## Repo layout

```
config.yaml            seed, row counts, thresholds, bank name, generator params
src/generate.py        entry point
src/gen_*.py           one generator per file
src/typologies.py      one function per planted pattern
src/validate.py        pre-upload checks + _summary.md
src/evaluate.py        agent output vs ground truth
src/foundation.py      S1 reference build (entry point)
src/clean.py           S1 cleaning + Foundry column types
src/entity_resolution.py   S1 customer_xref
src/customer_activity.py   S1 fx_daily_rate + customer_activity
src/features.py        S2 customer_links, alert features, risk score + tier
src/agent_inputs.py    S3 agent payload (the agent_context contract) + fairness keys
src/agent.py           S3 prompt harness: baseline + Claude backends, post-checks
src/repoint.py         S4 US outbound re-point: party flags + _s4_expected.md
docs/                  Foundry how-to guides
specs/                 features.md · ontology.md · agent_prompt.md
tests/                 pytest suite
```
