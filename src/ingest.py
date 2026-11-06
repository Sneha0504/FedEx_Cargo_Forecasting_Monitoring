"""Raw BTS T-100 Segment ingestion via DuckDB.

Registers every per-year ``data/raw/seg_*.csv`` file as a single DuckDB view
using :func:`read_csv` with a proper CSV parser (the files contain quoted
fields with embedded commas, so naive comma splitting must never be used).
The module validates that the expected columns are present, logs the total row
count and the distinct ``YEAR`` range, and exposes helpers that downstream
cleaning and aggregation steps build on.

No large intermediate files are written: the raw CSVs are read directly by
DuckDB each run.
"""

from __future__ import annotations

import logging

import duckdb

from src.config import Config, get_config, setup_logging

logger = logging.getLogger(__name__)

# Name of the DuckDB view registered over the full raw glob (all years).
RAW_VIEW = "raw_segments"

# The columns the pipeline relies on, exactly as they appear in the BTS files
# (UPPER_SNAKE). Validated on load so a changed export fails loudly rather than
# producing silently wrong aggregates.
EXPECTED_COLUMNS: tuple[str, ...] = (
    "DEPARTURES_PERFORMED",
    "PASSENGERS",
    "FREIGHT",
    "MAIL",
    "DISTANCE",
    "UNIQUE_CARRIER",
    "UNIQUE_CARRIER_NAME",
    "CARRIER",
    "CARRIER_NAME",
    "ORIGIN",
    "ORIGIN_CITY_NAME",
    "ORIGIN_STATE_ABR",
    "ORIGIN_COUNTRY",
    "ORIGIN_COUNTRY_NAME",
    "DEST",
    "DEST_CITY_NAME",
    "DEST_STATE_ABR",
    "DEST_STATE_NM",
    "DEST_COUNTRY",
    "DEST_COUNTRY_NAME",
    "YEAR",
    "MONTH",
    "DATA_SOURCE",
)


def build_connection(config: Config | None = None) -> duckdb.DuckDBPyConnection:
    """Open an in-memory DuckDB connection with the raw files registered.

    Creates the :data:`RAW_VIEW` view over the configured raw glob using a real
    CSV parser (``read_csv`` with ``union_by_name`` so a differing column order
    between yearly files is handled), validates that every column in
    :data:`EXPECTED_COLUMNS` is present, and logs the total row count and the
    distinct ``YEAR`` range found.

    Args:
        config: Loaded :class:`~src.config.Config`. Loaded from disk if omitted.

    Returns:
        A DuckDB connection with the ``raw_segments`` view ready to query.

    Raises:
        ValueError: If any expected column is missing from the raw files.
    """
    config = config or get_config()
    con = duckdb.connect()

    # CREATE VIEW cannot take a prepared parameter, so the glob (a trusted
    # config value, not user input) is inlined with single quotes escaped.
    glob_literal = config.data.raw_glob.replace("'", "''")
    con.execute(
        f"""
        CREATE VIEW {RAW_VIEW} AS
        SELECT * FROM read_csv('{glob_literal}', header=true, union_by_name=true)
        """
    )

    _validate_columns(con)

    total_rows, year_min, year_max = con.execute(
        f"SELECT COUNT(*), MIN(YEAR), MAX(YEAR) FROM {RAW_VIEW}"
    ).fetchone()
    logger.info(
        "Registered raw segments from %s: %d rows, YEAR range %s-%s.",
        config.data.raw_glob,
        total_rows,
        year_min,
        year_max,
    )

    return con


def _validate_columns(con: duckdb.DuckDBPyConnection) -> None:
    """Raise if any expected column is absent from the registered view."""
    present = {row[0] for row in con.execute(f"DESCRIBE {RAW_VIEW}").fetchall()}
    missing = [col for col in EXPECTED_COLUMNS if col not in present]
    if missing:
        raise ValueError(
            f"Raw segment files are missing expected column(s): {missing}. "
            f"Found columns: {sorted(present)}."
        )
    logger.debug("All %d expected columns present.", len(EXPECTED_COLUMNS))


def window_predicate(config: Config) -> str:
    """Return the SQL predicate restricting rows to the analysis window.

    The window is complete calendar years only (``start_year``..``end_year``
    from config); any year outside it (e.g. the partial 2026 tail) is excluded
    here so every downstream series spans whole years.
    """
    return f"YEAR BETWEEN {config.data.start_year} AND {config.data.end_year}"


def clean_predicate(config: Config) -> str:
    """Return the SQL predicate for the rows kept for aggregation.

    Combines the analysis :func:`window_predicate` with the row-validity filters
    that data-quality rules 1 and 5 describe, so aggregation operates on the
    cleaned set rather than describing a filter it never applies:

    * FREIGHT must be present and non-negative (it is a weight in pounds), and
    * DATA_SOURCE must classify as domestic (``D...``) or international (``I...``).

    With the current data no in-window row fails either filter, so they remove
    nothing; they make the exclusion real should a bad row ever appear.
    """
    return (
        f"{window_predicate(config)}"
        " AND FREIGHT IS NOT NULL AND FREIGHT >= 0"
        " AND DATA_SOURCE IS NOT NULL AND SUBSTR(DATA_SOURCE, 1, 1) IN ('D', 'I')"
    )


def fedex_window_frame(con: duckdb.DuckDBPyConnection, config: Config):
    """Return a tidy pandas frame of in-window FedEx segment rows.

    Convenience for downstream code that prefers a frame to SQL. Filters to the
    configured FedEx carrier code and the complete-year analysis window.

    Args:
        con: Connection from :func:`build_connection`.
        config: Loaded configuration.

    Returns:
        A pandas DataFrame of FedEx segment rows within the analysis window.
    """
    return con.execute(
        f"""
        SELECT *
        FROM {RAW_VIEW}
        WHERE UNIQUE_CARRIER = ?
          AND {window_predicate(config)}
        """,
        [config.fedex_carrier_code],
    ).df()


def main() -> None:
    """Register the raw files and log a short summary (smoke test for ingest)."""
    setup_logging()
    config = get_config()
    con = build_connection(config)
    fedex_rows = con.execute(
        f"SELECT COUNT(*) FROM {RAW_VIEW} WHERE UNIQUE_CARRIER = ? AND {window_predicate(config)}",
        [config.fedex_carrier_code],
    ).fetchone()[0]
    logger.info(
        "In-window (%d-%d) FedEx segment rows: %d.",
        config.data.start_year,
        config.data.end_year,
        fedex_rows,
    )


if __name__ == "__main__":
    main()
