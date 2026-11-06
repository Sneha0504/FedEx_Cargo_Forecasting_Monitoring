# FedEx Cargo Forecasting & Monitoring pipeline.
# Paths are relative so the project works even though the parent folder
# ("Coding Projects") contains a space.

PYTHON := .venv/bin/python

.PHONY: setup aggregate backtest break monitor excel dashboard test lint all

setup:
	python3 -m venv .venv
	$(PYTHON) -m pip install --upgrade pip
	$(PYTHON) -m pip install -r requirements.txt

aggregate:
	$(PYTHON) src/aggregate.py

backtest:
	$(PYTHON) src/backtest.py

break:
	$(PYTHON) src/break_detection.py

monitor:
	$(PYTHON) src/monitoring.py

excel:
	$(PYTHON) src/build_excel_report.py

dashboard:
	$(PYTHON) src/build_dashboard.py

lint:
	$(PYTHON) -m ruff check src tests

test:
	$(PYTHON) -m pytest

all: aggregate backtest break monitor excel dashboard test
