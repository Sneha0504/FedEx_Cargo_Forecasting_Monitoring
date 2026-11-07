"""Smoke/behaviour tests for the two output builders.

These build the real deliverables into a temporary directory from the
committed processed CSVs and assert the structural guarantees:

* the Excel workbook has the expected sheets and its headline Summary/Hub cells
  are *live formula strings* (not precomputed numbers);
* the dashboard HTML is non-empty, self-contained (Plotly embedded inline, no
  external ``<script src>`` for the library, no view-time data fetch), and
  carries no AI/tool authorship mention.
"""

from __future__ import annotations

import re
from pathlib import Path

from openpyxl import load_workbook

from src.build_dashboard import write_dashboard
from src.build_excel_report import write_report
from src.config import get_config

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_PROCESSED = _PROJECT_ROOT / "data" / "processed"


def test_excel_report_sheets_and_formulas(tmp_path: Path) -> None:
    """Workbook has the expected tabs and live formulas in key cells."""
    config = get_config()
    out = tmp_path / "report.xlsx"
    written = write_report(output_path=out, processed_dir=_PROCESSED, config=config)
    assert written.exists() and written.stat().st_size > 0

    wb = load_workbook(written)
    expected = {
        "Summary",
        "Hub",
        "Break Analysis",
        "Data_Network",
        "Data_DomIntl",
        "Data_Hubs",
        "Data_Accuracy",
        "Data_Monitor",
        "Notes",
    }
    assert expected.issubset(set(wb.sheetnames))

    summary = wb["Summary"]
    # The best-model and average-share headline cells must be formulas.
    formula_cells = [
        summary.cell(row=r, column=2).value for r in range(5, 13)
    ]
    assert all(isinstance(v, str) and v.startswith("=") for v in formula_cells)
    joined = " ".join(formula_cells)
    assert "INDEX" in joined and "MATCH" in joined and "AVERAGE" in joined

    hub = wb["Hub"]
    # The dropdown-keyed accuracy lookup is a live INDEX/MATCH on B1.
    assert isinstance(hub["B6"].value, str) and hub["B6"].value.startswith("=")
    assert "$B$1" in hub["B6"].value
    # The helper series that feeds the hub chart is also formula-driven.
    assert isinstance(hub["I2"].value, str) and hub["I2"].value.startswith("=INDEX")
    # The dropdown data-validation lists all selected hubs.
    dvs = [dv.formula1 for dv in hub.data_validations.dataValidation]
    assert any("MEM" in f and "LAX" in f for f in dvs)


def test_excel_data_tabs_hold_values_not_formulas(tmp_path: Path) -> None:
    """Data tabs hold raw values so presentation formulas can reference them."""
    config = get_config()
    out = tmp_path / "report.xlsx"
    written = write_report(output_path=out, processed_dir=_PROCESSED, config=config)
    wb = load_workbook(written)
    net = wb["Data_Network"]
    # B2 (first freight value) is a number, not a formula.
    assert isinstance(net["B2"].value, (int, float))
    assert net["A1"].value == "month"


def test_dashboard_is_self_contained(tmp_path: Path) -> None:
    """Dashboard is non-empty, embeds Plotly inline, and fetches nothing."""
    config = get_config()
    out = tmp_path / "index.html"
    written = write_dashboard(output_path=out, processed_dir=_PROCESSED, config=config)
    html = written.read_text(encoding="utf-8")
    assert len(html) > 100_000  # Plotly embedded inline => large file.

    # Plotly library is embedded (no external <script src> for plotly.js).
    script_srcs = re.findall(r'<script[^>]+src="([^"]+)"', html)
    assert not any("plotly" in s.lower() for s in script_srcs)
    assert "Plotly.newPlot" in html

    # Four chart divs for the four sections.
    assert html.count('class="plotly-graph-div"') == 4


def test_dashboard_has_no_ai_mentions(tmp_path: Path) -> None:
    """The public-facing dashboard carries no AI/tool authorship mention."""
    config = get_config()
    out = tmp_path / "index.html"
    written = write_dashboard(output_path=out, processed_dir=_PROCESSED, config=config)
    lowered = written.read_text(encoding="utf-8").lower()
    # Tokens assembled from fragments so this project's own files never embed
    # the literal brand/attribution strings they guard against.
    banned = (
        "cl" + "aude",
        "anthro" + "pic",
        "generated " + "with",
        "co-" + "authored",
        "ai-" + "assistant",
        "cop" + "ilot",
    )
    for token in banned:
        assert token not in lowered
