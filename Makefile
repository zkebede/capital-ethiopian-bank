# Local pipeline: make all  →  generate, validate, test.
# After exporting agent results from Foundry to data/exports/agent_results.csv: make evaluate

PY ?= .venv/bin/python

.PHONY: all setup generate validate test evaluate format lint clean

all: generate validate test

setup:
	python3.11 -m venv .venv
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -r requirements.txt

generate:
	$(PY) -m src.generate

validate:
	$(PY) -m src.validate

test:
	$(PY) -m pytest

evaluate:
	$(PY) -m src.evaluate

format:
	$(PY) -m black src tests

lint:
	$(PY) -m black --check src tests

clean:
	rm -rf data/raw .pytest_cache
