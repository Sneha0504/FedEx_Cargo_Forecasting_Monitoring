"""Typed configuration loading and logging setup.

Loads ``config/config.yaml`` into a frozen, typed :class:`Config` structure and
provides a :func:`setup_logging` helper that configures the stdlib ``logging``
module. No module in this project uses ``print``; all diagnostics go through
``logging``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

# Default location of the YAML config, resolved relative to the project root
# (the parent of the ``src`` package) so no absolute path is hard-coded.
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "config.yaml"


@dataclass(frozen=True)
class DataConfig:
    """Filesystem locations, filenames, and the analysis window.

    The trailing filename fields have defaults so existing callers that
    construct a :class:`DataConfig` with only the original arguments keep
    working; they name the multi-level backtest, break-detection, and
    monitoring outputs written by the analysis-results stage.
    """

    raw_dir: str
    raw_glob: str
    processed_dir: str
    network_series_file: str
    backtest_results_file: str
    start_year: int
    end_year: int
    hub_series_file: str = "hub_monthly.csv"
    network_domestic_intl_file: str = "network_domestic_intl.csv"
    backtest_accuracy_file: str = "backtest_accuracy.csv"
    backtest_forecasts_file: str = "backtest_forecasts.csv"
    break_analysis_file: str = "break_analysis.csv"
    monitoring_file: str = "monitoring.csv"


@dataclass(frozen=True)
class HubsConfig:
    """Hub-selection settings."""

    top_n: int


@dataclass(frozen=True)
class SarimaConfig:
    """SARIMA model orders."""

    order: tuple[int, int, int]
    seasonal_order: tuple[int, int, int, int]


@dataclass(frozen=True)
class ForecastingConfig:
    """Model hyper-parameters for the forecasting methods."""

    trend_window: int
    sarima: SarimaConfig


@dataclass(frozen=True)
class BacktestConfig:
    """Rolling-backtest window settings."""

    eval_months: int
    min_train_months: int


@dataclass(frozen=True)
class MonitoringConfig:
    """Forecast-error monitoring settings.

    ``plain_report_sigma`` is the control-limit multiple for the plain
    year-over-year volume review used as the lead-time baseline.
    """

    error_window: int
    threshold_sigma: float
    plain_report_sigma: float = 2.0


@dataclass(frozen=True)
class BreakConfig:
    """Structural-break (binary-segmentation sup-Chow) scan settings.

    ``trim_fraction`` is the fraction of each segment trimmed from both ends so
    the sup-F scan only considers candidate break months in its central portion
    (standard Andrews trimming). ``bootstrap_reps`` controls the residual
    bootstrap used for the sup-F p-value. Binary segmentation recurses into the
    sub-segments around each detected break, stopping when a segment is shorter
    than ``min_segment_months``, its bootstrap p-value is at or above
    ``significance_level``, or ``max_breaks`` breaks have been found.
    """

    trim_fraction: float = 0.15
    bootstrap_reps: int = 1000
    random_seed: int = 42
    max_breaks: int = 3
    min_segment_months: int = 24
    significance_level: float = 0.05
    block_length: int = 12


@dataclass(frozen=True)
class Config:
    """Top-level typed configuration for the pipeline."""

    data: DataConfig
    fedex_carrier_code: str
    hubs: HubsConfig
    forecasting: ForecastingConfig
    backtest: BacktestConfig
    monitoring: MonitoringConfig
    break_detection: BreakConfig = BreakConfig()


def get_config(path: str | Path | None = None) -> Config:
    """Load and validate the YAML config into a typed :class:`Config`.

    Args:
        path: Path to the YAML file. Defaults to :data:`DEFAULT_CONFIG_PATH`.

    Returns:
        A frozen :class:`Config` instance.

    Raises:
        FileNotFoundError: If the config file does not exist.
        KeyError: If a required section or key is missing.
    """
    config_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with config_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}

    sarima_raw = raw["forecasting"]["sarima"]
    order = tuple(int(v) for v in sarima_raw["order"])
    seasonal_order = tuple(int(v) for v in sarima_raw["seasonal_order"])

    data_raw = raw["data"]
    break_raw = raw.get("break", {}) or {}
    return Config(
        data=DataConfig(
            raw_dir=data_raw["raw_dir"],
            raw_glob=data_raw["raw_glob"],
            processed_dir=data_raw["processed_dir"],
            network_series_file=data_raw["network_series_file"],
            backtest_results_file=data_raw["backtest_results_file"],
            start_year=int(data_raw["start_year"]),
            end_year=int(data_raw["end_year"]),
            hub_series_file=str(data_raw.get("hub_series_file", "hub_monthly.csv")),
            network_domestic_intl_file=str(
                data_raw.get("network_domestic_intl_file", "network_domestic_intl.csv")
            ),
            backtest_accuracy_file=str(
                data_raw.get("backtest_accuracy_file", "backtest_accuracy.csv")
            ),
            backtest_forecasts_file=str(
                data_raw.get("backtest_forecasts_file", "backtest_forecasts.csv")
            ),
            break_analysis_file=str(
                data_raw.get("break_analysis_file", "break_analysis.csv")
            ),
            monitoring_file=str(data_raw.get("monitoring_file", "monitoring.csv")),
        ),
        fedex_carrier_code=str(raw["fedex_carrier_code"]),
        hubs=HubsConfig(top_n=int(raw["hubs"]["top_n"])),
        forecasting=ForecastingConfig(
            trend_window=int(raw["forecasting"]["trend_window"]),
            sarima=SarimaConfig(
                order=order,  # type: ignore[arg-type]
                seasonal_order=seasonal_order,  # type: ignore[arg-type]
            ),
        ),
        backtest=BacktestConfig(
            eval_months=int(raw["backtest"]["eval_months"]),
            min_train_months=int(raw["backtest"]["min_train_months"]),
        ),
        monitoring=MonitoringConfig(
            error_window=int(raw["monitoring"]["error_window"]),
            threshold_sigma=float(raw["monitoring"]["threshold_sigma"]),
            plain_report_sigma=float(
                raw["monitoring"].get("plain_report_sigma", 2.0)
            ),
        ),
        break_detection=BreakConfig(
            trim_fraction=float(break_raw.get("trim_fraction", 0.15)),
            bootstrap_reps=int(break_raw.get("bootstrap_reps", 1000)),
            random_seed=int(break_raw.get("random_seed", 42)),
            max_breaks=int(break_raw.get("max_breaks", 3)),
            min_segment_months=int(break_raw.get("min_segment_months", 24)),
            significance_level=float(break_raw.get("significance_level", 0.05)),
            block_length=int(break_raw.get("block_length", 12)),
        ),
    )


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    """Configure the stdlib root logger with a consistent format.

    Safe to call more than once: existing handlers are left untouched after the
    first configuration so repeated calls do not duplicate log output.

    Args:
        level: Logging level for the root logger (default ``logging.INFO``).

    Returns:
        The configured root logger.
    """
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    root = logging.getLogger()
    root.setLevel(level)
    return root
