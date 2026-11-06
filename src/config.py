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
    """Filesystem locations and filenames for raw and processed data."""

    raw_dir: str
    processed_dir: str
    network_series_file: str
    backtest_results_file: str


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
    """Forecast-error monitoring settings."""

    error_window: int
    threshold_sigma: float


@dataclass(frozen=True)
class Config:
    """Top-level typed configuration for the pipeline."""

    data: DataConfig
    hubs: HubsConfig
    forecasting: ForecastingConfig
    backtest: BacktestConfig
    monitoring: MonitoringConfig


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

    return Config(
        data=DataConfig(
            raw_dir=raw["data"]["raw_dir"],
            processed_dir=raw["data"]["processed_dir"],
            network_series_file=raw["data"]["network_series_file"],
            backtest_results_file=raw["data"]["backtest_results_file"],
        ),
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
