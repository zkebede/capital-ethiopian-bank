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
make all        # generate → validate → test
```

Or step by step:

```bash
python -m src.generate     # data/raw/*.csv in ~3s
python -m src.validate     # every check must print PASS; writes data/raw/_summary.md
pytest                     # unit tests (one per typology)
```

## Pipeline

```
config.yaml ──► src.generate ──► data/raw/*.csv            (full, with ground_truth)
                              ├► data/raw/upload/*.csv     (no ground_truth) ──► Foundry raw/ → Ontology
                              └► data/raw/eval/ground_truth.csv ──────────────► Foundry eval dataset
            ──► src.validate ──► PASS/FAIL + data/raw/_summary.md (row counts, planted counts, demo pair)

Foundry: features → risk tier → AIP Logic agent → analyst Actions
            ──► export data/exports/agent_results.csv
            ──► src.evaluate ──► data/exports/eval_report.md
```

CI (`.github/workflows/ci.yml`) runs formatting, tests, generation, and
validation, including the same-seed hash check, on every push.

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
specs/                 features.md · ontology.md · agent_prompt.md
tests/                 pytest suite
```
