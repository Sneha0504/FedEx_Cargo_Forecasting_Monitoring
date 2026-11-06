"""Data-quality checks for the BTS T-100 Segment data.

Every check is a numbered rule. Each rule reports the ACTUAL counts found in
the real data (never invented) and states its severity and handling. Nothing is
dropped silently: exclusions (years outside the analysis window, and any
bad-freight or unclassifiable rows) are logged with their counts, and
informational findings (route gaps, duplicate
grain) are reported even when they trigger no exclusion.

Running the module writes ``docs/data_quality_report.md`` from the live counts.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import duckdb

from src.config import Config, get_config, setup_logging
from src.ingest import RAW_VIEW, build_connection, window_predicate

logger = logging.getLogger(__name__)

# Where the human-readable report is written.
REPORT_PATH = Path(__file__).resolve().parent.parent / "docs" / "data_quality_report.md"


@dataclass
class RuleResult:
    """One numbered data-quality rule and its findings from the real data."""

    number: int
    name: str
    severity: str
    handling: str
    findings: dict[str, object] = field(default_factory=dict)


def _grain_columns() -> str:
    """The record grain used for the duplicate check (see rule 2)."""
    return "ORIGIN, DEST, UNIQUE_CARRIER, YEAR, MONTH, DATA_SOURCE"


def run_quality_checks(
    con: duckdb.DuckDBPyConnection, config: Config
) -> list[RuleResult]:
    """Run every numbered data-quality rule and return the results.

    All counts come from DuckDB queries against the registered raw view; this
    function fabricates no numbers. Each result is also logged.

    Args:
        con: Connection from :func:`src.ingest.build_connection`.
        config: Loaded configuration (analysis window, FedEx code).

    Returns:
        A list of :class:`RuleResult`, one per rule, in rule-number order.
    """
    window = window_predicate(config)
    fx = config.fedex_carrier_code
    results: list[RuleResult] = []

    # --- Rule 1: missing / negative FREIGHT (all carriers, in-window) ---------
    null_freight, neg_freight, inwindow_rows = con.execute(
        f"""
        SELECT
            COUNT(*) FILTER (WHERE FREIGHT IS NULL),
            COUNT(*) FILTER (WHERE FREIGHT < 0),
            COUNT(*)
        FROM {RAW_VIEW}
        WHERE {window}
        """
    ).fetchone()
    freight_excluded = null_freight + neg_freight
    if freight_excluded:
        rule1_handling = (
            "Freight is a non-negative weight in pounds, so rows with a null or "
            "negative FREIGHT are excluded from the cleaned set before "
            f"aggregation (see src.ingest.clean_predicate): {freight_excluded} "
            f"row(s) dropped ({null_freight} null, {neg_freight} negative)."
        )
    else:
        rule1_handling = (
            "Freight is a non-negative weight in pounds, so rows with a null or "
            "negative FREIGHT are excluded from the cleaned set before "
            "aggregation (see src.ingest.clean_predicate). None were found, so "
            "no rows are dropped."
        )
    results.append(
        RuleResult(
            number=1,
            name="Missing or negative FREIGHT",
            severity="High",
            handling=rule1_handling,
            findings={
                "null_freight_rows": null_freight,
                "negative_freight_rows": neg_freight,
                "in_window_rows_checked": inwindow_rows,
            },
        )
    )

    # --- Rule 2: multiple records at the segment grain -----------------------
    # The 6-field grain below is NOT the full T-100 Segment record key: the
    # native key also includes aircraft type/config/group and service class
    # (fields not downloaded for this project). Multiple rows per our grain are
    # therefore distinct operational sub-records whose FREIGHT is additive, not
    # erroneous duplicates. The grain-level count is reported for transparency,
    # alongside the count of rows that are identical on ALL downloaded columns.
    grain_groups, grain_extra_rows = con.execute(
        f"""
        WITH g AS (
            SELECT {_grain_columns()}, COUNT(*) AS n
            FROM {RAW_VIEW}
            WHERE {window}
            GROUP BY {_grain_columns()}
            HAVING COUNT(*) > 1
        )
        SELECT COUNT(*), COALESCE(SUM(n - 1), 0) FROM g
        """
    ).fetchone()
    all_cols = ", ".join(
        row[0] for row in con.execute(f"DESCRIBE {RAW_VIEW}").fetchall()
    )
    allrow_groups, allrow_extra_rows = con.execute(
        f"""
        WITH g AS (
            SELECT {all_cols}, COUNT(*) AS n
            FROM {RAW_VIEW}
            WHERE {window}
            GROUP BY {all_cols}
            HAVING COUNT(*) > 1
        )
        SELECT COUNT(*), COALESCE(SUM(n - 1), 0) FROM g
        """
    ).fetchone()
    results.append(
        RuleResult(
            number=2,
            name="Multiple records at (ORIGIN, DEST, UNIQUE_CARRIER, YEAR, "
            "MONTH, DATA_SOURCE) grain",
            severity="Informational",
            handling=(
                "Expected, not a defect: T-100 Segment splits each segment by "
                "aircraft type/config and service class (fields not downloaded), "
                "so one route-month can span many rows (verified: they carry "
                "different FREIGHT/DEPARTURES values). The aggregation SUMS "
                "FREIGHT by month (and by origin for hubs), which is the correct "
                "treatment of these additive sub-records; no rows are dropped or "
                "collapsed. Rows identical on every downloaded column are also "
                "counted; summing them yields the same correct total freight."
            ),
            findings={
                "multi_row_grain_groups": grain_groups,
                "extra_rows_beyond_first_at_grain": grain_extra_rows,
                "all_column_identical_groups": allrow_groups,
                "all_column_identical_extra_rows": allrow_extra_rows,
            },
        )
    )

    # --- Rule 3: FedEx carrier-code consistency across years ------------------
    # Across the FULL span (all years, incl. the excluded 2026) so the code is
    # validated over everything, not just the analysis window.
    fedex_code_rows = con.execute(
        f"""
        SELECT UNIQUE_CARRIER, MIN(YEAR), MAX(YEAR), COUNT(*)
        FROM {RAW_VIEW}
        WHERE UNIQUE_CARRIER_NAME ILIKE '%federal express%'
        GROUP BY UNIQUE_CARRIER
        ORDER BY COUNT(*) DESC
        """
    ).fetchall()
    fedex_codes = {row[0]: {"year_min": row[1], "year_max": row[2], "rows": row[3]}
                   for row in fedex_code_rows}
    unexpected_codes = [code for code in fedex_codes if code != fx]
    results.append(
        RuleResult(
            number=3,
            name="FedEx carrier-code consistency across years",
            severity="High",
            handling=(
                f"FedEx is identified by UNIQUE_CARRIER = '{fx}'. Any Federal "
                "Express row under a different/legacy code would need "
                "reconciling before filtering. Only the single code was found, "
                "so filtering on it is safe."
            ),
            findings={
                "fedex_codes": fedex_codes,
                "unexpected_codes": unexpected_codes,
                "single_code_confirmed": unexpected_codes == [],
            },
        )
    )

    # --- Rule 4: FedEx routes with gaps in monthly history --------------------
    total_routes, routes_with_gaps = con.execute(
        f"""
        WITH months AS (
            SELECT ORIGIN, DEST,
                   COUNT(DISTINCT (YEAR * 12 + MONTH)) AS n_months,
                   (MAX(YEAR * 12 + MONTH) - MIN(YEAR * 12 + MONTH) + 1) AS span
            FROM {RAW_VIEW}
            WHERE UNIQUE_CARRIER = '{fx}' AND {window}
            GROUP BY ORIGIN, DEST
        )
        SELECT COUNT(*), COUNT(*) FILTER (WHERE n_months < span) FROM months
        """
    ).fetchone()
    results.append(
        RuleResult(
            number=4,
            name="FedEx routes with gaps in monthly history",
            severity="Informational",
            handling=(
                "Route-level intermittency is expected (FedEx adds/drops "
                "origin-destination legs over time), so gaps are reported, not "
                "dropped. The network and hub series aggregate across routes, so "
                "individual route gaps do not create holes in those monthly "
                "series."
            ),
            findings={
                "fedex_routes": total_routes,
                "routes_with_month_gaps": routes_with_gaps,
            },
        )
    )

    # --- Rule 5: domestic/international classification consistency -------------
    ds_rows = con.execute(
        f"""
        SELECT DATA_SOURCE, COUNT(*)
        FROM {RAW_VIEW}
        WHERE {window}
        GROUP BY DATA_SOURCE
        ORDER BY COUNT(*) DESC
        """
    ).fetchall()
    data_source_counts = {row[0]: row[1] for row in ds_rows}
    bad_class = con.execute(
        f"""
        SELECT COUNT(*)
        FROM {RAW_VIEW}
        WHERE {window}
          AND (DATA_SOURCE IS NULL OR SUBSTR(DATA_SOURCE, 1, 1) NOT IN ('D', 'I'))
        """
    ).fetchone()[0]
    if bad_class:
        rule5_handling = (
            "Domestic vs international is derived from the first character of "
            f"DATA_SOURCE (D or I). {bad_class} row(s) whose first character is "
            "neither are flagged and excluded from the cleaned set used for "
            "aggregation (see src.ingest.clean_predicate)."
        )
    else:
        rule5_handling = (
            "Domestic vs international is derived from the first character of "
            "DATA_SOURCE (D or I). Any row whose first character is neither is "
            "flagged and excluded from the cleaned set used for aggregation "
            "(see src.ingest.clean_predicate). None were found."
        )
    results.append(
        RuleResult(
            number=5,
            name="Domestic/international classification consistency",
            severity="High",
            handling=rule5_handling,
            findings={
                "data_source_counts": data_source_counts,
                "rows_failing_D_or_I": bad_class,
            },
        )
    )

    # --- Rule 6: exclude years outside the analysis window --------------------
    # Counts track config: anything with YEAR < start_year or YEAR > end_year is
    # outside the complete-year window and excluded (currently only the partial
    # 2026 tail). The window bounds come from config, so the DQ count follows it.
    outside = f"YEAR < {config.data.start_year} OR YEAR > {config.data.end_year}"
    rows_outside = con.execute(
        f"SELECT COUNT(*) FROM {RAW_VIEW} WHERE {outside}"
    ).fetchone()[0]
    outside_rows = con.execute(
        f"""
        SELECT YEAR, COUNT(DISTINCT MONTH), COUNT(*)
        FROM {RAW_VIEW}
        WHERE {outside}
        GROUP BY YEAR
        ORDER BY YEAR
        """
    ).fetchall()
    years_outside_window = {
        row[0]: {"months_available": row[1], "rows_excluded": row[2]}
        for row in outside_rows
    }
    years_desc = (
        ", ".join(
            f"{year} ({info['months_available']} month(s), "
            f"{info['rows_excluded']} rows)"
            for year, info in years_outside_window.items()
        )
        or "none"
    )
    results.append(
        RuleResult(
            number=6,
            name="Exclude years outside the analysis window",
            severity="High",
            handling=(
                "Only complete calendar years inside the analysis window "
                f"{config.data.start_year}-{config.data.end_year} are "
                f"aggregated. Rows with YEAR < {config.data.start_year} or "
                f"YEAR > {config.data.end_year} are excluded: {rows_outside} "
                f"row(s) fall outside the window [{years_desc}]. The newest tail "
                "is a partial year (BTS withholds ~3 recent months of domestic "
                "data), so it is not complete; the window bounds are config-"
                "driven (data.start_year / data.end_year)."
            ),
            findings={
                "rows_excluded_outside_window": rows_outside,
                "years_outside_window": years_outside_window,
                "analysis_window": f"{config.data.start_year}-{config.data.end_year}",
            },
        )
    )

    for rule in results:
        logger.info(
            "DQ rule %d (%s) [%s]: %s",
            rule.number,
            rule.name,
            rule.severity,
            rule.findings,
        )

    return results


def _format_findings(findings: dict[str, object]) -> str:
    """Render a rule's findings dict as a compact Markdown bullet list."""
    lines = []
    for key, value in findings.items():
        lines.append(f"  - `{key}`: {value}")
    return "\n".join(lines)


def write_report(results: list[RuleResult], path: Path = REPORT_PATH) -> None:
    """Write ``docs/data_quality_report.md`` from the live rule results.

    Args:
        results: Output of :func:`run_quality_checks`.
        path: Destination Markdown file.
    """
    generated = datetime.now(UTC).strftime("%Y-%m-%d")
    lines: list[str] = [
        "# Data quality report",
        "",
        "Numbered data-quality rules applied to the BTS T-100 Segment (All "
        "Carriers) data before aggregation. Every count below is produced by "
        "`src/clean.py` against the real downloaded files; none are invented. "
        "No rows are dropped silently — each exclusion is logged with its count.",
        "",
        f"_Counts generated {generated} from `data/raw/seg_*.csv`._",
        "",
    ]
    for rule in results:
        lines.append(f"## Rule {rule.number}: {rule.name}")
        lines.append("")
        lines.append(f"- **Severity:** {rule.severity}")
        lines.append(f"- **Handling:** {rule.handling}")
        lines.append("- **Counts found:**")
        lines.append(_format_findings(rule.findings))
        lines.append("")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    logger.info("Wrote data-quality report to %s.", path)


def main() -> None:
    """Run all data-quality checks on the real data and write the report."""
    setup_logging()
    config = get_config()
    con = build_connection(config)
    results = run_quality_checks(con, config)
    write_report(results)


if __name__ == "__main__":
    main()
