.PHONY: install browsers seed run serve test lint format check icons screenshots

PY ?= .venv/bin/python

install:
	uv venv .venv --python 3.11 || python3 -m venv .venv
	uv pip install --python $(PY) -r requirements-dev.txt || $(PY) -m pip install -r requirements-dev.txt

browsers:
	$(PY) -m playwright install chromium

seed:
	$(PY) scripts/seed_demo.py

run:
	$(PY) app.py --dev

serve:
	$(PY) app.py

test:
	$(PY) -m pytest

lint:
	$(PY) -m ruff check src tests app.py scripts

format:
	$(PY) -m ruff format src tests app.py scripts

check: lint test

icons:
	$(PY) scripts/vendor_icons.py --prune

screenshots:
	$(PY) scripts/screenshots.py
