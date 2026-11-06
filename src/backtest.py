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
    """Load the processed network series, run the backtest, and persist results.

    The aggregation step that writes the processed series is not part of this
    build, so when the file is absent this logs a clear message and returns
    without fabricating any data. When data is present, results are written to
    the configured backtest-results file so the monitoring step can read them.
    """
    setup_logging()
    config = get_config()
    processed_dir = Path(config.data.processed_dir)
    series_path = processed_dir / config.data.network_series_file

    if not series_path.exists():
        logger.info(
            "Processed data not found at %s; run the aggregation step first. "
            "Nothing to backtest.",
            series_path,
        )
        return

    frame = pd.read_csv(series_path, parse_dates=["month"], index_col="month")
    # Give the index an explicit month-start frequency. A CSV round-trip drops
    # the DatetimeIndex ``freq``, which makes statsmodels emit a "No frequency
    # information" ValueWarning on every SARIMA fit; setting it here keeps the
    # fits quiet. (sort first so ``asfreq`` has a monotonic index to work on.)
    series = frame.iloc[:, 0].astype(float).sort_index().asfreq("MS")
    results = rolling_backtest(
        series,
        _build_methods(config),
        min_train_months=config.backtest.min_train_months,
        eval_months=config.backtest.eval_months,
    )

    results_path = processed_dir / config.data.backtest_results_file
    results_path.parent.mkdir(parents=True, exist_ok=True)
    results.to_csv(results_path, index=False)

    table = accuracy_table(results)
    logger.info(
        "Backtest complete; results written to %s. Accuracy by method:\n%s",
        results_path,
        table.to_string(index=False),
    )


if __name__ == "__main__":
    main()
