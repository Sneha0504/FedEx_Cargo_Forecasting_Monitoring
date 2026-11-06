"""Hand-calculated checks for the aggregation layer.

A tiny synthetic segment table (nine rows, no real data) is registered as the
``raw_segments`` view on an in-memory DuckDB connection and the aggregation
functions in :mod:`src.aggregate` are driven over it. Every expected value is
worked out by hand below, so the assertions are exact and deterministic.

The fixture is built so that it exercises each behaviour the processed CSVs
depend on:

* years 2019 and 2022 sit outside the configured 2020-2021 window (dropped);
* carrier ``5X`` (non-FedEx) contributes to market freight but never to FedEx;
* both a ``D*`` and an ``I*`` DATA_SOURCE appear, for the domestic/intl split;
* origins MEM / IND / OAK let the top-N hub ranking be checked.
"""

from __future__ import annotations

import duckdb
import pandas as pd
import pytest

from src.aggregate import (
    hub_monthly,
    network_domestic_intl,
    network_monthly,
    select_hubs,
)
from src.config import (
    BacktestConfig,
    Config,
    DataConfig,
    ForecastingConfig,
    HubsConfig,
    MonitoringConfig,
    SarimaConfig,
)
from src.ingest import RAW_VIEW

# Synthetic segment rows: (YEAR, MONTH, UNIQUE_CARRIER, ORIGIN, DATA_SOURCE,
# FREIGHT). Window is 2020-2021 (see _config); 2019 and 2022 are out-of-window.
_ROWS = [
    # --- in-window FedEx (FX) ------------------------------------------------
    (2020, 1, "FX", "MEM", "DU", 100.0),  # MEM domestic
    (2020, 1, "FX", "MEM", "IU", 50.0),   # MEM international
    (2020, 1, "FX", "IND", "DU", 30.0),   # IND domestic
    (2020, 2, "FX", "MEM", "DU", 200.0),  # MEM domestic, month 2
    (2020, 2, "FX", "OAK", "IU", 10.0),   # OAK international, month 2
    # --- in-window non-FedEx (5X): market freight only -----------------------
    (2020, 1, "5X", "MEM", "DU", 400.0),
    (2020, 2, "5X", "IND", "DU", 100.0),
    # --- out-of-window FedEx: must be dropped by the window filter ------------
    (2019, 12, "FX", "MEM", "DU", 9999.0),
    (2022, 1, "FX", "MEM", "DU", 8888.0),
]


def _config() -> Config:
    """A minimal Config: window 2020-2021, FedEx = FX, top-2 hubs."""
    return Config(
        data=DataConfig(
            raw_dir="",
            raw_glob="",
            processed_dir="",
            network_series_file="network_monthly.csv",
            backtest_results_file="backtest_results.csv",
            start_year=2020,
            end_year=2021,
        ),
        fedex_carrier_code="FX",
        hubs=HubsConfig(top_n=2),
        forecasting=ForecastingConfig(
            trend_window=3,
            sarima=SarimaConfig(order=(1, 1, 1), seasonal_order=(0, 1, 1, 12)),
        ),
        backtest=BacktestConfig(eval_months=36, min_train_months=48),
        monitoring=MonitoringConfig(error_window=12, threshold_sigma=3.0),
    )


@pytest.fixture
def con() -> duckdb.DuckDBPyConnection:
    """In-memory DuckDB connection with the synthetic ``raw_segments`` view."""
    frame = pd.DataFrame(
        _ROWS,
        columns=["YEAR", "MONTH", "UNIQUE_CARRIER", "ORIGIN", "DATA_SOURCE", "FREIGHT"],
    )
    connection = duckdb.connect()
    connection.register(RAW_VIEW, frame)
    return connection


def test_network_monthly_window_fedex_and_share(con: duckdb.DuckDBPyConnection) -> None:
    """network_monthly: in-window only, FX-only FedEx, and the share ratio.

    In-window months and totals (2019-12 and 2022-01 are dropped):

      2020-01  FedEx = 100 + 50 + 30 = 180
               market = 180 + 400 (5X) = 580
               share  = 180 / 580 = 0.3103448...
      2020-02  FedEx = 200 + 10 = 210
               market = 210 + 100 (5X) = 310
               share  = 210 / 310 = 0.6774193...
    """
    net = network_monthly(con, _config())

    # (a) Only the two in-window months survive; 2019 and 2022 are gone.
    assert list(net["month"]) == ["2020-01-01", "2020-02-01"]

    # (b) Only FX counts as FedEx (5X's 400 / 100 are excluded from fedex_freight
    # but included in market_freight).
    assert net.loc[0, "fedex_freight"] == pytest.approx(180.0)
    assert net.loc[1, "fedex_freight"] == pytest.approx(210.0)
    assert net.loc[0, "market_freight"] == pytest.approx(580.0)
    assert net.loc[1, "market_freight"] == pytest.approx(310.0)

    # (d) fedex_market_share == fedex_freight / market_freight.
    assert net.loc[0, "fedex_market_share"] == pytest.approx(180.0 / 580.0)
    assert net.loc[1, "fedex_market_share"] == pytest.approx(210.0 / 310.0)


def test_network_domestic_intl_sums_to_fedex(con: duckdb.DuckDBPyConnection) -> None:
    """network_domestic_intl: D/I split, and (c) domestic + intl == FedEx total.

      2020-01  domestic = 100 + 30 = 130 ; intl = 50  -> 180
      2020-02  domestic = 200          ; intl = 10  -> 210
    """
    cfg = _config()
    di = network_domestic_intl(con, cfg)
    net = network_monthly(con, cfg)

    assert list(di["month"]) == ["2020-01-01", "2020-02-01"]
    assert di.loc[0, "fedex_domestic_freight"] == pytest.approx(130.0)
    assert di.loc[0, "fedex_intl_freight"] == pytest.approx(50.0)
    assert di.loc[1, "fedex_domestic_freight"] == pytest.approx(200.0)
    assert di.loc[1, "fedex_intl_freight"] == pytest.approx(10.0)

    # (c) domestic + intl equals the FedEx network total for each month.
    combined = di["fedex_domestic_freight"] + di["fedex_intl_freight"]
    assert combined.iloc[0] == pytest.approx(net.loc[0, "fedex_freight"])
    assert combined.iloc[1] == pytest.approx(net.loc[1, "fedex_freight"])


def test_hub_ranking_and_monthly(con: duckdb.DuckDBPyConnection) -> None:
    """select_hubs / hub_monthly: (e) top-N origins by FedEx freight.

    Total FedEx origin freight over the window:
      MEM = 100 + 50 + 200 = 350  (rank 1)
      IND = 30                     (rank 2)
      OAK = 10                     (dropped: top_n = 2)
    """
    cfg = _config()
    hubs = select_hubs(con, cfg)

    # (e) top-2 hubs by FedEx freight are MEM then IND; OAK is excluded.
    assert list(hubs["hub"]) == ["MEM", "IND"]
    assert list(hubs["rank"]) == [1, 2]
    assert hubs.loc[0, "total_fedex_freight"] == pytest.approx(350.0)
    assert hubs.loc[1, "total_fedex_freight"] == pytest.approx(30.0)
    assert "OAK" not in set(hubs["hub"])

    # hub_monthly fills a full month x hub spine (IND has no Feb FedEx -> 0).
    hm = hub_monthly(con, cfg)
    by_key = {(row.month, row.hub): row.fedex_freight for row in hm.itertuples()}
    assert by_key[("2020-01-01", "MEM")] == pytest.approx(150.0)  # 100 + 50
    assert by_key[("2020-01-01", "IND")] == pytest.approx(30.0)
    assert by_key[("2020-02-01", "MEM")] == pytest.approx(200.0)
    assert by_key[("2020-02-01", "IND")] == pytest.approx(0.0)  # spine zero-fill
