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


def _months_between(later: pd.Timestamp, earlier: pd.Timestamp) -> int:
    """Whole-month difference ``later - earlier`` (positive if later is after)."""
    return (later.year - earlier.year) * 12 + (later.month - earlier.month)


def _monitor_flags_on_or_after(
    monitor: pd.DataFrame, anchor: pd.Timestamp
) -> list[pd.Timestamp]:
    """All flagged months at or after ``anchor`` (ascending; possibly empty)."""
    flagged = monitor[monitor["flagged"] & (monitor["month"] >= anchor)]
    return [pd.Timestamp(m) for m in sorted(flagged["month"].tolist())]


def _first_flag_on_or_after(
    monitor: pd.DataFrame, anchor: pd.Timestamp
) -> pd.Timestamp | None:
    """First flagged month at or after ``anchor`` (``None`` if none)."""
    flags = _monitor_flags_on_or_after(monitor, anchor)
    return flags[0] if flags else None


def _plain_report_flags(
    series: pd.Series,
    anchor: pd.Timestamp,
    sigma: float,
    window: int,
) -> list[pd.Timestamp]:
    """All months a plain YoY volume review breaches a lower control limit.

    This models a routine monthly review of the *same* (network) volume series
    the forecast-error monitor watches, using a standard control-limit rule on
    the 12-month year-over-year change rather than a forecast model:

        yoy[t]      = series[t] / series[t - 12] - 1
        mean[t]     = mean of the ``window`` yoy values strictly before t
        std[t]      = sample std (ddof=1) of those same prior yoy values
        lower[t]    = mean[t] - sigma * std[t]
        flagged[t]  = yoy[t] < lower[t]

    Only months on or after ``anchor`` are considered. Using prior-only
    statistics keeps the rule point-in-time (no hindsight), exactly as the
    forecast-error monitor is.

    Args:
        series: Monthly volume series indexed by a month-start ``DatetimeIndex``.
        anchor: Earliest month considered.
        sigma: Lower control-limit multiple (e.g. 2.0).
        window: Number of trailing prior YoY values for the mean/std.

    Returns:
        The breaching months on/after ``anchor``, ascending (possibly empty).
    """
    series = series.sort_index()
    yoy = series / series.shift(12) - 1.0
    prior = yoy.shift(1)
    mean = prior.rolling(window=window, min_periods=window).mean()
    std = prior.rolling(window=window, min_periods=window).std()
    lower = mean - sigma * std
    breached = ((yoy < lower) & (yoy.index >= anchor)).fillna(False)
    return [pd.Timestamp(m) for m in yoy.index[breached]]


def _plain_report_first_flag(
    series: pd.Series,
    anchor: pd.Timestamp,
    sigma: float,
    window: int,
) -> pd.Timestamp | None:
    """First month a plain YoY volume review breaches its lower control limit."""
    flags = _plain_report_flags(series, anchor, sigma, window)
    return flags[0] if flags else None


def main() -> None:
    """Run point-in-time monitoring on the best network model and report lead time.

    Steps:

    1. Load the multi-level backtest forecasts and accuracy, restrict to the
       ``network`` level, and pick the lowest-WAPE network method.
    2. Run :func:`rolling_error_monitor` forward across that method's one-step-
       ahead errors over the evaluation window and write ``monitoring.csv``.
    3. Anchor a lead-time episode on the monitor's OWN flagged anomaly (a fast
       operational detector) and compare it with when a plain year-over-year
       network-volume review would first flag (see
       :func:`_plain_report_first_flag`). This is one illustrative episode
       (n = 1), not an average. The Step-7 certified structural breaks
       (``break_analysis.csv``) are a conservative confirmer and are read only
       to state, honestly, that they precede the live-monitoring window and
       that the monitor's anomaly is not (yet) certified as a structural break.

    When a required input is absent this logs a clear message and returns
    without fabricating any data.
    """
    setup_logging()
    config = get_config()
    processed_dir = Path(config.data.processed_dir)
    forecasts_path = processed_dir / config.data.backtest_forecasts_file
    accuracy_path = processed_dir / config.data.backtest_accuracy_file

    if not forecasts_path.exists() or not accuracy_path.exists():
        logger.info(
            "Backtest output not found (%s / %s); run the backtest step first. "
            "Nothing to monitor.",
            forecasts_path,
            accuracy_path,
        )
        return

    forecasts = pd.read_csv(forecasts_path, parse_dates=["month"])
    accuracy = pd.read_csv(accuracy_path)

    network_acc = accuracy[accuracy["level"] == "network"].sort_values("wape")
    network_fc = forecasts[forecasts["level"] == "network"]
    if network_acc.empty or network_fc.empty:
        logger.info("No network-level backtest rows found; nothing to monitor.")
        return

    best_method = str(network_acc.iloc[0]["method"])
    best_results = network_fc[network_fc["method"] == best_method]
    logger.info(
        "Monitoring best NETWORK method by WAPE: %s (WAPE=%.4f).",
        best_method,
        float(network_acc.iloc[0]["wape"]),
    )

    monitor = rolling_error_monitor(
        best_results,
        window=config.monitoring.error_window,
        threshold_sigma=config.monitoring.threshold_sigma,
    )
    monitor_path = processed_dir / config.data.monitoring_file
    monitor_path.parent.mkdir(parents=True, exist_ok=True)
    monitor.to_csv(monitor_path, index=False)

    flagged_months = monitor.loc[monitor["flagged"], "month"]
    n_flagged = int(len(flagged_months))
    first_flag = (
        pd.Timestamp(flagged_months.min()) if n_flagged else None
    )
    logger.info(
        "Monitoring complete; %d month(s) flagged. Results written to %s.",
        n_flagged,
        monitor_path,
    )
    logger.info(
        "First month the monitor flags an anomaly: %s.",
        first_flag.date() if first_flag is not None else "none",
    )

    # Lead-time episode: anchored on the MONITOR'S OWN flagged anomaly (a fast
    # operational detector), not on the Step-7 structural break (a conservative
    # confirmer). The two are complementary: the monitor flags a regime-change
    # candidate as it happens; the break test only certifies a shift years
    # later. See docs/methodology.md.
    eval_start = pd.Timestamp(monitor["month"].min())
    eval_end = pd.Timestamp(monitor["month"].max())
    window = config.monitoring.error_window
    plain_sigma = config.monitoring.plain_report_sigma
    matched_sigma = config.monitoring.threshold_sigma

    network_path = processed_dir / config.data.network_series_file
    network_series = pd.read_csv(
        network_path, parse_dates=["month"], index_col="month"
    ).iloc[:, 0].astype(float)

    monitor_window_flags = _monitor_flags_on_or_after(monitor, eval_start)
    logger.info(
        "Forecast-error monitor flags within the %s..%s evaluation window: [%s].",
        eval_start.date(),
        eval_end.date(),
        ", ".join(m.date().isoformat() for m in monitor_window_flags) or "none",
    )

    if not monitor_window_flags:
        logger.info(
            "The monitor raised no anomaly in the evaluation window; no "
            "lead-time episode to report."
        )
    else:
        monitor_event = monitor_window_flags[0]
        plain_flag = _plain_report_first_flag(
            network_series, monitor_event, plain_sigma, window
        )
        logger.info(
            "Monitor's flagged anomaly (lead-time anchor): %s.",
            monitor_event.date(),
        )
        logger.info(
            "Plain YoY volume review (%.1f-sigma lower control limit on network "
            "YoY, %d-month trailing) first flags at/after the anomaly: %s.",
            plain_sigma,
            window,
            plain_flag.date() if plain_flag is not None else "none",
        )
        if plain_flag is None:
            logger.info(
                "Lead time undefined: the plain YoY review never confirmed the "
                "anomaly within the evaluation window."
            )
        else:
            lead_time = _months_between(plain_flag, monitor_event)
            logger.info(
                "LEAD TIME (one illustrative episode, n=1) = %d month(s): the "
                "monitor's anomaly (%s) is %d month(s) %s the plain YoY review's "
                "first flag (%s). (Positive = monitor earlier.)",
                lead_time,
                monitor_event.date(),
                abs(lead_time),
                "earlier than"
                if lead_time > 0
                else ("later than" if lead_time < 0 else "the same as"),
                plain_flag.date(),
            )

        # Full in-window flag set of the plain review, and the matched-sigma
        # comparison -- reported honestly in both directions.
        plain_window_flags = _plain_report_flags(
            network_series, eval_start, plain_sigma, window
        )
        logger.info(
            "Plain %.1f-sigma YoY review flags within the evaluation window: "
            "[%s].",
            plain_sigma,
            ", ".join(m.date().isoformat() for m in plain_window_flags) or "none",
        )
        earlier_plain = [m for m in plain_window_flags if m < monitor_event]
        if earlier_plain:
            logger.info(
                "The plain review also raises earlier flag(s) at [%s], before "
                "the monitor's anomaly, where the forecast-error monitor did "
                "NOT flag (no corresponding monitor alarm there).",
                ", ".join(m.date().isoformat() for m in earlier_plain),
            )
        plain_window_flags_matched = _plain_report_flags(
            network_series, eval_start, matched_sigma, window
        )
        logger.info(
            "At a matched %.1f-sigma, the plain YoY review flags within the "
            "evaluation window: [%s]; the forecast-error monitor flags [%s].",
            matched_sigma,
            ", ".join(m.date().isoformat() for m in plain_window_flags_matched)
            or "none",
            ", ".join(m.date().isoformat() for m in monitor_window_flags) or "none",
        )

    # Honest relationship to the Step-7 certified structural breaks. The break
    # test is a conservative confirmer; the monitor's anomaly is not called a
    # "break" here because the seasonality-aware test does not certify it.
    break_path = processed_dir / config.data.break_analysis_file
    if not break_path.exists():
        logger.info(
            "Break analysis not found at %s; cannot relate the anomaly to "
            "certified structural breaks.",
            break_path,
        )
        return

    breaks = pd.read_csv(break_path, parse_dates=["break_month"])
    significance = config.break_detection.significance_level
    certified = breaks[breaks["p_bootstrap_block"] < significance]
    dom_certified = certified[certified["series"] == "domestic"]["break_month"]
    intl_certified = certified[certified["series"] == "international"]["break_month"]
    logger.info(
        "Certified structural breaks (block-bootstrap p < %.3g): domestic [%s], "
        "international [%s]. All precede the %s..%s live-monitoring window, so "
        "the monitor could not have flagged them in real time.",
        significance,
        ", ".join(pd.Timestamp(m).date().isoformat() for m in sorted(dom_certified))
        or "none",
        ", ".join(pd.Timestamp(m).date().isoformat() for m in sorted(intl_certified))
        or "none",
        eval_start.date(),
        eval_end.date(),
    )

    if monitor_window_flags:
        # Magnitude of the monitor's flagged anomaly on the domestic series:
        # mean level over the post-anomaly months vs an equal-length prior
        # window. Reported as a visible drop, NOT a certified structural break.
        di_path = processed_dir / config.data.network_domestic_intl_file
        domestic_series = (
            pd.read_csv(di_path, parse_dates=["month"], index_col="month")[
                "fedex_domestic_freight"
            ]
            .astype(float)
            .sort_index()
        )
        post = domestic_series[domestic_series.index >= monitor_event]
        n_post = len(post)
        prior = domestic_series[domestic_series.index < monitor_event].iloc[-n_post:]
        drop_pct = (post.mean() - prior.mean()) / prior.mean() * 100.0
        logger.info(
            "The monitor's flagged anomaly at %s is a large visible domestic "
            "drop (%.1f%% over the %d post-months vs the prior %d months), but "
            "the conservative seasonality-aware break test does NOT certify it "
            "as structural on only ~%d post-months (~one seasonal cycle); it is "
            "a regime-change candidate, not a certified break.",
            monitor_event.date(),
            drop_pct,
            n_post,
            n_post,
            n_post,
        )


if __name__ == "__main__":
    main()
