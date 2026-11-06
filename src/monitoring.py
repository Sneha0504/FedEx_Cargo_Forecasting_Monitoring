"""Point-in-time forecast-error monitoring.

Maintains a rolling "normal range" for forecast errors and flags months whose
error falls outside it. The range for any month uses **only** errors strictly
before that month, so a flag could have been raised in production at that
month with no hindsight. The engine is pure and testable; ``main`` wires it to
backtest output on disk and exits cleanly when that output is absent.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

from src.config import get_config, setup_logging

logger = logging.getLogger(__name__)

# Output column order.
_MONITOR_COLUMNS = [
    "month",
    "error",
    "roll_mean",
    "roll_std",
    "lower",
    "upper",
    "flagged",
]

def _coerce_errors(data: pd.Series | pd.DataFrame) -> pd.Series:
    """Coerce assorted inputs into an error Series indexed by month.

    Accepts, in order of preference:

    * a :class:`pandas.Series` of errors (its index is taken as the month);
    * a :class:`pandas.DataFrame` with an ``error`` column;
    * a :class:`pandas.DataFrame` with ``actual`` and ``forecast`` columns,
      from which ``error = actual - forecast`` is computed.

    If a ``month`` column is present it becomes the index.
    """
    if isinstance(data, pd.Series):
        return data

    if isinstance(data, pd.DataFrame):
        frame = data.copy()
        if "error" in frame.columns:
            errors = frame["error"].astype(float)
        elif "actual" in frame.columns and "forecast" in frame.columns:
            errors = (frame["actual"] - frame["forecast"]).astype(float)
        else:
            raise ValueError(
                "DataFrame must have an 'error' column or both 'actual' and "
                "'forecast' columns."
            )
        if "month" in frame.columns:
            errors.index = pd.DatetimeIndex(frame["month"])
        return errors

    raise TypeError(f"Unsupported input type for monitoring: {type(data)!r}")


def rolling_error_monitor(
    results_df_or_errors: pd.Series | pd.DataFrame,
    window: int,
    threshold_sigma: float,
) -> pd.DataFrame:
    """Flag forecast errors that leave a trailing rolling normal range.

    Windowing (exact, hand-checkable). For month ``t`` with position ``i`` in
    the time-sorted error series, the normal range is built from the ``window``
    errors immediately before ``t`` -- positions ``i-window .. i-1`` -- and
    never includes the error at ``t`` itself (implemented as a one-step shift
    before the rolling window):

        roll_mean[t] = mean(error[i-window : i])
        roll_std[t]  = sample std (ddof=1) of error[i-window : i]
        lower[t]     = roll_mean[t] - threshold_sigma * roll_std[t]
        upper[t]     = roll_mean[t] + threshold_sigma * roll_std[t]
        flagged[t]   = (error[t] < lower[t]) or (error[t] > upper[t])

    The first ``window`` months have fewer than ``window`` prior errors, so
    their range is undefined (NaN) and ``flagged`` is ``False``.

    Args:
        results_df_or_errors: Backtest results (``error`` column, or ``actual``
            and ``forecast`` columns) or a Series of errors indexed by month.
        window: Number of trailing prior errors defining the normal range.
        threshold_sigma: Multiple of the rolling std for the range half-width.

    Returns:
        A DataFrame with columns ``month``, ``error``, ``roll_mean``,
        ``roll_std``, ``lower``, ``upper`` and ``flagged`` (bool), in month
        order.

    Raises:
        ValueError: If ``window`` is less than 2 (a window of 1 makes the
            sample std always NaN), ``threshold_sigma`` is not positive, or the
            error stream has duplicate months (e.g. a multi-method backtest
            frame that was not filtered to a single method).
    """
    if window < 2:
        raise ValueError(f"window must be >= 2; got {window}.")
    if threshold_sigma <= 0:
        raise ValueError(f"threshold_sigma must be > 0; got {threshold_sigma}.")

    errors = _coerce_errors(results_df_or_errors).astype(float).sort_index()
    if errors.index.has_duplicates:
        raise ValueError(
            "error stream has duplicate months; monitoring requires one error "
            "per month. If this came from a multi-method backtest, filter to a "
            "single method before monitoring."
        )

    prior = errors.shift(1)
    roll_mean = prior.rolling(window=window, min_periods=window).mean()
    roll_std = prior.rolling(window=window, min_periods=window).std()
    lower = roll_mean - threshold_sigma * roll_std
    upper = roll_mean + threshold_sigma * roll_std
    flagged = ((errors < lower) | (errors > upper)).fillna(False)

    monitor = pd.DataFrame(
        {
            "month": errors.index,
            "error": errors.to_numpy(),
            "roll_mean": roll_mean.to_numpy(),
            "roll_std": roll_std.to_numpy(),
            "lower": lower.to_numpy(),
            "upper": upper.to_numpy(),
            "flagged": flagged.to_numpy(),
        },
        columns=_MONITOR_COLUMNS,
    )
    return monitor.reset_index(drop=True)


def main() -> None:
    """Load persisted backtest results and run the error monitor.

    The backtest step that writes these results depends on processed data that
    is not part of this build, so when the file is absent this logs a clear
    message and returns without fabricating any data.

    Backtest output holds one row per month *per method*. The monitor needs a
    single error per month, so results are filtered to the best method (lowest
    WAPE) before monitoring; otherwise same-month rows from different methods
    would interleave and corrupt the trailing window.
    """
    setup_logging()
    config = get_config()
    results_path = Path(config.data.processed_dir) / config.data.backtest_results_file

    if not results_path.exists():
        logger.info(
            "Backtest output not found at %s; run the backtest step first. "
            "Nothing to monitor.",
            results_path,
        )
        return

    from src.backtest import accuracy_table

    results = pd.read_csv(results_path, parse_dates=["month"])
    table = accuracy_table(results)
    best_method = str(table.loc[0, "method"])
    best_results = results[results["method"] == best_method]
    logger.info(
        "Monitoring best method by WAPE: %s (WAPE=%.4f).",
        best_method,
        float(table.loc[0, "wape"]),
    )

    monitor = rolling_error_monitor(
        best_results,
        window=config.monitoring.error_window,
        threshold_sigma=config.monitoring.threshold_sigma,
    )
    flagged = int(monitor["flagged"].sum())
    logger.info("Monitoring complete; %d month(s) flagged.", flagged)


if __name__ == "__main__":
    main()
