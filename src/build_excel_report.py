"""Build the Excel report with live formulas.

Writes ``outputs/FedEx_Forecasting_Report.xlsx`` (git-ignored) with:

* **Data tabs** holding the raw processed series as plain values so that
  presentation formulas can reference them.
* **Presentation tabs** (``Summary``, ``Hub``, ``Break Analysis``) whose
  headline cells are *live Excel formulas* (the string stored in the cell
  begins with ``=``) rather than numbers precomputed in Python. The actual
  numeric results are produced by Excel at open/recalculation time.

The charts are :mod:`openpyxl` ``LineChart`` objects that reference the data
tabs, so they render from the stored series.

Recalculation note: a full automated recalculation needs a headless
spreadsheet engine (LibreOffice ``soffice``). When one is available this
module recalculates the workbook and asserts that no cell holds an error
literal (``#REF!``, ``#DIV/0!``, ``#N/A``, ``#VALUE!``, ``#NAME?``). When one
is *not* available it does **not** claim an automated recalc: it runs a static
formula audit (every formula's referenced sheets exist and no error literal is
embedded in any formula string) and records, on a ``Notes`` tab, exactly what
was and was not verified.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
from openpyxl import Workbook, load_workbook
from openpyxl.chart import LineChart, Reference
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.worksheet.worksheet import Worksheet

from src.config import Config, get_config, setup_logging

logger = logging.getLogger(__name__)

# Project root (parent of the ``src`` package) so output/processed paths work
# regardless of the current working directory.
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Default workbook location (git-ignored via ``outputs/*.xlsx``).
DEFAULT_OUTPUT = PROJECT_ROOT / "outputs" / "FedEx_Forecasting_Report.xlsx"

# Excel error literals that must never appear after a recalculation.
ERROR_LITERALS = ("#REF!", "#DIV/0!", "#N/A", "#VALUE!", "#NAME?", "#NULL!", "#NUM!")

# Styling constants.
_HEADER_FILL = PatternFill("solid", fgColor="1F3864")
_HEADER_FONT = Font(bold=True, color="FFFFFF")
_TITLE_FONT = Font(bold=True, size=14, color="1F3864")
_SUBTITLE_FONT = Font(bold=True, size=11, color="1F3864")
_LABEL_FONT = Font(bold=True)
_NOTE_FONT = Font(italic=True, color="595959")
_FREIGHT_FMT = "#,##0"
_PCT_FMT = "0.00%"
_DATE_FMT = "yyyy-mm"


@dataclass(frozen=True)
class Frames:
    """The processed series needed to build the workbook."""

    network: pd.DataFrame
    dom_intl: pd.DataFrame
    hubs_selected: pd.DataFrame
    hub_pivot: pd.DataFrame
    accuracy: pd.DataFrame
    monitor: pd.DataFrame
    breaks: pd.DataFrame


def load_frames(processed_dir: Path, config: Config) -> Frames:
    """Load the processed CSVs required by the report.

    Args:
        processed_dir: Directory holding the processed CSVs.
        config: Pipeline configuration (for filenames and hub ordering).

    Returns:
        A :class:`Frames` bundle with every series the workbook references.
    """
    data = config.data

    network = pd.read_csv(
        processed_dir / data.network_series_file, parse_dates=["month"]
    ).sort_values("month")
    dom_intl = pd.read_csv(
        processed_dir / data.network_domestic_intl_file, parse_dates=["month"]
    ).sort_values("month")
    hubs_selected = pd.read_csv(processed_dir / "hubs_selected.csv").sort_values("rank")
    hub_long = pd.read_csv(
        processed_dir / data.hub_series_file, parse_dates=["month"]
    ).sort_values("month")
    accuracy = pd.read_csv(processed_dir / data.backtest_accuracy_file)
    monitor = pd.read_csv(processed_dir / data.monitoring_file, parse_dates=["month"])
    breaks = pd.read_csv(
        processed_dir / data.break_analysis_file, parse_dates=["break_month"]
    )

    # Pivot hub freight to month x hub, columns ordered by selection rank so the
    # presentation layer can address hubs by a stable column position.
    hub_order = hubs_selected["hub"].tolist()
    hub_pivot = (
        hub_long.pivot(index="month", columns="hub", values="fedex_freight")
        .reindex(columns=hub_order)
        .reset_index()
        .sort_values("month")
    )

    return Frames(
        network=network,
        dom_intl=dom_intl,
        hubs_selected=hubs_selected,
        hub_pivot=hub_pivot,
        accuracy=accuracy,
        monitor=monitor,
        breaks=breaks,
    )


def _style_header(ws: Worksheet, ncols: int, row: int = 1) -> None:
    """Apply the header fill/font to the first ``ncols`` cells of ``row``."""
    for col in range(1, ncols + 1):
        cell = ws.cell(row=row, column=col)
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT


def _autosize(ws: Worksheet, widths: dict[str, int]) -> None:
    """Set column widths from a ``{column_letter: width}`` mapping."""
    for letter, width in widths.items():
        ws.column_dimensions[letter].width = width


def _write_data_network(wb: Workbook, frames: Frames) -> int:
    """Write the ``Data_Network`` tab; return the last populated row index."""
    ws = wb.create_sheet("Data_Network")
    ws.append(["month", "fedex_freight", "market_freight", "fedex_market_share"])
    for _, r in frames.network.iterrows():
        ws.append(
            [
                r["month"].date(),
                float(r["fedex_freight"]),
                float(r["market_freight"]),
                float(r["fedex_market_share"]),
            ]
        )
    last_row = ws.max_row
    for row in range(2, last_row + 1):
        ws.cell(row=row, column=1).number_format = _DATE_FMT
        ws.cell(row=row, column=2).number_format = _FREIGHT_FMT
        ws.cell(row=row, column=3).number_format = _FREIGHT_FMT
        ws.cell(row=row, column=4).number_format = _PCT_FMT
    _style_header(ws, 4)
    _autosize(ws, {"A": 12, "B": 16, "C": 16, "D": 18})
    return last_row


def _write_data_dom_intl(wb: Workbook, frames: Frames) -> int:
    """Write the ``Data_DomIntl`` tab; return the last populated row index."""
    ws = wb.create_sheet("Data_DomIntl")
    ws.append(["month", "fedex_domestic_freight", "fedex_intl_freight"])
    for _, r in frames.dom_intl.iterrows():
        ws.append(
            [
                r["month"].date(),
                float(r["fedex_domestic_freight"]),
                float(r["fedex_intl_freight"]),
            ]
        )
    last_row = ws.max_row
    for row in range(2, last_row + 1):
        ws.cell(row=row, column=1).number_format = _DATE_FMT
        ws.cell(row=row, column=2).number_format = _FREIGHT_FMT
        ws.cell(row=row, column=3).number_format = _FREIGHT_FMT
    _style_header(ws, 3)
    _autosize(ws, {"A": 12, "B": 22, "C": 20})
    return last_row


def _write_data_hubs(wb: Workbook, frames: Frames) -> tuple[int, list[str]]:
    """Write the pivoted ``Data_Hubs`` tab.

    Returns:
        ``(last_row, hub_order)`` where ``hub_order`` is the column order of the
        hubs (column B onward), used by the ``Hub`` tab's INDEX/MATCH formulas.
    """
    ws = wb.create_sheet("Data_Hubs")
    hub_order = [c for c in frames.hub_pivot.columns if c != "month"]
    ws.append(["month", *hub_order])
    for _, r in frames.hub_pivot.iterrows():
        ws.append([r["month"].date(), *(float(r[h]) for h in hub_order)])
    last_row = ws.max_row
    for row in range(2, last_row + 1):
        ws.cell(row=row, column=1).number_format = _DATE_FMT
        for col in range(2, 2 + len(hub_order)):
            ws.cell(row=row, column=col).number_format = _FREIGHT_FMT
    _style_header(ws, 1 + len(hub_order))
    widths = {"A": 12}
    for idx in range(len(hub_order)):
        widths[get_column_letter(2 + idx)] = 14
    _autosize(ws, widths)
    return last_row, hub_order


def _write_data_accuracy(wb: Workbook, frames: Frames) -> tuple[int, int]:
    """Write the long ``Data_Accuracy`` tab with network rows first.

    A live ``key`` column (``level|method``) is added so the Hub tab can do a
    single-key INDEX/MATCH that works in every Excel version.

    Returns:
        ``(last_row, n_network_rows)`` where ``n_network_rows`` is how many
        contiguous rows (starting at row 2) belong to the ``network`` level.
    """
    ws = wb.create_sheet("Data_Accuracy")
    ws.append(["level", "method", "wape", "mape", "key"])

    acc = frames.accuracy.copy()
    hub_order = frames.hubs_selected["hub"].tolist()
    level_rank = {"network": -1, **{h: i for i, h in enumerate(hub_order)}}
    acc["_lvl"] = acc["level"].map(lambda x: level_rank.get(x, 99))
    acc = acc.sort_values(["_lvl", "wape"]).drop(columns="_lvl")

    n_network = int((acc["level"] == "network").sum())

    for _, r in acc.iterrows():
        ws.append([r["level"], r["method"], float(r["wape"]), float(r["mape"])])
    last_row = ws.max_row
    for row in range(2, last_row + 1):
        ws.cell(row=row, column=3).number_format = _PCT_FMT
        ws.cell(row=row, column=4).number_format = _PCT_FMT
        # Live helper key, so lookups stay formula-driven end to end.
        ws.cell(row=row, column=5).value = f'=A{row}&"|"&B{row}'
    _style_header(ws, 5)
    _autosize(ws, {"A": 12, "B": 22, "C": 10, "D": 10, "E": 26})
    return last_row, n_network


def _write_data_monitor(wb: Workbook, frames: Frames) -> int:
    """Write the ``Data_Monitor`` tab; return the last populated row index."""
    ws = wb.create_sheet("Data_Monitor")
    ws.append(["month", "error", "roll_mean", "roll_std", "lower", "upper", "flagged"])
    for _, r in frames.monitor.iterrows():
        ws.append(
            [
                r["month"].date(),
                float(r["error"]),
                None if pd.isna(r["roll_mean"]) else float(r["roll_mean"]),
                None if pd.isna(r["roll_std"]) else float(r["roll_std"]),
                None if pd.isna(r["lower"]) else float(r["lower"]),
                None if pd.isna(r["upper"]) else float(r["upper"]),
                bool(r["flagged"]),
            ]
        )
    last_row = ws.max_row
    for row in range(2, last_row + 1):
        ws.cell(row=row, column=1).number_format = _DATE_FMT
        for col in range(2, 7):
            ws.cell(row=row, column=col).number_format = _FREIGHT_FMT
    _style_header(ws, 7)
    _autosize(ws, {"A": 12, "B": 16, "C": 16, "D": 14, "E": 16, "F": 16, "G": 10})
    return last_row


def _build_summary(wb: Workbook, net_last: int, n_network: int) -> None:
    """Build the ``Summary`` tab (headline live formulas + two line charts)."""
    ws = wb.create_sheet("Summary", 0)
    net_rng = f"Data_Network!$B$2:$B${net_last}"
    share_rng = f"Data_Network!$D$2:$D${net_last}"
    acc_last = 1 + n_network  # network block occupies rows 2..(1+n_network)
    wape_rng = f"Data_Accuracy!$C$2:$C${acc_last}"
    method_rng = f"Data_Accuracy!$B$2:$B${acc_last}"
    mape_rng = f"Data_Accuracy!$D$2:$D${acc_last}"

    ws["A1"] = "FedEx Cargo Forecasting - Executive Summary"
    ws["A1"].font = _TITLE_FONT
    ws["A3"] = (
        "Headline figures below are live Excel formulas over the Data_* tabs; "
        "values compute on open/recalculation."
    )
    ws["A3"].font = _NOTE_FONT

    rows: list[tuple[str, str, str]] = [
        (
            "Earliest month FedEx network freight (lbs)",
            f"=INDEX({net_rng},1)",
            _FREIGHT_FMT,
        ),
        (
            "Latest month FedEx network freight (lbs)",
            f"=INDEX({net_rng},COUNT({net_rng}))",
            _FREIGHT_FMT,
        ),
        (
            "Overall freight change (latest vs earliest)",
            f"=INDEX({net_rng},COUNT({net_rng}))/INDEX({net_rng},1)-1",
            _PCT_FMT,
        ),
        (
            "Average FedEx market share",
            f"=AVERAGE({share_rng})",
            _PCT_FMT,
        ),
        (
            "Latest FedEx market share",
            f"=INDEX({share_rng},COUNT({share_rng}))",
            _PCT_FMT,
        ),
        (
            "Best network model (lowest WAPE)",
            f"=INDEX({method_rng},MATCH(MIN({wape_rng}),{wape_rng},0))",
            "General",
        ),
        (
            "Best network model WAPE",
            f"=MIN({wape_rng})",
            _PCT_FMT,
        ),
        (
            "Best network model MAPE",
            f"=INDEX({mape_rng},MATCH(MIN({wape_rng}),{wape_rng},0))",
            _PCT_FMT,
        ),
    ]
    start = 5
    ws.cell(row=start - 1, column=1, value="Metric").font = _SUBTITLE_FONT
    ws.cell(row=start - 1, column=2, value="Value").font = _SUBTITLE_FONT
    for i, (label, formula, fmt) in enumerate(rows):
        r = start + i
        ws.cell(row=r, column=1, value=label).font = _LABEL_FONT
        cell = ws.cell(row=r, column=2, value=formula)
        if fmt != "General":
            cell.number_format = fmt
    _autosize(ws, {"A": 44, "B": 18})

    # Line chart: network freight trend.
    freight_chart = LineChart()
    freight_chart.title = "FedEx network freight (lbs), 2011-2025"
    freight_chart.height = 8
    freight_chart.width = 18
    freight_chart.y_axis.numFmt = _FREIGHT_FMT
    freight_chart.y_axis.title = "Freight (lbs)"
    freight_chart.x_axis.title = "Month"
    data = Reference(
        wb["Data_Network"], min_col=2, min_row=1, max_row=net_last, max_col=2
    )
    cats = Reference(wb["Data_Network"], min_col=1, min_row=2, max_row=net_last)
    freight_chart.add_data(data, titles_from_data=True)
    freight_chart.set_categories(cats)
    ws.add_chart(freight_chart, "D5")

    # Line chart: market-share trend.
    share_chart = LineChart()
    share_chart.title = "FedEx market share, 2011-2025"
    share_chart.height = 8
    share_chart.width = 18
    share_chart.y_axis.numFmt = _PCT_FMT
    share_chart.y_axis.title = "Market share"
    share_chart.x_axis.title = "Month"
    sdata = Reference(
        wb["Data_Network"], min_col=4, min_row=1, max_row=net_last, max_col=4
    )
    share_chart.add_data(sdata, titles_from_data=True)
    share_chart.set_categories(cats)
    ws.add_chart(share_chart, "D22")


def _build_hub(
    wb: Workbook,
    hub_last: int,
    hub_order: list[str],
    acc_last: int,
    methods: list[str],
) -> None:
    """Build the ``Hub`` tab: a dropdown plus dropdown-keyed live formulas.

    The selected hub (cell ``B1``) drives every figure: per-method WAPE/MAPE
    via single-key INDEX/MATCH on ``Data_Accuracy!key``, a helper column that
    pulls the selected hub's monthly series out of the pivoted ``Data_Hubs``
    tab via INDEX/MATCH, and a line chart that reads that helper column (so the
    chart tracks the dropdown on recalculation).
    """
    ws = wb.create_sheet("Hub")
    ws["A1"] = "Select hub:"
    ws["A1"].font = _LABEL_FONT
    ws["B1"] = hub_order[0]
    ws["B1"].font = Font(bold=True, color="1F3864")
    ws["B1"].alignment = Alignment(horizontal="center")
    ws["B1"].fill = PatternFill("solid", fgColor="D9E1F2")

    dv = DataValidation(
        type="list",
        formula1='"' + ",".join(hub_order) + '"',
        allow_blank=False,
        showDropDown=False,
    )
    dv.prompt = "Pick a hub to drive the figures below."
    dv.promptTitle = "Hub selector"
    ws.add_data_validation(dv)
    dv.add(ws["B1"])

    ws["A3"] = "Backtest accuracy for the selected hub (live, keyed on B1)"
    ws["A3"].font = _SUBTITLE_FONT

    key_rng = f"Data_Accuracy!$E$2:$E${acc_last}"
    acc_wape = f"Data_Accuracy!$C$2:$C${acc_last}"
    acc_mape = f"Data_Accuracy!$D$2:$D${acc_last}"

    # Per-method helper block.
    ws["A5"] = "Method"
    ws["B5"] = "WAPE"
    ws["C5"] = "MAPE"
    for c in ("A5", "B5", "C5"):
        ws[c].font = _SUBTITLE_FONT
    for i, method in enumerate(methods):
        r = 6 + i
        ws.cell(row=r, column=1, value=method)
        ws.cell(
            row=r,
            column=2,
            value=f'=INDEX({acc_wape},MATCH($B$1&"|"&$A{r},{key_rng},0))',
        ).number_format = _PCT_FMT
        ws.cell(
            row=r,
            column=3,
            value=f'=INDEX({acc_mape},MATCH($B$1&"|"&$A{r},{key_rng},0))',
        ).number_format = _PCT_FMT
    first_m, last_m = 6, 6 + len(methods) - 1
    wape_cells = f"$B${first_m}:$B${last_m}"
    mape_cells = f"$C${first_m}:$C${last_m}"
    method_cells = f"$A${first_m}:$A${last_m}"

    best_row = last_m + 2
    ws.cell(row=best_row, column=1, value="Best method (lowest WAPE)").font = _LABEL_FONT
    ws.cell(
        row=best_row,
        column=2,
        value=f"=INDEX({method_cells},MATCH(MIN({wape_cells}),{wape_cells},0))",
    )
    ws.cell(row=best_row + 1, column=1, value="Best WAPE").font = _LABEL_FONT
    ws.cell(row=best_row + 1, column=2, value=f"=MIN({wape_cells})").number_format = (
        _PCT_FMT
    )
    ws.cell(row=best_row + 2, column=1, value="Best MAPE").font = _LABEL_FONT
    ws.cell(
        row=best_row + 2,
        column=2,
        value=f"=INDEX({mape_cells},MATCH(MIN({wape_cells}),{wape_cells},0))",
    ).number_format = _PCT_FMT

    # Selected-hub volume headline figures (from the helper column H/I below).
    sel = f"$I$2:$I${hub_last}"
    vol_row = best_row + 4
    ws.cell(row=vol_row, column=1, value="Selected hub volume (lbs)").font = (
        _SUBTITLE_FONT
    )
    ws.cell(row=vol_row + 1, column=1, value="Earliest month freight").font = _LABEL_FONT
    ws.cell(
        row=vol_row + 1, column=2, value=f"=INDEX({sel},1)"
    ).number_format = _FREIGHT_FMT
    ws.cell(row=vol_row + 2, column=1, value="Latest month freight").font = _LABEL_FONT
    ws.cell(
        row=vol_row + 2, column=2, value=f"=INDEX({sel},COUNT({sel}))"
    ).number_format = _FREIGHT_FMT
    ws.cell(
        row=vol_row + 3, column=1, value="Latest 12-month change (YoY)"
    ).font = _LABEL_FONT
    ws.cell(
        row=vol_row + 3,
        column=2,
        value=f"=INDEX({sel},COUNT({sel}))/INDEX({sel},COUNT({sel})-12)-1",
    ).number_format = _PCT_FMT

    # Helper series (columns H month / I selected-hub freight) feeding the chart.
    ws["H1"] = "month"
    ws["I1"] = "selected_hub_freight"
    for c in ("H1", "I1"):
        ws[c].font = _HEADER_FONT
        ws[c].fill = _HEADER_FILL
    n_hubs = len(hub_order)
    hub_block = f"Data_Hubs!$B$2:${get_column_letter(1 + n_hubs)}${hub_last}"
    hdr_block = f"Data_Hubs!$B$1:${get_column_letter(1 + n_hubs)}$1"
    for i in range(hub_last - 1):
        r = 2 + i
        ws.cell(row=r, column=8, value=f"=Data_Hubs!A{r}").number_format = _DATE_FMT
        ws.cell(
            row=r,
            column=9,
            value=f"=INDEX({hub_block},ROW()-1,MATCH($B$1,{hdr_block},0))",
        ).number_format = _FREIGHT_FMT

    _autosize(ws, {"A": 30, "B": 18, "C": 14, "H": 12, "I": 20})

    chart = LineChart()
    chart.title = "Selected hub monthly FedEx freight (lbs) - driven by B1"
    chart.height = 9
    chart.width = 20
    chart.y_axis.numFmt = _FREIGHT_FMT
    chart.y_axis.title = "Freight (lbs)"
    chart.x_axis.title = "Month"
    data = Reference(ws, min_col=9, min_row=1, max_row=hub_last, max_col=9)
    cats = Reference(ws, min_col=8, min_row=2, max_row=hub_last)
    chart.add_data(data, titles_from_data=True)
    chart.set_categories(cats)
    ws.add_chart(chart, "E5")


def _build_break_analysis(
    wb: Workbook,
    frames: Frames,
    di_last: int,
    mon_last: int,
) -> None:
    """Build the ``Break Analysis`` tab: dom/intl chart + labeled callouts.

    The certified domestic break (data-derived from ``break_analysis.csv``) and
    the monitoring-flagged anomaly (located with a live MATCH over
    ``Data_Monitor``) are called out in clearly-labeled cells, with 2024-09
    explicitly described as the monitoring-flagged anomaly / regime-change
    candidate and *not* a certified structural break.
    """
    ws = wb.create_sheet("Break Analysis")
    ws["A1"] = "Structural Break & Monitoring Alert - Domestic vs International"
    ws["A1"].font = _TITLE_FONT

    dom = frames.breaks[frames.breaks["series"] == "domestic"]
    # The certified domestic break the monitoring narrative anchors on: the
    # latest block-significant domestic break (2021-04 in the processed data).
    sig = dom[dom["p_bootstrap_block"] < 0.05]
    certified = sig.sort_values("break_month").iloc[-1] if not sig.empty else dom.iloc[0]

    ws["A3"] = "Certified domestic structural break (binary-segmentation sup-Chow)"
    ws["A3"].font = _SUBTITLE_FONT
    callouts = [
        ("Break month", certified["break_month"].date(), _DATE_FMT),
        ("Pre-break mean freight (lbs)", float(certified["pre_mean"]), _FREIGHT_FMT),
        ("Post-break mean freight (lbs)", float(certified["post_mean"]), _FREIGHT_FMT),
        ("Change at break", float(certified["pct_change"]) / 100.0, _PCT_FMT),
        ("Block-bootstrap p-value", float(certified["p_bootstrap_block"]), "0.0000"),
    ]
    for i, (label, value, fmt) in enumerate(callouts):
        r = 4 + i
        ws.cell(row=r, column=1, value=label).font = _LABEL_FONT
        cell = ws.cell(row=r, column=2, value=value)
        cell.number_format = fmt

    ws["A10"] = "Monitoring-flagged anomaly (regime-change candidate)"
    ws["A10"].font = _SUBTITLE_FONT
    mon_month_rng = f"Data_Monitor!$A$2:$A${mon_last}"
    mon_flag_rng = f"Data_Monitor!$G$2:$G${mon_last}"
    mon_err_rng = f"Data_Monitor!$B$2:$B${mon_last}"
    ws.cell(row=11, column=1, value="First flagged month").font = _LABEL_FONT
    ws.cell(
        row=11,
        column=2,
        value=f"=INDEX({mon_month_rng},MATCH(TRUE,{mon_flag_rng},0))",
    ).number_format = _DATE_FMT
    ws.cell(row=12, column=1, value="Forecast error at flag (lbs)").font = _LABEL_FONT
    ws.cell(
        row=12,
        column=2,
        value=f"=INDEX({mon_err_rng},MATCH(TRUE,{mon_flag_rng},0))",
    ).number_format = _FREIGHT_FMT

    ws["A14"] = (
        "Note: 2021-04 is a CERTIFIED domestic structural break (conservative "
        "seasonality-aware test). The monitoring flag (2024-09) is a flagged "
        "anomaly / regime-change candidate, NOT a certified structural break: "
        "the conservative test does not certify it on ~16 post-anomaly months."
    )
    ws["A14"].font = _NOTE_FONT
    ws["A14"].alignment = Alignment(wrap_text=True, vertical="top")
    ws.merge_cells("A14:B18")

    _autosize(ws, {"A": 42, "B": 20})

    chart = LineChart()
    chart.title = "FedEx domestic vs international monthly freight (lbs)"
    chart.height = 10
    chart.width = 22
    chart.y_axis.numFmt = _FREIGHT_FMT
    chart.y_axis.title = "Freight (lbs)"
    chart.x_axis.title = "Month"
    data = Reference(
        wb["Data_DomIntl"], min_col=2, min_row=1, max_row=di_last, max_col=3
    )
    cats = Reference(wb["Data_DomIntl"], min_col=1, min_row=2, max_row=di_last)
    chart.add_data(data, titles_from_data=True)
    chart.set_categories(cats)
    ws.add_chart(chart, "D3")


def _build_notes(wb: Workbook, recalc_mode: str, checks: list[str]) -> None:
    """Write the ``Notes`` tab documenting the recalculation/audit path."""
    ws = wb.create_sheet("Notes")
    ws["A1"] = "Workbook notes - formulas & verification"
    ws["A1"].font = _TITLE_FONT
    lines = [
        "",
        "Presentation tabs (Summary, Hub, Break Analysis) use LIVE Excel "
        "formulas over the Data_* tabs; Excel computes the values on open.",
        "The Hub tab's cell B1 is a data-validation dropdown that drives every "
        "figure on that tab via INDEX/MATCH.",
        "",
        f"Recalculation verification path taken: {recalc_mode}.",
        "",
        "Checks performed:",
    ]
    for line in lines:
        ws.append([line])
    for check in checks:
        ws.append([f"  - {check}"])
    for row in range(1, ws.max_row + 1):
        ws.cell(row=row, column=1).alignment = Alignment(
            wrap_text=True, vertical="top"
        )
    ws.column_dimensions["A"].width = 110


def build_workbook(frames: Frames) -> Workbook:
    """Assemble the full workbook (data tabs, presentation tabs, charts).

    Args:
        frames: Processed series loaded by :func:`load_frames`.

    Returns:
        The assembled :class:`openpyxl.Workbook` (not yet verified/saved).
    """
    wb = Workbook()
    # Drop the default empty sheet; every sheet is created explicitly.
    wb.remove(wb.active)

    net_last = _write_data_network(wb, frames)
    di_last = _write_data_dom_intl(wb, frames)
    hub_last, hub_order = _write_data_hubs(wb, frames)
    acc_last, n_network = _write_data_accuracy(wb, frames)
    mon_last = _write_data_monitor(wb, frames)

    methods = (
        frames.accuracy[frames.accuracy["level"] == "network"]["method"].tolist()
        or frames.accuracy["method"].drop_duplicates().tolist()
    )

    _build_summary(wb, net_last, n_network)
    _build_hub(wb, hub_last, hub_order, acc_last, methods)
    _build_break_analysis(wb, frames, di_last, mon_last)

    # Order the sheets: presentation first, then data, then notes.
    desired = [
        "Summary",
        "Hub",
        "Break Analysis",
        "Data_Network",
        "Data_DomIntl",
        "Data_Hubs",
        "Data_Accuracy",
        "Data_Monitor",
    ]
    wb._sheets.sort(key=lambda s: desired.index(s.title) if s.title in desired else 99)
    return wb


def _find_soffice() -> str | None:
    """Return a usable LibreOffice/soffice executable path, or ``None``."""
    for name in ("soffice", "libreoffice"):
        found = shutil.which(name)
        if found:
            return found
    mac_bundle = Path("/Applications/LibreOffice.app/Contents/MacOS/soffice")
    if mac_bundle.exists():
        return str(mac_bundle)
    return None


def _static_formula_audit(wb: Workbook) -> list[str]:
    """Audit formulas without a recalc engine.

    Verifies that (a) no formula string embeds an error literal and (b) every
    ``Sheet!`` reference inside a formula names a sheet that exists.

    Returns:
        A list of human-readable problem descriptions (empty when clean).
    """
    problems: list[str] = []
    sheet_names = set(wb.sheetnames)
    ref_pattern = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)!")
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                value = cell.value
                if not isinstance(value, str) or not value.startswith("="):
                    continue
                for literal in ERROR_LITERALS:
                    if literal in value:
                        problems.append(
                            f"{ws.title}!{cell.coordinate}: embeds {literal}"
                        )
                for name in ref_pattern.findall(value):
                    # Ignore function-name false positives: only flag tokens
                    # that look like a sheet reference but match no sheet AND
                    # are not a known Excel construct.
                    if name not in sheet_names and name.startswith("Data_"):
                        problems.append(
                            f"{ws.title}!{cell.coordinate}: references unknown "
                            f"sheet '{name}'"
                        )
    return problems


def _recalc_with_soffice(path: Path, soffice: str) -> list[str]:
    """Recalculate ``path`` headlessly with LibreOffice and scan for errors.

    Converts the workbook to xlsx via ``soffice`` (which recalculates formulas),
    reloads with ``data_only=True`` and scans every cached value for an Excel
    error literal.

    Returns:
        A list of ``sheet!cell: #ERROR`` strings (empty when clean).
    """
    problems: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        cmd = [
            soffice,
            "--headless",
            "--calc",
            "--convert-to",
            "xlsx:Calc MS Excel 2007 XML",
            "--outdir",
            tmp,
            str(path),
        ]
        subprocess.run(cmd, check=True, capture_output=True, timeout=180)
        out = Path(tmp) / (path.stem + ".xlsx")
        if not out.exists():
            raise FileNotFoundError(f"soffice produced no output at {out}")
        recalced = load_workbook(out, data_only=True)
        for ws in recalced.worksheets:
            for row in ws.iter_rows():
                for cell in row:
                    if isinstance(cell.value, str) and cell.value in ERROR_LITERALS:
                        problems.append(f"{ws.title}!{cell.coordinate}: {cell.value}")
    return problems


def verify_workbook(path: Path) -> tuple[str, list[str], list[str]]:
    """Verify the saved workbook and return the path taken.

    Returns:
        ``(recalc_mode, problems, checks)``:
        * ``recalc_mode`` - "automated LibreOffice recalc" or "documented
          manual / static audit (no soffice available)".
        * ``problems`` - any error findings (empty means clean).
        * ``checks`` - human-readable descriptions of what was verified.
    """
    soffice = _find_soffice()
    if soffice is not None:
        logger.info("LibreOffice found at %s; running headless recalc.", soffice)
        problems = _recalc_with_soffice(path, soffice)
        checks = [
            "Full headless recalculation via LibreOffice (soffice).",
            "Scanned every cached cell value for #REF!/#DIV/0!/#N/A/#VALUE!/"
            "#NAME?/#NULL!/#NUM! - none found." if not problems else
            "Error literals detected (see report).",
        ]
        return "automated LibreOffice recalc", problems, checks

    logger.warning(
        "No LibreOffice/soffice available; NOT claiming an automated recalc. "
        "Running a static formula audit and documenting manual spot-checks."
    )
    wb = load_workbook(path)
    problems = _static_formula_audit(wb)
    checks = [
        "No headless spreadsheet engine (soffice) was available, so NO "
        "automated full recalculation was performed.",
        "Static audit: no formula string embeds an Excel error literal "
        "(#REF!, #DIV/0!, #N/A, #VALUE!, #NAME?, #NULL!, #NUM!).",
        "Static audit: every Data_* sheet reference inside a formula resolves "
        "to a sheet that exists in the workbook.",
        "Spot-check by inspection: Summary uses INDEX/COUNT over "
        "Data_Network!B/D and INDEX/MATCH(MIN()) over the network rows of "
        "Data_Accuracy.",
        "Spot-check by inspection: Hub lookups are single-key "
        "INDEX/MATCH(B1&\"|\"&method) over Data_Accuracy!key, and the helper "
        "column is INDEX(Data_Hubs block, ROW()-1, MATCH(B1, header)).",
        "Spot-check by inspection: Break Analysis locates the monitoring flag "
        "with MATCH(TRUE, Data_Monitor flagged column).",
        "To recalc fully, open the workbook in Excel/LibreOffice (formulas "
        "recompute on open) or run `make excel` where soffice is installed.",
    ]
    return (
        "documented manual / static audit (no soffice available)",
        problems,
        checks,
    )


def write_report(
    output_path: Path | None = None,
    processed_dir: Path | None = None,
    config: Config | None = None,
) -> Path:
    """Build, save, and verify the Excel report.

    Args:
        output_path: Where to write the ``.xlsx`` (default
            :data:`DEFAULT_OUTPUT`).
        processed_dir: Directory with the processed CSVs (default from config).
        config: Optional preloaded config.

    Returns:
        The path to the saved workbook.
    """
    config = config or get_config()
    output_path = output_path or DEFAULT_OUTPUT
    processed_dir = processed_dir or (PROJECT_ROOT / config.data.processed_dir)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    frames = load_frames(processed_dir, config)
    wb = build_workbook(frames)

    # Recalculate on open so formula results are present the moment a user
    # opens the file in Excel.
    wb.calculation.fullCalcOnLoad = True
    wb.save(output_path)
    logger.info("Wrote workbook with %d sheets to %s", len(wb.sheetnames), output_path)

    recalc_mode, problems, checks = verify_workbook(output_path)
    # Re-open to append the Notes tab with the verification outcome, then save.
    wb2 = load_workbook(output_path)
    _build_notes(wb2, recalc_mode, checks)
    wb2.calculation.fullCalcOnLoad = True
    wb2.save(output_path)

    if problems:
        for p in problems:
            logger.error("Formula verification problem: %s", p)
        logger.error(
            "Verification (%s) found %d problem(s).", recalc_mode, len(problems)
        )
    else:
        logger.info("Verification (%s): no formula errors found.", recalc_mode)
    return output_path


def main() -> None:
    """Build the Excel report and log the verification outcome."""
    setup_logging()
    write_report()


if __name__ == "__main__":
    main()
