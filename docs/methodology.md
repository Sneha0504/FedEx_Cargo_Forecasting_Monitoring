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
