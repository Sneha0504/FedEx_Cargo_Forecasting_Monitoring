# PROJECT SPEC (internal reference)

This file is the standing specification for the project. It is internal and is
not linked from the public README.

Project name:   FedEx_Cargo_Forecasting_Monitoring
Target role:    Data Scientist / Forecasting Analyst (logistics, transportation, supply chain)

## Goal

Forecast FedEx's monthly air cargo freight volume, at both the network level and
its largest individual hubs, using real federal flight data. Compare several
forecasting methods on a rolling backtest, then build a production-style
monitoring system that flags when the forecast's error grows beyond its normal
range, the way a team would detect a real business change (not just noise) in a
live forecasting pipeline.

## Key questions

1. How has FedEx's air cargo freight volume and market share trended over the
   available years?
2. Which forecasting method performs best on a rolling, out-of-sample backtest,
   at the network level and at the level of FedEx's largest hubs?
3. Is there a real break in the data where FedEx's freight volume shifted
   suddenly (domestic vs international), and can it be traced to an identifiable
   real-world cause?
4. Can a monitoring system, using only information available at each point in
   time, flag that break as soon as it happens, rather than only being visible
   in hindsight?

## Required tools

Python (pandas, NumPy, statsmodels, scikit-learn), SQL (SQLite or DuckDB for the
aggregation queries), Excel (openpyxl, live formulas), Plotly (static published
dashboard), Git, GitHub CLI.

## Deliverables

Data ingestion and cleaning pipeline, hub and network-level time series, a
multi-model rolling backtest, a monitoring system with alert logic, an Excel
report with live formulas, a precomputed static dashboard published on GitHub
Pages, clean public README.

## Data source

U.S. Bureau of Transportation Statistics T-100 Segment (All Carriers):
https://www.transtats.bts.gov/Fields.asp?gnoyr_VQ=FMF
Real monthly freight and mail volume by airline and route. The download
interface is manual/form-based; exact steps are documented in
docs/data_sources.md.

## Build steps (summary)

1. Folder setup + GitHub repo (public).
2. docs/PLAN.md, then PAUSE for approval.
3. Download T-100 data to data/raw (document exact steps + date range).
4. Profiling + docs/data_quality_report.md (numbered rules, never drop silently).
5. Aggregation: network-level + hub-level series, domestic/international split.
6. Rolling one-month-ahead backtest, >=3 methods, WAPE/MAPE, no leakage.
7. Structural break detection on domestic/international split, cross-referenced
   to public FedEx business events (data-derived vs externally-sourced kept
   distinct).
8. Monitoring system: rolling error normal-range, flag the break, report lead
   time vs a plain volume report.
9. pytest: no future-data leakage; methods match hand calcs; monitoring range
   math; planted-anomaly detection.
10. Code standards: Python 3.11+, pinned reqs, venv, type hints, docstrings,
    logging, config.yaml, ruff, Makefile targets.
11. Excel report with live formulas (Summary, Hub w/ dropdown, Break Analysis);
    zero formula errors after recalc.
12. Precomputed Plotly static dashboard -> docs_site/index.html; publish via
    GitHub Pages; record live URL.
13. Public README (strict content rules; no AI/tool mentions anywhere in repo).
14. Honesty rules: only list tools actually run; every number from script
    output; AI_USE.md logs bugs + anything unverified.
15. Final check: `make all` + tests clean; Pages live; numbers match; Excel zero
    errors; repo/history free of any AI attribution.

## Notes / spec clarifications

- The spec's root-folder sketch lists `PLAN.md` at the root, but Steps 2 and 13
  both refer to `docs/PLAN.md`; the plan lives at `docs/PLAN.md`.
- The Excel report (`outputs/FedEx_Forecasting_Report.xlsx`) is built locally and
  is git-ignored.
