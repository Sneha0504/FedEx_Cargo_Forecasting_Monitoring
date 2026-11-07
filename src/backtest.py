"""Rolling one-step-ahead backtest engine and accuracy metrics.

The engine is pure and testable: :func:`rolling_backtest` never passes any data
at or after a target month to a forecasting method, so there is no look-ahead
leakage. ``main`` wires the engine to processed data on disk, but exits cleanly
with a log message when that data does not yet exist.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from functools import partial
from pathlib import Path

import numpy as np
import pandas as pd

from src.config import get_config, setup_logging

logger = logging.getLogger(__name__)

# Column order for the backtest results frame.
_RESULT_COLUMNS = ["month", "method", "forecast", "actual"]


def rolling_backtest(
    series: pd.Series,
    methods: Mapping[str, Callable[[pd.Series], float]],
    min_train_months: int,
    eval_months: int,
) -> pd.DataFrame:
    """Run a rolling, expanding-window one-step-ahead backtest.

    For each of the last ``eval_months`` target months in ``series``, every
    method is fit on an expanding training window containing **only** the series
    values strictly before that target month. A target month is evaluated only
    once at least ``min_train_months`` of history precede it.

    No value at or after a target month is ever passed to a method, so the
    backtest is leakage-free by construction.

    Args:
        series: Monthly series indexed by a month-start ``DatetimeIndex``.
        methods: Mapping of method name -> forecasting callable following the
            ``(train) -> float`` contract in :mod:`src.forecasting`.
        min_train_months: Minimum months of history required before the first
            forecast.
        eval_months: Number of most-recent months to use as forecast targets.

    Returns:
        A DataFrame with columns ``month`` (the forecast target month),
        ``method``, ``forecast`` and ``actual``. Empty (with the correct
        columns) if no target month has enough history.

    Raises:
        ValueError: If ``eval_months`` or ``min_train_months`` is less than 1,
            or the series index has duplicate months.
    """
    if eval_months < 1:
        raise ValueError(f"eval_months must be >= 1; got {eval_months}.")
    if min_train_months < 1:
        raise ValueError(f"min_train_months must be >= 1; got {min_train_months}.")

    series = series.sort_index()
    if series.index.has_duplicates:
        raise ValueError(
            "series index has duplicate months; the backtest requires one "
            "observation per month."
        )

    # eval_months >= 1 here, so this slice selects exactly the last
    # ``eval_months`` target months (never the whole index).
    target_months = series.index[-eval_months:]

    rows: list[dict[str, object]] = []
    for target in target_months:
        train = series[series.index < target]
        if len(train) < min_train_months:
            continue
        actual = float(series.loc[target])
        for name, method in methods.items():
            forecast = float(method(train))
            rows.append(
                {
                    "month": target,
                    "method": name,
                    "forecast": forecast,
                    "actual": actual,
                }
            )

    return pd.DataFrame(rows, columns=_RESULT_COLUMNS)


def wape(actual: object, forecast: object) -> float:
    """Weighted absolute percentage error.

    WAPE = sum(|actual - forecast|) / sum(|actual|), returned as a fraction
    (not multiplied by 100). Robust to individual zero actuals. Returns NaN if
    every actual is zero.
    """
    actual_arr = np.asarray(actual, dtype=float)
    forecast_arr = np.asarray(forecast, dtype=float)
    denom = np.abs(actual_arr).sum()
    if denom == 0:
        return float("nan")
    return float(np.abs(actual_arr - forecast_arr).sum() / denom)


def mape(actual: object, forecast: object) -> float:
    """Mean absolute percentage error.

    MAPE = mean(|actual - forecast| / |actual|) over entries with a non-zero
    actual, returned as a fraction (not multiplied by 100). Returns NaN if every
    actual is zero.
    """
    actual_arr = np.asarray(actual, dtype=float)
    forecast_arr = np.asarray(forecast, dtype=float)
    mask = actual_arr != 0
    if not mask.any():
        return float("nan")
    return float(
        np.mean(
            np.abs(
                (actual_arr[mask] - forecast_arr[mask]) / actual_arr[mask]
            )
        )
    )


def accuracy_table(results_df: pd.DataFrame) -> pd.DataFrame:
    """Compute WAPE and MAPE per method from backtest results.

    Args:
        results_df: Output of :func:`rolling_backtest`.

    Returns:
        A DataFrame with columns ``method``, ``wape`` and ``mape``, sorted by
        ascending WAPE (best method first).
    """
    rows: list[dict[str, object]] = []
    for method, group in results_df.groupby("method"):
        rows.append(
            {
                "method": method,
                "wape": wape(group["actual"], group["forecast"]),
                "mape": mape(group["actual"], group["forecast"]),
            }
        )
    table = pd.DataFrame(rows, columns=["method", "wape", "mape"])
    return table.sort_values("wape").reset_index(drop=True)


def backtest_level(
    series: pd.Series,
    level: str,
    methods: Mapping[str, Callable[[pd.Series], float]],
    min_train_months: int,
    eval_months: int,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Backtest one labelled level and return its forecasts and accuracy.

    Args:
        series: Monthly series for this level, indexed by a month-start
            ``DatetimeIndex`` (frequency is set to ``"MS"`` internally).
        level: Label for this level (``"network"`` or a hub code such as
            ``"MEM"``); written into a ``level`` column on both frames.
        methods: Method registry (see :func:`rolling_backtest`).
        min_train_months: Minimum months of history before the first forecast.
        eval_months: Number of most-recent months used as forecast targets.

    Returns:
        A ``(forecasts, accuracy)`` tuple. ``forecasts`` has columns
        ``level, month, method, forecast, actual``; ``accuracy`` has columns
        ``level, method, wape, mape`` (one row per method, best WAPE first).
    """
    series = series.astype(float).sort_index().asfreq("MS")
    results = rolling_backtest(
        series,
        methods,
        min_train_months=min_train_months,
        eval_months=eval_months,
    )

    forecasts = results.copy()
    forecasts.insert(0, "level", level)

    accuracy = accuracy_table(results)
    accuracy.insert(0, "level", level)

    return forecasts, accuracy


def _load_hub_series(hub_path: Path) -> dict[str, pd.Series]:
    """Load ``hub_monthly.csv`` into one month-indexed series per hub.

    Args:
        hub_path: Path to the long-format hub series file with columns
            ``month, hub, fedex_freight``.

    Returns:
        An ordered mapping of hub code -> monthly ``fedex_freight`` series,
        indexed by a month-start ``DatetimeIndex``. Hubs are ordered by total
        historical freight (largest first) for stable, readable logs.
    """
    frame = pd.read_csv(hub_path, parse_dates=["month"])
    order = (
        frame.groupby("hub")["fedex_freight"].sum().sort_values(ascending=False).index
    )
    series_by_hub: dict[str, pd.Series] = {}
    for hub in order:
        hub_frame = frame[frame["hub"] == hub].sort_values("month")
        series = pd.Series(
            hub_frame["fedex_freight"].to_numpy(dtype=float),
            index=pd.DatetimeIndex(hub_frame["month"]),
            name=hub,
        )
        series_by_hub[str(hub)] = series
    return series_by_hub


def _build_methods(
    config: object,
) -> dict[str, Callable[[pd.Series], float]]:
    """Build the method registry with hyper-parameters bound from config.

    Function defaults are preserved in :mod:`src.forecasting` so the pure
    functions stay directly callable in tests; here the configured values are
    bound via :func:`functools.partial` so the backtest uses them.
    """
    from src.forecasting import sarima_forecast, seasonal_naive, seasonal_naive_trend

    fc = config.forecasting  # type: ignore[attr-defined]
    return {
        "seasonal_naive": seasonal_naive,
        "seasonal_naive_trend": partial(
            seasonal_naive_trend, trend_window=fc.trend_window
        ),
        "sarima": partial(
            sarima_forecast,
            order=fc.sarima.order,
            seasonal_order=fc.sarima.seasonal_order,
            trend_window=fc.trend_window,
        ),
    }


def main() -> None:
    """Run the multi-level rolling backtest and persist the results.

    The network series (``network_monthly.csv``) and every qualifying hub
    (one series per hub in ``hub_monthly.csv``) are each backtested with the
    same leakage-free rolling engine and the same three methods. Two files are
    written to the processed directory:

    * ``backtest_accuracy.csv`` -- ``level, method, wape, mape`` (one row per
      level x method), and
    * ``backtest_forecasts.csv`` -- ``level, month, method, forecast, actual``
      (every per-month one-step-ahead forecast, needed by the dashboard).

    The network-only results are also written to the configured
    ``backtest_results_file`` for continuity. Each level's WAPE-winning method
    is logged. When the processed series are absent this logs a clear message
    and returns without fabricating any data.
    """
    setup_logging()
    config = get_config()
    processed_dir = Path(config.data.processed_dir)
    network_path = processed_dir / config.data.network_series_file
    hub_path = processed_dir / config.data.hub_series_file

    if not network_path.exists():
        logger.info(
            "Processed data not found at %s; run the aggregation step first. "
            "Nothing to backtest.",
            network_path,
        )
        return

    methods = _build_methods(config)
    min_train_months = config.backtest.min_train_months
    eval_months = config.backtest.eval_months

    # Collect each level's series: network first, then each hub (if present).
    network_frame = pd.read_csv(network_path, parse_dates=["month"], index_col="month")
    levels: list[tuple[str, pd.Series]] = [
        ("network", network_frame.iloc[:, 0]),
    ]
    if hub_path.exists():
        for hub, series in _load_hub_series(hub_path).items():
            levels.append((hub, series))
    else:
        logger.warning(
            "Hub series not found at %s; backtesting the network level only.",
            hub_path,
        )

    forecast_frames: list[pd.DataFrame] = []
    accuracy_frames: list[pd.DataFrame] = []
    for level, series in levels:
        forecasts, accuracy = backtest_level(
            series,
            level,
            methods,
            min_train_months=min_train_months,
            eval_months=eval_months,
        )
        forecast_frames.append(forecasts)
        accuracy_frames.append(accuracy)
        best = accuracy.iloc[0]
        logger.info(
            "Level %-7s best method by WAPE: %-20s (WAPE=%.4f, MAPE=%.4f).",
            level,
            str(best["method"]),
            float(best["wape"]),
            float(best["mape"]),
        )

    all_forecasts = pd.concat(forecast_frames, ignore_index=True)
    all_accuracy = pd.concat(accuracy_frames, ignore_index=True)

    processed_dir.mkdir(parents=True, exist_ok=True)
    forecasts_path = processed_dir / config.data.backtest_forecasts_file
    accuracy_path = processed_dir / config.data.backtest_accuracy_file
    all_forecasts.to_csv(forecasts_path, index=False)
    all_accuracy.to_csv(accuracy_path, index=False)

    # Network-only results, kept for continuity with the original output file.
    network_results = all_forecasts[all_forecasts["level"] == "network"][
        _RESULT_COLUMNS
    ]
    network_results.to_csv(processed_dir / config.data.backtest_results_file, index=False)

    logger.info(
        "Multi-level backtest complete across %d level(s): %s. "
        "Accuracy -> %s; forecasts -> %s.",
        len(levels),
        ", ".join(level for level, _ in levels),
        accuracy_path,
        forecasts_path,
    )


if __name__ == "__main__":
    main()
