# FedEx Cargo Forecasting & Monitoring pipeline.
# Paths are relative so the project works even though the parent folder
# ("Coding Projects") contains a space.

PYTHON := .venv/bin/python

# Interpreter used only to bootstrap the virtualenv. Overridable, e.g.
#   make setup PYTHON_BOOTSTRAP=python3.11
# The plain ``python3`` on a machine may be older than 3.11 (which breaks the
# pinned numpy/pandas install), so a modern interpreter is chosen by default.
PYTHON_BOOTSTRAP ?= python3.12

.PHONY: setup aggregate backtest break monitor excel dashboard test lint all

setup:
	@$(PYTHON_BOOTSTRAP) -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || \
		{ echo "ERROR: '$(PYTHON_BOOTSTRAP)' is missing or older than Python 3.11; numpy/pandas will not install. Set PYTHON_BOOTSTRAP to a >=3.11 interpreter, e.g. make setup PYTHON_BOOTSTRAP=python3.12"; exit 1; }
	$(PYTHON_BOOTSTRAP) -m venv .venv
	$(PYTHON) -m pip install --upgrade pip
	$(PYTHON) -m pip install -r requirements.txt

aggregate:
	$(PYTHON) -m src.aggregate

backtest:
	$(PYTHON) -m src.backtest

break:
	$(PYTHON) -m src.break_detection

monitor:
	$(PYTHON) -m src.monitoring

excel:
	$(PYTHON) -m src.build_excel_report

dashboard:
	$(PYTHON) -m src.build_dashboard

lint:
	$(PYTHON) -m ruff check src tests conftest.py

test:
	$(PYTHON) -m pytest

all: aggregate backtest break monitor excel dashboard test
