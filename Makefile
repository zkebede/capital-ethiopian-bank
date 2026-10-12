# Local pipeline: make all  →  generate, validate, S1 foundation, S2 features, test.
# After exporting agent results from Foundry to data/exports/agent_results.csv: make evaluate

PY ?= .venv/bin/python

.PHONY: all setup generate validate foundation features test evaluate format lint clean

all: generate validate foundation features test

setup:
	python3.11 -m venv .venv
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -r requirements.txt

generate:
	$(PY) -m src.generate

validate:
	$(PY) -m src.validate

foundation:
	$(PY) -m src.foundation

features:
	$(PY) -m src.features

test:
	$(PY) -m pytest

evaluate:
	$(PY) -m src.evaluate

format:
	$(PY) -m black src tests

lint:
	$(PY) -m black --check src tests

clean:
	rm -rf data/raw data/derived .pytest_cache
