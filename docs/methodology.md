# Methodology

Definitions implemented in the ingestion, cleaning, and aggregation stages.
Backtest, break-detection, and monitoring methodology are added as those stages
are built.

## Analysis window

Complete calendar years only: **2011–2025 (180 months)**. The partial 2026 tail
(January–July only, from the BTS ~3-month reporting lag) is excluded so every
series spans whole years and the recent months are not misread as a decline.
The window bounds live in `config/config.yaml` (`data.start_year`,
`data.end_year`) and are applied in SQL, not hard-coded.

## Source and grain

U.S. BTS **T-100 Segment (All Carriers)**: one record per operated nonstop
segment (origin→destination airport pair) per carrier per month, with freight
in **pounds**. The downloaded native key also splits by aircraft type/config
and service class, so a single route-month can span many rows; freight across
those sub-records is **summed** (see the data-quality report, rule 2).

## Aggregation definitions

- **FedEx:** `UNIQUE_CARRIER = 'FX'` ("Federal Express Corporation"), a single
  code across the whole span (`fedex_carrier_code` in config).
- **Domestic vs international:** the first character of `DATA_SOURCE`
  (`D` = domestic, `I` = international; values `DU`/`DF`/`IU`/`IF`).
- **Network freight:** sum of segment `FREIGHT` per month. This sums freight
  across operated legs — a consistent monthly volume proxy, **not** a count of
  unique shipments.
- **Market freight / FedEx market share:** `market_freight` is all-carrier
  segment freight per month; `fedex_market_share = fedex_freight /
  market_freight`.
- **Hub freight:** FedEx freight by **ORIGIN** airport. Hubs are the top
  `hubs.top_n` (default 5) airports ranked by **total FedEx origin freight over
  the full 2011–2025 window**. Selected hubs (from the real data): **MEM, IND,
  OAK, ANC, LAX**.
- **Domestic/international split** is produced at both the network and hub
  level and is a first-class output.

## Processed outputs (`data/processed/`)

Month is written as a first-of-month ISO date (`YYYY-MM-01`), sorted ascending.

| File | Grain | Columns |
|---|---|---|
| `network_monthly.csv` | month | `month, fedex_freight, market_freight, fedex_market_share` |
| `network_domestic_intl.csv` | month | `month, fedex_domestic_freight, fedex_intl_freight` |
| `hub_monthly.csv` | month × hub | `month, hub, fedex_freight` |
| `hub_domestic_intl.csv` | month × hub | `month, hub, domestic_freight, intl_freight` |
| `hubs_selected.csv` | hub | `hub, total_fedex_freight, rank` |

In `network_monthly.csv`, `fedex_freight` is intentionally the first data
column because the backtest loads that series and forecasts the first data
column (FedEx network freight).

## Rolling backtest (multi-level)

The one-month-ahead rolling backtest (`src/backtest.py`) runs the same
leakage-free expanding-window engine on **six levels**: the network total and
each of the five qualifying hubs (MEM, IND, OAK, ANC, LAX), one series per hub
from `hub_monthly.csv`. For each level, every target month in the evaluation
window (`backtest.eval_months`, 36 → 2023-01..2025-12) is forecast from only
the history strictly before it, with a minimum of `backtest.min_train_months`
(48) months of history. The three methods are M1 seasonal-naive, M2
seasonal-naive + trend, and M3 SARIMA (with the documented fallbacks).

Two regenerable outputs are written to `data/processed/`:

| File | Grain | Columns |
|---|---|---|
| `backtest_accuracy.csv` | level × method | `level, method, wape, mape` |
| `backtest_forecasts.csv` | level × month × method | `level, month, method, forecast, actual` |

`level` is `network` or a hub code. WAPE is primary, MAPE secondary; the
WAPE-winning method per level is logged. The network-only forecasts are also
written to `backtest_results.csv` for continuity with the earlier single-level
output.

## Structural-break detection (domestic / international)

`src/break_detection.py` tests for **multiple** breaks in the **mean level** of
each network series separately — FedEx domestic and international freight
(`network_domestic_intl.csv`) — using **binary segmentation** (recursive
supremum-Chow) on the **seasonally-adjusted** series.

**Seasonal adjustment first.** Freight is strongly seasonal, and a naïve test
would mistake seasonal swings for structural shifts (and a naïve i.i.d.
residual bootstrap would understate p-values badly). So before any break test
the series is seasonally adjusted: a single full-window OLS of
`freight ~ 1 + t + D_feb + … + D_dec` (a linear trend plus 11 monthly dummies,
January as baseline) is fit; the estimated monthly effects are mean-centered
across the twelve calendar months and subtracted, removing **only** the
seasonal component while keeping the level and trend. The break scan then runs
on this adjusted series. **Caveat:** the seasonal factors are estimated over
the full window, so a late break lightly contaminates them (the adjustment is
not re-estimated per segment).

**Within a segment.** For each candidate break month in the central portion of
the segment (the first/last `break.trim_fraction`, default 0.15, of months are
trimmed, per Andrews), the segment is split into two regimes and a Chow
F-statistic is computed for a break in a **level + linear-trend** regression
(`y ~ 1 + t`, `k = 2` parameters per regime — kept at 2 so a short post-break
segment, e.g. ~16 months, still has adequate degrees of freedom):

```
F(i) = [ (RSS_pooled − RSS_split) / k ] / [ RSS_split / (m − 2k) ]
```

where `RSS_split = RSS_regime1 + RSS_regime2` and `m` is the segment length.
The month with the maximum F (the **sup-F**) is the segment's candidate break.

**Significance — two residual bootstraps, conservative one gates.** Under the
no-break null, responses are regenerated as the pooled (single-regime) fit plus
resampled seasonally-adjusted residuals, and the full sup-F scan is re-run on
each replicate (`break.bootstrap_reps`, default 1000, seeded per segment for
reproducibility); each p-value is `(1 + #{sup-F* ≥ sup-F_obs}) / (reps + 1)`.
Two resampling schemes are reported:

- **`p_bootstrap_iid`** — residuals resampled independently. Appropriate only if
  residuals are serially uncorrelated; on real freight they are not, so this
  figure is anti-conservative (too small) and is reported for comparison only.
- **`p_bootstrap_block`** — a **moving-block** bootstrap (block length
  `break.block_length`, default 12 months) that preserves short-range
  autocorrelation. This is the conservative, honest figure and is the
  **acceptance gate** (a break is kept only while `p_bootstrap_block <
  break.significance_level`, default 0.05).

The classical `F(k, m − 2k)` asymptotic is **not** used: the break date is
*estimated* (the supremum is taken over many candidate dates) so the sup-F has
a non-standard distribution (Andrews, 1993) under which it would overstate
significance.

**Binary segmentation.** The globally most significant break (largest sup-F) is
accepted first if it passes the block gate; the procedure then recurses into
the sub-segments on either side of each accepted break. Recursion stops when a
segment is shorter than `break.min_segment_months` (default 24 — a *stopping*
rule, so an accepted break may still leave a shorter leaf that is not split
further), when no candidate break passes the block gate, or when
`break.max_breaks` (default 3) breaks have been found.

For each accepted break, `f_stat` and both p-values come from the
seasonally-adjusted series, while `pre_mean`/`post_mean` are the mean freight
levels before and from the break on the **original** series (segment-local) so
`pct_change = (post − pre) / pre × 100` stays interpretable as real freight.
Results are written to `data/processed/break_analysis.csv` with **one row per
detected break** (`series, break_month, f_stat, p_bootstrap_iid,
p_bootstrap_block, pre_mean, post_mean, pct_change`), sorted by series then
break month. All figures are purely data-derived; no business-event
interpretation of a break is produced by the code or its output.

Note that the moving-block gate is materially stricter than an i.i.d. bootstrap
on these autocorrelated series: breaks whose i.i.d. p-value is ~0.001 can carry
a block p-value well above 0.05 and are then (correctly) not accepted. Which
dates pass is entirely data-driven; the monitoring narrative (below) anchors on
the **latest** block-significant domestic break, whatever it is.

## Point-in-time monitoring and lead time

`src/monitoring.py` applies the rolling-error monitor to the **best network
model** — the lowest-WAPE network method from `backtest_accuracy.csv` — over
its one-step-ahead errors across the evaluation window. At each month the
normal range is `mean ± threshold_sigma × std` of the prior
`monitoring.error_window` (12) errors (strictly earlier months only; default
`threshold_sigma` = 3). The per-month series (`month, error, roll_mean,
roll_std, lower, upper, flagged`) is written to `data/processed/monitoring.csv`.

**Two complementary instruments.** The forecast-error monitor is a **fast
operational detector**: it flags a regime-change candidate as it happens, from
one month's forecast error. The Step-7 structural-break test is a
**conservative confirmer**: with a seasonality-aware moving-block bootstrap it
only certifies a shift once enough post-break data has accrued (years). They
answer different questions, so Step 8 is anchored on the **monitor's own
flagged anomaly**, not on the Step-7 structural anchor.

Concretely, the break test certifies the domestic 2021-04 and international
2020-05 shifts (among others) — all of which **precede** the live-monitoring
window `2023-01..2025-12`, so the monitor could not have flagged them in real
time. Within that window the monitor flags **2024-09**, a large visible
domestic drop (~−21% over the 16 post-months vs the prior 16). That month is
**not** called a structural break here: the conservative test does not certify
it on only ~16 post-months (~one seasonal cycle). It is the monitor's flagged
anomaly / regime-change candidate.

**Lead-time episode (anchor = the monitor's anomaly).** `monitor_event` is the
monitor's first flagged month in the evaluation window (2024-09).

**Plain-report baseline (exact definition).** A routine monthly review of the
*same* network volume series the monitor watches, using a standard control
limit on the 12-month year-over-year change rather than a forecast model:

```
yoy[t]     = freight[t] / freight[t−12] − 1
mean[t]    = mean of the window (monitoring.error_window = 12) yoy values strictly before t
std[t]     = sample std (ddof=1) of those same prior yoy values
lower[t]   = mean[t] − monitoring.plain_report_sigma × std[t]
flagged[t] = yoy[t] < lower[t]
```

`monitoring.plain_report_sigma` defaults to 2.0 — a conventional 2σ lower
control limit. Only prior-to-`t` statistics are used, so the review is
point-in-time (no hindsight), exactly like the forecast-error monitor. The
first flagged month on or after `monitor_event` is the plain-report detection
(2024-11).

**Lead time** `= plain-report month − monitor_event` (whole months);
**positive means the monitor is earlier**. Here that is **+2 months** (monitor
2024-09, plain review 2024-11). This is **one illustrative episode (n = 1), not
an average** — a single anomaly on a single series, reported for context, not a
performance estimate. The real computed numbers are logged; the figure is
reported honestly whatever its sign.

**Within-window context (reported honestly).** Two further facts are logged
because they bear on how the two instruments actually compare across the
`2023-01..2025-12` evaluation window:

- A matched-sensitivity line: at a 3σ limit equal to the monitor's own
  `threshold_sigma`, the plain YoY review never breaches **within the
  2023-01..2025-12 evaluation window**, whereas the forecast-error monitor does
  flag — i.e. at matched sensitivity the plain review is the cruder instrument.
- The full flag sets within the window are logged. In particular the 2σ plain
  YoY review also fires at **2023-04** (and later months), where the
  forecast-error monitor raises **no** flag — an earlier plain-review trigger
  that the forecast-error monitor does not share. This cuts against a simple
  "monitor is always earlier" reading and is reported rather than hidden.

Finally, the monitor's 2024-09 anomaly has only **16 months** of post-anomaly
data to end-2025 (~one seasonal cycle), which is part of why the conservative
moving-block gate on the seasonally-adjusted series does not (yet) certify it as
a structural break — exactly the fast-detector / conservative-confirmer split
above.
