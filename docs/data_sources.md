# Data sources

## Dataset

**U.S. Bureau of Transportation Statistics (BTS) — T-100 Segment (All Carriers)**

- Field definitions / overview: https://www.transtats.bts.gov/Fields.asp?gnoyr_VQ=FMF
  (Note: the `gnoyr_VQ=FMF` page is actually titled *T-100 **Market*** (All
  Carriers). This project uses the *T-100 **Segment*** (All Carriers) table —
  segment-level data reflects freight physically carried on each operated flight
  leg, which is what we want for airport/hub throughput. Reach it via the steps
  below and confirm the page heading reads **"T-100 Segment (All Carriers)"**.)
- What it is: monthly, by operated nonstop segment (origin→destination airport
  pair) and carrier — departures, seats, passengers, **freight (pounds)**, mail,
  aircraft type, and a domestic/international flag.
- Usage terms: U.S. Government public-domain data, free to use (BTS/RITA). No
  login required.

## How to download (manual — the BTS form cannot be fully scripted)

This environment is non-interactive, so these steps are run manually in a
browser; the downloaded CSVs are placed untouched in `data/raw/`.

1. Go to **https://www.transtats.bts.gov/** → **Data Finder** → **By Mode:
   Aviation** → **Air Carriers**, then open the **Data Tables** list for Air
   Carriers (direct: https://www.transtats.bts.gov/tables.asp?QO_VQ=EEE&QO_anzr=Nv4 ).
2. Find **"T-100 Segment (All Carriers)"** in the list and click its
   **Download** link. Confirm the resulting page's heading says **T-100 Segment
   (All Carriers)** (not *Market*).
3. **Select fields** (check these boxes; or use "Select all fields" to be safe):
   - Summaries: **Freight**, **Mail**, Passengers, Distance, DeparturesPerformed
     (Payload/Seats optional)
   - Carrier: **UniqueCarrier**, **Carrier**, **CarrierName** (AirlineID optional)
   - Origin: **Origin**, OriginCityName, OriginState, OriginCountry
   - Destination: **Dest**, DestCityName, DestState, DestCountry
   - Time Period: **Year**, **Month** (Quarter optional)
   - Other: **DataSource** (D = Domestic, I = International) — **required** for
     the break analysis; Class (service class) optional
4. **Set the filters**:
   - **Filter Geography** = **All**
   - **Filter Period** = **All** (returns all 12 months of the selected year)
   - **Filter Year** = one year at a time.
5. Click **Download** (a "Prezipped File" option bundles all selected fields).
   You get one **zipped CSV per year**.
6. **Repeat per year** across the chosen range (below), then unzip every CSV
   **untouched** into `data/raw/`.

## Date range to download

Target range for this build: **2011 through 2025** (15 full years). Optionally
add **2026 year-to-date**.

- Rationale: long enough for stable seasonality and a multi-year rolling
  backtest, and it spans the candidate 2024 structural break (end of FedEx's
  domestic air-cargo contract with the USPS) with years of history on both
  sides.
- **Reporting-lag caveat:** BTS withholds the most recent ~3 months of U.S.
  domestic data so the combined database ends on a uniform date. The latest
  month(s) in the newest file may therefore be incomplete; this is noted and
  handled in the data-quality step and excluded from the evaluation window if
  partial.

## To be recorded after download (fill in once files are in `data/raw/`)

- [ ] Exact file names saved to `data/raw/`
- [ ] Row count per file and total
- [ ] Actual earliest and latest YEAR-MONTH present
- [ ] Confirmed FedEx `UniqueCarrier` code(s) found (expected `FX`; note any
      legacy/alternate codes)
- [ ] The exact column header names as they appear in the CSV (so the pipeline
      maps them correctly)
