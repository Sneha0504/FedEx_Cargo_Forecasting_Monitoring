"""Aggregation of cleaned BTS segment data into small, committed series.

All aggregation is DuckDB SQL over the raw view registered by
:mod:`src.ingest`, restricted to the cleaned, complete-year analysis window
(see :func:`src.ingest.clean_predicate`: in-window years with valid freight and
a D/I DATA_SOURCE). The module writes five compact CSVs to ``data/processed/``
(month as a first-of-month ISO date ``YYYY-MM-01``, sorted):

* ``network_monthly.csv`` — FedEx vs market freight and FedEx market share.
  ``fedex_freight`` is deliberately the first data column so the backtest's
  loader (which forecasts the first data column) targets FedEx network freight.
* ``network_domestic_intl.csv`` — FedEx domestic vs international freight.
* ``hub_monthly.csv`` — FedEx freight by top-N origin hub (long format).
* ``hub_domestic_intl.csv`` — FedEx domestic vs international freight per hub.
* ``hubs_selected.csv`` — the chosen hubs with their total freight and rank.

Definitions are recorded in ``docs/methodology.md``.
"""

from __future__ import annotations

import logging
from pathlib import Path

import duckdb
import pandas as pd

from src.config import Config, get_config, setup_logging
from src.ingest import RAW_VIEW, build_connection, clean_predicate

logger = logging.getLogger(__name__)

# Temp table holding the chosen hubs for joins within a single connection.
_SELECTED_HUBS = "selected_hubs"

# Output filenames (the network series name comes from config so the backtest
# and this writer stay in sync).
DOMESTIC_INTL_FILE = "network_domestic_intl.csv"
HUB_MONTHLY_FILE = "hub_monthly.csv"
HUB_DOMESTIC_INTL_FILE = "hub_domestic_intl.csv"
HUBS_SELECTED_FILE = "hubs_selected.csv"


def network_monthly(con: duckdb.DuckDBPyConnection, config: Config) -> pd.DataFrame:
    """Monthly FedEx freight, total market freight, and FedEx market share.

    ``fedex_freight`` is the first data column (backtest forecast target);
    ``market_freight`` sums all carriers; ``fedex_market_share`` is their ratio.
    """
    where_clause = clean_predicate(config)
    return con.execute(
        f"""
        SELECT
            strftime(make_date(YEAR, MONTH, 1), '%Y-%m-%d') AS month,
            SUM(CASE WHEN UNIQUE_CARRIER = ? THEN FREIGHT ELSE 0 END) AS fedex_freight,
            SUM(FREIGHT) AS market_freight,
            SUM(CASE WHEN UNIQUE_CARRIER = ? THEN FREIGHT ELSE 0 END)
                / NULLIF(SUM(FREIGHT), 0) AS fedex_market_share
        FROM {RAW_VIEW}
        WHERE {where_clause}
        GROUP BY YEAR, MONTH
        ORDER BY YEAR, MONTH
        """,
        [config.fedex_carrier_code, config.fedex_carrier_code],
    ).df()


def network_domestic_intl(
    con: duckdb.DuckDBPyConnection, config: Config
) -> pd.DataFrame:
    """Monthly FedEx domestic vs international freight (DATA_SOURCE first char)."""
    where_clause = clean_predicate(config)
    return con.execute(
        f"""
        SELECT
            strftime(make_date(YEAR, MONTH, 1), '%Y-%m-%d') AS month,
            SUM(CASE WHEN SUBSTR(DATA_SOURCE, 1, 1) = 'D' THEN FREIGHT ELSE 0 END)
                AS fedex_domestic_freight,
            SUM(CASE WHEN SUBSTR(DATA_SOURCE, 1, 1) = 'I' THEN FREIGHT ELSE 0 END)
                AS fedex_intl_freight
        FROM {RAW_VIEW}
        WHERE {where_clause} AND UNIQUE_CARRIER = ?
        GROUP BY YEAR, MONTH
        ORDER BY YEAR, MONTH
        """,
        [config.fedex_carrier_code],
    ).df()


def select_hubs(con: duckdb.DuckDBPyConnection, config: Config) -> pd.DataFrame:
    """Choose the top-N FedEx hubs by total origin freight over the window.

    Creates the :data:`_SELECTED_HUBS` temp table on ``con`` (so later queries
    can join on it) and returns ``hub, total_fedex_freight, rank``.
    """
    where_clause = clean_predicate(config)
    con.execute(f"DROP TABLE IF EXISTS {_SELECTED_HUBS}")
    con.execute(
        f"""
        CREATE TEMP TABLE {_SELECTED_HUBS} AS
        SELECT
            ORIGIN AS hub,
            SUM(FREIGHT) AS total_fedex_freight,
            ROW_NUMBER() OVER (ORDER BY SUM(FREIGHT) DESC) AS rank
        FROM {RAW_VIEW}
        WHERE {where_clause} AND UNIQUE_CARRIER = ?
        GROUP BY ORIGIN
        ORDER BY total_fedex_freight DESC
        LIMIT ?
        """,
        [config.fedex_carrier_code, config.hubs.top_n],
    )
    return con.execute(
        f"SELECT hub, total_fedex_freight, rank FROM {_SELECTED_HUBS} ORDER BY rank"
    ).df()


def hub_monthly(con: duckdb.DuckDBPyConnection, config: Config) -> pd.DataFrame:
    """Monthly FedEx freight per selected hub (long format, complete spine).

    A month x hub spine with ``COALESCE(..., 0)`` guarantees a value for every
    month in the window and every chosen hub, so no month is missing for a hub
    that happened to carry no freight that month. Requires :func:`select_hubs`
    to have populated the temp table on ``con``.
    """
    where_clause = clean_predicate(config)
    return con.execute(
        f"""
        WITH months AS (
            SELECT DISTINCT make_date(YEAR, MONTH, 1) AS m
            FROM {RAW_VIEW} WHERE {where_clause}
        ),
        spine AS (
            SELECT months.m AS m, h.hub AS hub
            FROM months CROSS JOIN {_SELECTED_HUBS} h
        ),
        fx AS (
            SELECT make_date(YEAR, MONTH, 1) AS m, ORIGIN AS hub,
                   SUM(FREIGHT) AS fedex_freight
            FROM {RAW_VIEW}
            WHERE {where_clause} AND UNIQUE_CARRIER = ?
              AND ORIGIN IN (SELECT hub FROM {_SELECTED_HUBS})
            GROUP BY 1, 2
        )
        SELECT
            strftime(spine.m, '%Y-%m-%d') AS month,
            spine.hub AS hub,
            COALESCE(fx.fedex_freight, 0) AS fedex_freight
        FROM spine LEFT JOIN fx USING (m, hub)
        ORDER BY spine.m, spine.hub
        """,
        [config.fedex_carrier_code],
    ).df()


def hub_domestic_intl(
    con: duckdb.DuckDBPyConnection, config: Config
) -> pd.DataFrame:
    """Monthly FedEx domestic vs international freight per selected hub (long)."""
    where_clause = clean_predicate(config)
    return con.execute(
        f"""
        WITH months AS (
            SELECT DISTINCT make_date(YEAR, MONTH, 1) AS m
            FROM {RAW_VIEW} WHERE {where_clause}
        ),
        spine AS (
            SELECT months.m AS m, h.hub AS hub
            FROM months CROSS JOIN {_SELECTED_HUBS} h
        ),
        fx AS (
            SELECT make_date(YEAR, MONTH, 1) AS m, ORIGIN AS hub,
                SUM(CASE WHEN SUBSTR(DATA_SOURCE, 1, 1) = 'D' THEN FREIGHT ELSE 0 END)
                    AS domestic_freight,
                SUM(CASE WHEN SUBSTR(DATA_SOURCE, 1, 1) = 'I' THEN FREIGHT ELSE 0 END)
                    AS intl_freight
            FROM {RAW_VIEW}
            WHERE {where_clause} AND UNIQUE_CARRIER = ?
              AND ORIGIN IN (SELECT hub FROM {_SELECTED_HUBS})
            GROUP BY 1, 2
        )
        SELECT
            strftime(spine.m, '%Y-%m-%d') AS month,
            spine.hub AS hub,
            COALESCE(fx.domestic_freight, 0) AS domestic_freight,
            COALESCE(fx.intl_freight, 0) AS intl_freight
        FROM spine LEFT JOIN fx USING (m, hub)
        ORDER BY spine.m, spine.hub
        """,
        [config.fedex_carrier_code],
    ).df()


def _write(df: pd.DataFrame, path: Path) -> None:
    """Write a DataFrame to CSV without an index and log the row count."""
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)
    logger.info("Wrote %s (%d rows).", path, len(df))


def main() -> None:
    """Run the full aggregation and write all processed CSVs."""
    setup_logging()
    config = get_config()
    con = build_connection(config)
    processed_dir = Path(config.data.processed_dir)

    hubs = select_hubs(con, config)
    logger.info(
        "Selected top-%d FedEx hubs by total origin freight (%d-%d):\n%s",
        config.hubs.top_n,
        config.data.start_year,
        config.data.end_year,
        hubs.to_string(index=False),
    )

    net = network_monthly(con, config)
    net_di = network_domestic_intl(con, config)
    hub_m = hub_monthly(con, config)
    hub_di = hub_domestic_intl(con, config)

    _write(net, processed_dir / config.data.network_series_file)
    _write(net_di, processed_dir / DOMESTIC_INTL_FILE)
    _write(hub_m, processed_dir / HUB_MONTHLY_FILE)
    _write(hub_di, processed_dir / HUB_DOMESTIC_INTL_FILE)
    _write(hubs, processed_dir / HUBS_SELECTED_FILE)

    logger.info(
        "Network series spans %s .. %s (%d months). FedEx market share "
        "min=%.4f max=%.4f.",
        net["month"].iloc[0],
        net["month"].iloc[-1],
        len(net),
        net["fedex_market_share"].min(),
        net["fedex_market_share"].max(),
    )


if __name__ == "__main__":
    main()
