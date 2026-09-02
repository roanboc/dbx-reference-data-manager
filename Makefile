.PHONY: install seed run test lint format check

PY ?= .venv/bin/python
STREAMLIT ?= .venv/bin/streamlit

install:
	uv venv .venv --python 3.11 || python3 -m venv .venv
	uv pip install --python $(PY) -r requirements-dev.txt || $(PY) -m pip install -r requirements-dev.txt

seed:
	$(PY) scripts/seed_demo.py

run:
	$(STREAMLIT) run app.py

test:
	$(PY) -m pytest

lint:
	$(PY) -m ruff check src tests app.py scripts

format:
	$(PY) -m ruff format src tests app.py scripts

check: lint test
