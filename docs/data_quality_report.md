# Data quality report

Numbered data-quality rules applied to the BTS T-100 Segment (All Carriers) data before aggregation. Every count below is produced by `src/clean.py` against the real downloaded files; none are invented. No rows are dropped silently — each exclusion is logged with its count.

_Counts generated 2026-11-06 from `data/raw/seg_*.csv`._

## Rule 1: Missing or negative FREIGHT

- **Severity:** High
- **Handling:** Freight is a non-negative weight in pounds, so rows with a null or negative FREIGHT are excluded from the cleaned set before aggregation (see src.ingest.clean_predicate). None were found, so no rows are dropped.
- **Counts found:**
  - `null_freight_rows`: 0
  - `negative_freight_rows`: 0
  - `in_window_rows_checked`: 7031427

## Rule 2: Multiple records at (ORIGIN, DEST, UNIQUE_CARRIER, YEAR, MONTH, DATA_SOURCE) grain

- **Severity:** Informational
- **Handling:** Expected, not a defect: T-100 Segment splits each segment by aircraft type/config and service class (fields not downloaded), so one route-month can span many rows (verified: they carry different FREIGHT/DEPARTURES values). The aggregation SUMS FREIGHT by month (and by origin for hubs), which is the correct treatment of these additive sub-records; no rows are dropped or collapsed. Rows identical on every downloaded column are also counted; summing them yields the same correct total freight.
- **Counts found:**
  - `multi_row_grain_groups`: 1548558
  - `extra_rows_beyond_first_at_grain`: 2632362
  - `all_column_identical_groups`: 22499
  - `all_column_identical_extra_rows`: 24832

## Rule 3: FedEx carrier-code consistency across years

- **Severity:** High
- **Handling:** FedEx is identified by UNIQUE_CARRIER = 'FX'. Any Federal Express row under a different/legacy code would need reconciling before filtering. Only the single code was found, so filtering on it is safe.
- **Counts found:**
  - `fedex_codes`: {'FX': {'year_min': 2011, 'year_max': 2026, 'rows': 278842}}
  - `unexpected_codes`: []
  - `single_code_confirmed`: True

## Rule 4: FedEx routes with gaps in monthly history

- **Severity:** Informational
- **Handling:** Route-level intermittency is expected (FedEx adds/drops origin-destination legs over time), so gaps are reported, not dropped. The network and hub series aggregate across routes, so individual route gaps do not create holes in those monthly series.
- **Counts found:**
  - `fedex_routes`: 6386
  - `routes_with_month_gaps`: 3321

## Rule 5: Domestic/international classification consistency

- **Severity:** High
- **Handling:** Domestic vs international is derived from the first character of DATA_SOURCE (D or I). Any row whose first character is neither is flagged and excluded from the cleaned set used for aggregation (see src.ingest.clean_predicate). None were found.
- **Counts found:**
  - `data_source_counts`: {'DU': 5679578, 'IU': 754355, 'IF': 552727, 'DF': 44767}
  - `rows_failing_D_or_I`: 0

## Rule 6: Exclude years outside the analysis window

- **Severity:** High
- **Handling:** Only complete calendar years inside the analysis window 2011-2025 are aggregated. Rows with YEAR < 2011 or YEAR > 2025 are excluded: 332658 row(s) fall outside the window [2026 (7 month(s), 332658 rows)]. The newest tail is a partial year (BTS withholds ~3 recent months of domestic data), so it is not complete; the window bounds are config-driven (data.start_year / data.end_year).
- **Counts found:**
  - `rows_excluded_outside_window`: 332658
  - `years_outside_window`: {2026: {'months_available': 7, 'rows_excluded': 332658}}
  - `analysis_window`: 2011-2025

