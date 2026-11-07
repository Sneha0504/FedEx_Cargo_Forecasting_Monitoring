"""Build the precomputed, self-contained Plotly dashboard.

Writes ``docs_site/index.html``: a single HTML page with every series embedded
inline and Plotly's JavaScript bundled in (``include_plotlyjs=True``), so the
page opens directly from the filesystem with no server and performs no
view-time computation and no external data fetch.

The page contains four sections, all built from the processed CSVs:

1. Network FedEx freight trend and market-share trend (2011-2025), as two
   stacked panels.
2. A hub selector (Plotly ``updatemenus`` dropdown) that switches between each
   hub's monthly freight trend, with the hub's backtest WAPE/MAPE in the title.
3. A break-analysis chart (domestic vs international freight) marking the
   certified domestic structural break (2021-04) and the monitoring-flagged
   anomaly (2024-09) as an annotated alert point - labelled a regime-change
   candidate, explicitly NOT a certified break.
4. A model-comparison chart: backtest WAPE per method at every level.

The page is public-facing and contains no tool/authorship mention of any kind.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from src.config import Config, get_config, setup_logging

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = PROJECT_ROOT / "docs_site" / "index.html"

# Pounds -> millions of pounds, for readable axes.
_LB_TO_M = 1.0e6

# Okabe-Ito colourblind-safe palette.
_C_BLUE = "#0072B2"
_C_ORANGE = "#E69F00"
_C_GREEN = "#009E73"
_C_VERMILLION = "#D55E00"
_C_PURPLE = "#CC79A7"
_C_SKY = "#56B4E9"
_C_YELLOW = "#F0E442"
_HUB_COLORS = [_C_BLUE, _C_ORANGE, _C_GREEN, _C_PURPLE, _C_SKY]
_METHOD_COLORS = {
    "sarima": _C_BLUE,
    "seasonal_naive_trend": _C_ORANGE,
    "seasonal_naive": _C_GREEN,
}
_METHOD_LABELS = {
    "sarima": "SARIMA",
    "seasonal_naive_trend": "Seasonal naive + trend",
    "seasonal_naive": "Seasonal naive",
}

# Certified domestic structural break the narrative anchors on.
_CERTIFIED_BREAK = "2021-04-01"
# Monitoring-flagged anomaly / regime-change candidate (not a certified break).
_MONITOR_ALERT = "2024-09-01"

_PLOT_BG = "#FFFFFF"
_GRID = "#E6E6E6"
_FONT_FAMILY = "Segoe UI, Helvetica, Arial, sans-serif"
_MARGIN = {"l": 70, "r": 30, "t": 70, "b": 50}


@dataclass(frozen=True)
class DashboardData:
    """Processed series needed to render the dashboard."""

    network: pd.DataFrame
    dom_intl: pd.DataFrame
    hubs_selected: pd.DataFrame
    hub_long: pd.DataFrame
    accuracy: pd.DataFrame
    monitor: pd.DataFrame
    breaks: pd.DataFrame


def load_data(processed_dir: Path, config: Config) -> DashboardData:
    """Load the processed CSVs required by the dashboard.

    Args:
        processed_dir: Directory holding the processed CSVs.
        config: Pipeline configuration (filenames and hub ordering).

    Returns:
        A :class:`DashboardData` bundle.
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
    return DashboardData(
        network=network,
        dom_intl=dom_intl,
        hubs_selected=hubs_selected,
        hub_long=hub_long,
        accuracy=accuracy,
        monitor=monitor,
        breaks=breaks,
    )


def _base_layout(fig: go.Figure, title: str) -> None:
    """Apply the shared clean/professional layout to a figure."""
    fig.update_layout(
        title={"text": title, "x": 0.01, "xanchor": "left", "font": {"size": 18}},
        font={"family": _FONT_FAMILY, "size": 13, "color": "#222"},
        plot_bgcolor=_PLOT_BG,
        paper_bgcolor=_PLOT_BG,
        margin=_MARGIN,
        hovermode="x unified",
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.02, "x": 0},
    )
    fig.update_xaxes(gridcolor=_GRID, zeroline=False)
    fig.update_yaxes(gridcolor=_GRID, zeroline=False)


def build_network_figure(data: DashboardData) -> go.Figure:
    """Network freight trend (top) and market-share trend (bottom)."""
    net = data.network
    fig = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.12,
        subplot_titles=(
            "FedEx network freight (millions of lbs)",
            "FedEx market share of all-carrier freight",
        ),
    )
    fig.add_trace(
        go.Scatter(
            x=net["month"],
            y=net["fedex_freight"] / _LB_TO_M,
            mode="lines",
            name="FedEx freight",
            line={"color": _C_BLUE, "width": 2},
            hovertemplate="%{x|%b %Y}: %{y:,.0f} M lbs<extra></extra>",
        ),
        row=1,
        col=1,
    )
    fig.add_trace(
        go.Scatter(
            x=net["month"],
            y=net["fedex_market_share"] * 100.0,
            mode="lines",
            name="Market share",
            line={"color": _C_ORANGE, "width": 2},
            hovertemplate="%{x|%b %Y}: %{y:.1f}%<extra></extra>",
        ),
        row=2,
        col=1,
    )
    _base_layout(fig, "Network freight & market share, 2011-2025")
    fig.update_yaxes(title_text="Freight (M lbs)", row=1, col=1)
    fig.update_yaxes(title_text="Market share (%)", row=2, col=1)
    fig.update_xaxes(title_text="Month", row=2, col=1)
    fig.update_layout(showlegend=False, height=620)
    return fig


def _hub_accuracy(data: DashboardData, hub: str) -> tuple[str, float, float]:
    """Return ``(best_method_label, wape, mape)`` for a hub's best method."""
    rows = data.accuracy[data.accuracy["level"] == hub]
    best = rows.loc[rows["wape"].idxmin()]
    label = _METHOD_LABELS.get(best["method"], best["method"])
    return label, float(best["wape"]), float(best["mape"])


def build_hub_figure(data: DashboardData) -> go.Figure:
    """Hub freight trends with an ``updatemenus`` dropdown selector."""
    hubs = data.hubs_selected["hub"].tolist()
    fig = go.Figure()
    titles: list[str] = []
    for i, hub in enumerate(hubs):
        series = data.hub_long[data.hub_long["hub"] == hub]
        fig.add_trace(
            go.Scatter(
                x=series["month"],
                y=series["fedex_freight"] / _LB_TO_M,
                mode="lines",
                name=hub,
                visible=(i == 0),
                line={"color": _HUB_COLORS[i % len(_HUB_COLORS)], "width": 2},
                hovertemplate="%{x|%b %Y}: %{y:,.1f} M lbs<extra></extra>",
            )
        )
        label, wape, mape = _hub_accuracy(data, hub)
        titles.append(
            f"{hub} monthly FedEx freight - best backtest model: {label} "
            f"(WAPE {wape:.1%}, MAPE {mape:.1%})"
        )

    buttons = []
    for i, hub in enumerate(hubs):
        visible = [j == i for j in range(len(hubs))]
        buttons.append(
            {
                "label": hub,
                "method": "update",
                "args": [{"visible": visible}, {"title.text": titles[i]}],
            }
        )

    _base_layout(fig, titles[0])
    fig.update_layout(
        showlegend=False,
        height=560,
        yaxis_title="Freight (M lbs)",
        xaxis_title="Month",
        # Extra top margin creates a dedicated control band above the title so
        # the hub-selector dropdown no longer overlaps the (long) title text.
        margin={"l": 70, "r": 30, "t": 130, "b": 50},
        # Pin the title just above the plotting area, in the lower part of the
        # top margin, leaving the band above it free for the selector controls.
        title={"y": 1.0, "yref": "paper", "yanchor": "bottom"},
        updatemenus=[
            {
                "buttons": buttons,
                "direction": "down",
                "showactive": True,
                "x": 0.0,
                "xanchor": "left",
                # Sits in the upper part of the top margin, clear of the title.
                "y": 1.26,
                "yanchor": "top",
                "pad": {"r": 10, "t": 0},
                "bgcolor": "#F2F2F2",
                "bordercolor": "#CCCCCC",
            }
        ],
        annotations=[
            {
                "text": "Select hub:",
                "showarrow": False,
                "x": 0.0,
                "xref": "paper",
                "y": 1.33,
                "yref": "paper",
                "xanchor": "left",
                "yanchor": "top",
                "font": {"size": 12, "color": "#555"},
            }
        ],
    )
    return fig


def build_break_figure(data: DashboardData) -> go.Figure:
    """Domestic vs international freight with break + monitoring-alert markers."""
    di = data.dom_intl
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=di["month"],
            y=di["fedex_domestic_freight"] / _LB_TO_M,
            mode="lines",
            name="Domestic",
            line={"color": _C_BLUE, "width": 2},
            hovertemplate="%{x|%b %Y}: %{y:,.0f} M lbs<extra>Domestic</extra>",
        )
    )
    fig.add_trace(
        go.Scatter(
            x=di["month"],
            y=di["fedex_intl_freight"] / _LB_TO_M,
            mode="lines",
            name="International",
            line={"color": _C_ORANGE, "width": 2},
            hovertemplate="%{x|%b %Y}: %{y:,.0f} M lbs<extra>International</extra>",
        )
    )

    break_ts = pd.Timestamp(_CERTIFIED_BREAK)
    alert_ts = pd.Timestamp(_MONITOR_ALERT)
    y_max = float(di["fedex_domestic_freight"].max() / _LB_TO_M)

    # Certified domestic structural break: solid reference line.
    fig.add_vline(
        x=break_ts.to_pydatetime(),
        line={"color": _C_GREEN, "width": 2, "dash": "dash"},
    )
    fig.add_annotation(
        x=break_ts,
        y=y_max,
        text="Certified domestic<br>structural break (2021-04)",
        showarrow=True,
        arrowhead=2,
        ax=-70,
        ay=-30,
        font={"color": _C_GREEN, "size": 11},
        align="center",
    )

    # Monitoring-flagged anomaly: annotated alert point on the domestic line.
    alert_val = float(
        di.loc[di["month"] == alert_ts, "fedex_domestic_freight"].iloc[0] / _LB_TO_M
    )
    fig.add_trace(
        go.Scatter(
            x=[alert_ts],
            y=[alert_val],
            mode="markers",
            name="Monitoring alert",
            marker={
                "color": _C_VERMILLION,
                "size": 13,
                "symbol": "star",
                "line": {"color": "#FFFFFF", "width": 1},
            },
            hovertemplate=(
                "%{x|%b %Y}: %{y:,.0f} M lbs<br>"
                "Monitoring alert - regime-change candidate<extra></extra>"
            ),
        )
    )
    fig.add_annotation(
        x=alert_ts,
        y=alert_val,
        text="Monitoring alert (2024-09)<br>regime-change candidate,<br>not a certified break",
        showarrow=True,
        arrowhead=2,
        ax=-40,
        ay=60,
        font={"color": _C_VERMILLION, "size": 11},
        align="center",
    )

    _base_layout(fig, "Domestic vs international freight: break & monitoring alert")
    fig.update_layout(
        height=520,
        yaxis_title="Freight (M lbs)",
        xaxis_title="Month",
    )
    return fig


def build_model_comparison_figure(data: DashboardData) -> go.Figure:
    """Grouped bar of backtest WAPE per method across every level."""
    acc = data.accuracy
    levels = ["network", *data.hubs_selected["hub"].tolist()]
    level_labels = [lvl.capitalize() if lvl == "network" else lvl for lvl in levels]
    fig = go.Figure()
    methods = ["sarima", "seasonal_naive_trend", "seasonal_naive"]
    for method in methods:
        sub = acc[acc["method"] == method].set_index("level")
        y = [
            float(sub.loc[lvl, "wape"]) * 100.0 if lvl in sub.index else None
            for lvl in levels
        ]
        fig.add_trace(
            go.Bar(
                x=level_labels,
                y=y,
                name=_METHOD_LABELS[method],
                marker_color=_METHOD_COLORS[method],
                hovertemplate="%{x} - %{fullData.name}: WAPE %{y:.2f}%<extra></extra>",
            )
        )
    _base_layout(fig, "Backtest accuracy: WAPE per method and level (lower is better)")
    fig.update_layout(
        barmode="group",
        height=480,
        yaxis_title="WAPE (%)",
        xaxis_title="Level",
    )
    return fig


def build_html(data: DashboardData) -> str:
    """Assemble the full self-contained dashboard HTML string.

    Plotly's JS is embedded inline in the first figure (``include_plotlyjs=
    True``) and reused by the rest, so the page has no external dependency.

    Args:
        data: Processed series.

    Returns:
        The complete HTML document as a string.
    """
    net_fig = build_network_figure(data)
    hub_fig = build_hub_figure(data)
    break_fig = build_break_figure(data)
    model_fig = build_model_comparison_figure(data)

    config = {"responsive": True, "displayModeBar": False}
    # Embed Plotly's JS once (inline) in the first div; the others reference it.
    divs = [
        net_fig.to_html(full_html=False, include_plotlyjs=True, config=config),
        hub_fig.to_html(full_html=False, include_plotlyjs=False, config=config),
        break_fig.to_html(full_html=False, include_plotlyjs=False, config=config),
        model_fig.to_html(full_html=False, include_plotlyjs=False, config=config),
    ]

    sections = [
        (
            "Network freight & market share",
            "FedEx monthly air-cargo freight and its share of all-carrier "
            "freight across 2011-2025.",
            divs[0],
        ),
        (
            "Hub freight trends",
            "Monthly FedEx freight by hub airport. Use the dropdown to switch "
            "hubs; the title shows that hub's best one-month-ahead backtest "
            "model and accuracy.",
            divs[1],
        ),
        (
            "Structural break & monitoring alert",
            "Domestic vs international freight. The dashed line marks the "
            "certified 2021-04 domestic structural break; the star marks the "
            "2024-09 monitoring alert - a regime-change candidate, not a "
            "certified structural break.",
            divs[2],
        ),
        (
            "Forecasting model comparison",
            "One-month-ahead rolling-backtest WAPE for each method at the "
            "network level and each hub. Lower is better.",
            divs[3],
        ),
    ]
    body_parts = []
    for title, blurb, div in sections:
        body_parts.append(
            f'<section class="card">'
            f"<h2>{title}</h2>"
            f'<p class="blurb">{blurb}</p>'
            f"{div}"
            f"</section>"
        )
    body = "\n".join(body_parts)

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FedEx Cargo Forecasting &amp; Monitoring</title>
<style>
  :root {{ color-scheme: light; }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    background: #F4F6F9;
    color: #1A1A1A;
    font-family: {_FONT_FAMILY};
    line-height: 1.5;
  }}
  header {{
    background: #1F3864;
    color: #FFFFFF;
    padding: 28px 16px;
  }}
  header .wrap {{ max-width: 1100px; margin: 0 auto; }}
  header h1 {{ margin: 0 0 6px 0; font-size: 1.6rem; }}
  header p {{ margin: 0; opacity: 0.9; font-size: 0.98rem; }}
  main {{ max-width: 1100px; margin: 0 auto; padding: 16px; }}
  .card {{
    background: #FFFFFF;
    border: 1px solid #E2E6EC;
    border-radius: 10px;
    padding: 18px 16px 8px 16px;
    margin: 18px 0;
    box-shadow: 0 1px 3px rgba(0,0,0,0.05);
  }}
  .card h2 {{ margin: 0 0 4px 0; font-size: 1.18rem; color: #1F3864; }}
  .blurb {{ margin: 0 0 10px 0; color: #52606D; font-size: 0.95rem; }}
  .plotly-graph-div {{ width: 100% !important; }}
  footer {{
    max-width: 1100px; margin: 0 auto; padding: 8px 16px 40px 16px;
    color: #7A8794; font-size: 0.85rem;
  }}
  @media (max-width: 640px) {{
    header h1 {{ font-size: 1.3rem; }}
    main {{ padding: 8px; }}
    .card {{ padding: 14px 10px 6px 10px; }}
  }}
</style>
</head>
<body>
<header>
  <div class="wrap">
    <h1>FedEx Cargo Forecasting &amp; Monitoring</h1>
    <p>Monthly air-cargo freight at the network level and major hubs, from U.S.
    Bureau of Transportation Statistics T-100 Segment data (2011-2025).</p>
  </div>
</header>
<main>
{body}
</main>
<footer>
  Freight is BTS T-100 Segment freight in pounds (shown here in millions of
  lbs). WAPE/MAPE are from a leakage-free one-month-ahead rolling backtest.
</footer>
</body>
</html>
"""


def write_dashboard(
    output_path: Path | None = None,
    processed_dir: Path | None = None,
    config: Config | None = None,
) -> Path:
    """Build and write the self-contained dashboard HTML.

    Args:
        output_path: Destination HTML path (default :data:`DEFAULT_OUTPUT`).
        processed_dir: Directory with the processed CSVs (default from config).
        config: Optional preloaded config.

    Returns:
        The path to the written HTML file.
    """
    config = config or get_config()
    output_path = output_path or DEFAULT_OUTPUT
    processed_dir = processed_dir or (PROJECT_ROOT / config.data.processed_dir)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    data = load_data(processed_dir, config)
    html = build_html(data)
    output_path.write_text(html, encoding="utf-8")
    size_mb = output_path.stat().st_size / 1.0e6
    logger.info(
        "Wrote self-contained dashboard (%.2f MB) to %s", size_mb, output_path
    )
    return output_path


def main() -> None:
    """Build the dashboard HTML."""
    setup_logging()
    write_dashboard()


if __name__ == "__main__":
    main()
