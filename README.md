# Remittance Integrity Command Center

A Palantir AIP Developer Tier project. A fictional Ethiopian bank (**Tsehay Demo
Bank**) wants to win diaspora remittances back from the informal market without
taking on money-laundering risk. The app joins four bank systems into one
customer view, ranks alerts by behavioural risk, and has an AIP Logic agent
draft each case for a human analyst to clear or escalate.

This repo holds everything **outside Foundry**: the synthetic data generator,
pre-upload validation, version-controlled specs, and agent evaluation.

> All data is synthetic. No real people, banks, or employer data. All metrics
> are **on synthetic data**, not real-world accuracy.

## Quick start

```bash
make setup      # python3.11 venv + requirements
make all        # generate → validate → S1 foundation → S2 features → test
```

Or step by step:

```bash
python -m src.generate     # data/raw/*.csv in ~3s
python -m src.validate     # every check must print PASS; writes data/raw/_summary.md
python -m src.foundation   # S1 reference: clean → customer_xref → customer_activity
python -m src.features     # S2 reference: customer_links → alert_features → risk tiers
pytest                     # unit tests (typologies, S1, S2)
```

## Pipeline

```
config.yaml ──► src.generate ──► data/raw/*.csv            (full, with ground_truth)
                              ├► data/raw/upload/*.csv     (no ground_truth) ──► Foundry raw/ → Ontology
                              └► data/raw/eval/ground_truth.csv ──────────────► Foundry eval dataset
            ──► src.validate ──► PASS/FAIL + data/raw/_summary.md (row counts, planted counts, demo pair)
            ──► src.foundation ──► data/derived/ (*_clean, customer_xref, fx_daily_rate, customer_activity)
                                   + _s1_expected.md (numbers the Foundry build must match)
            ──► src.features ──► data/derived/ (customer_links, alert_features) + _s2_expected.md

Foundry: features → risk tier → AIP Logic agent → analyst Actions
            ──► export data/exports/agent_results.csv
            ──► src.evaluate ──► data/exports/eval_report.md
```

CI (`.github/workflows/ci.yml`) runs formatting, tests, generation,
validation (including the same-seed hash check), and the S1 and S2 reference
builds on every push.

## Building it in Foundry

Each sprint has a step-by-step guide. The local reference build is the spec, and
writes the numbers your Foundry datasets should match.

| Sprint | Guide | Reference | Result (synthetic data) |
|---|---|---|---|
| S1 data foundation | [`docs/foundry_s1_guide.md`](docs/foundry_s1_guide.md) | `src.foundation` | 120/120 duplicate profiles resolved, 0 false merges |
| S2 features + Ontology | [`docs/foundry_s2_guide.md`](docs/foundry_s2_guide.md) | `src.features` | 99.3% of planted alerts reach the agent; 99.9% of legitimate alerts stay Low |

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
python -m src.evaluate --results data/exports/agent_results.csv
```

Reports precision/recall/F1 for `likely_suspicious`, legacy vs agent
false-positive rates, a typology confusion matrix, and the 10 worst misses.
Alerts the agent never scored (Low tier) count as `likely_legitimate`.
Targets: recall ≥ 90% on planted cases; ≥ 80% of legitimate alerts cleared.

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
docs/                  Foundry how-to guides
specs/                 features.md · ontology.md · agent_prompt.md
tests/                 pytest suite
```
