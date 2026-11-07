# Memo — FedEx cargo forecasting & monitoring

Internal findings memo. Every number here comes from the pipeline's script
output over the U.S. BTS T-100 Segment (All Carriers) data, 2011–2025 (180
complete months; the partial 2026 year was excluded). Data-derived results and
externally-sourced business context are kept separate and labeled.

## 1. Volume and market-share trend (data-derived)

- FedEx network air-cargo freight: **12.78 billion lbs (2011) → 10.20 billion
  lbs (2025), −20%**.
- FedEx share of all-carrier air freight: **~29.6% (2011 average) → ~20.1%
  (2025 average)**; latest month (Dec 2025) **19.7%**; full-period range
  **19.0%–33.2%**. The share has declined fairly steadily.

## 2. Hubs (data-derived)

Top five origin airports by total FedEx freight, 2011–2025:

| Rank | Hub | Total FedEx freight |
|---|---|---|
| 1 | MEM (Memphis) | 68.76B lbs |
| 2 | IND (Indianapolis) | 16.22B lbs |
| 3 | OAK (Oakland) | 7.34B lbs |
| 4 | ANC (Anchorage) | 6.76B lbs |
| 5 | LAX (Los Angeles) | 5.71B lbs |

Memphis is the dominant superhub — about 4× Indianapolis and more than the other
four hubs combined.

## 3. Forecasting backtest (data-derived)

Rolling, leakage-free, one-month-ahead backtest over a 36-month evaluation window
(2023–2025); fit uses only data strictly before each target month. Primary metric
WAPE.

| Level | Best method | WAPE | MAPE |
|---|---|---|---|
| Network | SARIMA | 5.5% | 5.5% |
| MEM | SARIMA | 6.2% | 6.4% |
| IND | Seasonal-naive + trend | 8.6% | 8.5% |
| OAK | Seasonal-naive + trend | 5.6% | 5.6% |
| ANC | SARIMA | 7.4% | 7.5% |
| LAX | SARIMA | 7.1% | 7.2% |

At the network level, SARIMA (5.5%) edged seasonal-naive+trend (5.7%) and clearly
beat plain seasonal-naive (12.2%). No single method wins everywhere, but SARIMA or
seasonal-naive+trend is best at every level.

## 4. Structural breaks (data-derived) + business context (externally sourced)

Breaks found by seasonally-adjusted binary-segmentation sup-Chow with a
moving-block bootstrap (block length 12; the conservative p-value used as the
acceptance gate). Confirmed breaks (p < 0.05):

| Series | Break | pct change | F | block p |
|---|---|---|---|---|
| International | 2020-05 | +28.7% | 94.4 | 0.009 |
| International | 2017-01 | +6.4% | 151.4 | 0.001 |
| International | 2018-01 | +4.9% | 15.6 | 0.004 |
| Domestic | 2021-04 | −10.7% | 279.6 | 0.001 |
| Domestic | 2019-06 | +3.1% | 12.9 | 0.028 |

- **Data-derived:** the two largest, cleanest breaks are the **+28.7%
  international jump in May 2020** and the **−10.7% domestic step-down in April
  2021**.
- **Externally-sourced context (not proven by this data):** the May 2020
  international surge is consistent with the COVID-era collapse of passenger
  belly capacity that pushed freight onto dedicated cargo carriers. The 2021
  domestic decline is consistent with post-surge normalization. These are
  plausible explanations from public reporting, not causal claims established
  here.

## 5. Monitoring (data-derived) + business context

The forecast-error monitor (best network model = SARIMA; trailing 12-month error
window; 3-sigma limits) run forward over 2023–2025 raised **exactly one alert, in
September 2024**. A plain 2-sigma year-over-year volume review first flagged the
same shift in **November 2024** — a **2-month lead** for the monitor (one
illustrative episode, n=1). At a matched 3-sigma the plain YoY review never flags
within the evaluation window, while the monitor still flags Sept 2024; the plain
2-sigma review also raised an earlier flag in April 2023 where the forecast-error
monitor did not.

- **Externally-sourced context:** September 2024 is the month FedEx's domestic
  air-cargo contract with the U.S. Postal Service **expired (Sept 29, 2024)**;
  UPS was named USPS's primary air cargo provider. USPS had been the largest
  customer of FedEx's air Express segment. (Public reporting; see
  `docs/data_sources.md` / project notes — this is context, not a causal claim.)
- **Honest limitation:** the Sept 2024 domestic drop (~−21% over its first 16
  months) is large and visible, but with only ~16 months of post-event data a
  conservative seasonality-aware break test does **not** certify it as a
  permanent structural break. The monitor flags it operationally as a
  regime-change candidate; statistical confirmation would need more data. This
  complementarity — fast operational flag vs slow conservative confirmation — is
  the point of the monitoring layer.

## 6. Data quality (data-derived)

From `docs/data_quality_report.md`, over 7,031,427 in-window rows: 0 missing and 0
negative freight values; FedEx appears under a single carrier code (`FX`) across
the whole span (no legacy codes to reconcile); 0 rows fail the
domestic/international classification; the partial 2026 year (332,658 rows) was
excluded as incomplete. Multiple rows per route-month are expected (T-100 splits
each segment by aircraft type/service class) and are summed, not dropped.
