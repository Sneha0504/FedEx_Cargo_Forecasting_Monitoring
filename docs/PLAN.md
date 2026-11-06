# PLAN — FedEx Cargo Forecasting & Monitoring

Internal planning document (not linked from the public README). This is the
design to approve **before** any pipeline code is written.

## 1. Goal and key questions

Forecast FedEx's monthly air cargo freight volume at the network level and at
its largest hub airports, using real BTS flight data; compare forecasting
methods on a rolling out-of-sample backtest; detect a real structural break in
the domestic/international series; and build a monitoring system that flags that
break in (near) real time rather than only in hindsight.

Key questions:
1. How have FedEx's freight volume and market share trended over the available
   years?
2. Which forecasting method wins a rolling one-month-ahead backtest, at the
   network level and per hub?
3. Is there a statistically identifiable break (domestic vs international), and
   can it be tied to a real, publicly documented FedEx business event?
4. Can a point-in-time monitoring system flag that break as it happens, and how
   much earlier than a plain monthly volume report?

## 2. Data and why it fits

- **Source:** U.S. Bureau of Transportation Statistics (BTS), *T-100 Segment
  (All Carriers)*. (The spec's `gnoyr_VQ=FMF` link is actually the T-100
  *Market* table; this project uses the *Segment* table because segment-level
  freight reflects what physically moved through each airport. Exact navigation
  is in `docs/data_sources.md`.)
- Real monthly freight and mail volume (pounds) by carrier and by
  origin→destination airport segment, with a domestic/international marker.
- Fits because it is (a) real federal data, (b) monthly with long history
  (good for seasonality and backtesting), (c) carrier-level so FedEx can be
  isolated and its market share computed, and (d) airport-level so hubs can be
  built and a domestic/international split derived.
- **Fields planned:** `Year`, `Month`, `UniqueCarrier`, `Carrier`,
  `CarrierName`, `Origin`, `Dest`, `Freight`, `Mail`, `Distance`, and
  **`DataSource`** (D = Domestic, I = International) — the confirmed
  domestic/international indicator used for the break analysis. (Pipeline
  normalizes header case/spelling defensively.)
- **Date range (confirmed):** **2011–2025** (15 full years), optionally 2026
  YTD; one zipped CSV per year, downloaded manually and placed in `data/raw/`.
  Exact obtained start/end recorded in `docs/data_sources.md` after download.
  **Caveat:** BTS withholds ~3 recent months of domestic data, so the newest
  month(s) may be partial (handled in Step 4, excluded from eval if incomplete).
- **FedEx identification:** BTS unique carrier code for Federal Express is
  expected to be `FX`; Step 4 checks for any alternate/legacy codes across the
  full period before filtering (handled as a data-quality rule, not assumed).

## 3. Pipeline / module layout

- `src/ingest.py` — load raw BTS CSV(s) from `data/raw/`, concatenate, basic
  schema normalization, write a single typed parquet/CSV for downstream use.
- `src/clean.py` — apply the numbered data-quality rules from Step 4; log every
  exclusion by count; never drop silently.
- `src/aggregate.py` — DuckDB SQL over the cleaned data to build the series
  (Section 4).
- `src/backtest.py` — rolling backtest of the models (Section 5).
- `src/break_detection.py` — structural break test on the domestic/intl split
  (Section 6).
- `src/monitoring.py` — point-in-time forecast-error monitoring (Section 7).
- `src/build_excel_report.py` — Excel report with live formulas (Section 8).
- `src/build_dashboard.py` — precomputed Plotly static dashboard (Section 9).
- Config in `config/config.yaml`; logging (not print); type hints + docstrings;
  ruff-clean. Processed series written to `data/processed/` (small, committed).

## 4. Aggregation (SQL via DuckDB)

Filter to FedEx freight operations (`FREIGHT`, FedEx carrier code), then build:

1. **Network-level monthly series:** total FedEx freight per month; plus total
   market freight per month and FedEx market share = FedEx / all-carrier
   freight.
2. **Hub-level monthly series:** rank airports by total historical FedEx freight
   (origin-based); take the top `N` (config `hubs.top_n`, default 5) as the
   qualifying hubs; document which airports qualify (expected: MEM, IND, OAK,
   ANC, and one more — confirmed from the data, not assumed). Monthly freight per
   hub.
3. **Domestic vs international split** at both network and hub level, using the
   BTS `DataSource` field (D/I) (needed for Step 6). This split is a first-class
   output.

Note on freight semantics: T-100 Segment freight is pounds carried on each
operated leg. Network total sums legs (a consistent monthly volume proxy, not a
unique-shipment count); hub freight is freight on segments originating at the
hub. Documented in `docs/methodology.md`.

Freight unit (pounds) is documented; series are written to `data/processed/`.

## 5. Rolling backtest (one-month-ahead)

Evaluate **at least three** methods for one-month-ahead volume:

- **M1 — Seasonal naive:** forecast = value 12 months earlier. Baseline.
- **M2 — Seasonal naive + trend adjustment:** seasonal naive plus a recent
  year-over-year trend term (e.g. average YoY change over a trailing window).
- **M3 — SARIMA** (statsmodels) with monthly seasonality.
- **M4 (stretch) — Gradient boosting** (scikit-learn) on lagged + seasonal +
  calendar features. Included if time allows; M1–M3 satisfy the requirement.

Design:
- Rolling / expanding window. For each month `t` in a held-out evaluation period
  (config `backtest.eval_months`, default 36), fit using **only** data strictly
  before `t`, forecast `t`, compare to actual. Require a minimum training
  history (`backtest.min_train_months`, default 48) before the first forecast.
- Run at network level and for **each** qualifying hub.
- **Metric:** WAPE primary (robust to zeros/scale), MAPE secondary, reported per
  method × level. Identify the best performer per level.
- **No leakage:** enforced in code (fit sees only `< t`) and tested directly in
  Step 9, not just by inspection.

## 6. Break detection (domestic/international)

- On the domestic and international network series, test for a structural break:
  Chow-style before/after test around candidate dates, and/or a supremum-Chow /
  cumulative-sum scan over candidate months to locate the most likely break.
- Candidate dates driven by inspecting the series (not pre-assumed).
- If a break is found, cross-reference its date against **publicly documented**
  FedEx business events (e.g. the end of FedEx's domestic air-cargo contract
  with the U.S. Postal Service, reported to conclude in 2024; Amazon-related
  volume changes; network consolidation). **Data-derived claims (break date,
  magnitude, significance) and externally-sourced context (the business event)
  are reported separately and labeled as such.** Anything not confirmable is
  stated as unconfirmed in `AI_USE.md`.

## 7. Monitoring system

- Use the best backtest model (Step 5). Walk forward month-by-month through the
  evaluation period as if in production.
- At each month compute forecast error; maintain a **rolling normal range** from
  a trailing window of prior errors (config `monitoring.error_window`, default
  12): flag when the error leaves mean ± `threshold_sigma` × std (default 3).
- Uses **only** information available up to each month (no hindsight).
- Report: does monitoring flag the Step-6 break, and **how many months earlier**
  than it would be obvious in a plain monthly volume report.

## 8. Excel report (`openpyxl`, live formulas)

`outputs/FedEx_Forecasting_Report.xlsx` (built locally, git-ignored):
- **Summary** tab: network volume trend, market-share trend, best model + its
  accuracy — as live formulas referencing data tabs.
- **Hub** tab: data-validation dropdown to pick a hub; its volume trend and
  backtest accuracy via live formulas.
- **Break Analysis** tab: domestic/intl split around the break date, with the
  monitoring flag date shown alongside.
- Verify zero formula errors after full recalculation. If no headless recalc
  tool is available in this environment, document honest manual spot-checks
  rather than claiming an automated recalc that did not run.

## 9. Static dashboard (Plotly → GitHub Pages)

`src/build_dashboard.py` writes `docs_site/index.html`: one self-contained,
precomputed Plotly page (no server, no view-time computation):
- Network volume + market-share trend.
- Hub selector (volume trend + backtest accuracy per hub).
- Break-analysis chart (domestic vs intl around the date) with the monitoring
  alert point marked.
- Model-comparison chart (accuracy per method).
Embedded data kept summarized. Confirm it opens as a local file. Publish via
GitHub Pages (main branch, `/docs_site`); record the live github.io URL.

## 10. Tests (pytest)

- Backtest uses **no** future data in any fitted model (asserted directly).
- Each forecasting method matches a hand-calculated value on a tiny synthetic
  series.
- Monitoring rolling normal-range math is correct on a constructed example.
- Monitoring flags a planted anomaly and does **not** flag normal variation.

## 11. Deliverables & Makefile

Makefile targets: `setup`, `aggregate`, `backtest`, `break`, `monitor`, `excel`,
`dashboard`, `lint`, `test`, `all`. Public README written last per the strict
content rules (no AI/tool mentions anywhere in the repo).

## 12. Risks and fallbacks

- **BTS download friction / size:** manual UI; range may be limited. Fallback:
  use a defined, documented multi-year window and state it plainly.
- **FedEx carrier-code changes over time:** handled as a data-quality rule;
  reconcile codes before filtering.
- **SARIMA fit instability / slowness per hub:** cap orders, add a safe
  fallback to M2 if a fit fails; log it.
- **No headless Excel recalc available:** document manual spot-verification
  honestly instead of claiming an automated check.
- **Break not statistically clear:** report that honestly; still show the
  before/after comparison and what the test did/didn't find.

## 13. Out of scope

- Multi-step / long-horizon forecasting (only one-month-ahead).
- Causal modeling of the business event (we correlate a data break with a
  publicly reported event; we do not claim proven causation).
- Carriers other than FedEx (used only to compute market share).
- Real-time/live data feeds or a hosted backend (dashboard is precomputed).

---

**STATUS: approved.** Parameters locked: 2011–2025 range, top-5 hubs, data
downloaded manually to `data/raw/`. Next: Step 3 onward once the raw files are
present.
