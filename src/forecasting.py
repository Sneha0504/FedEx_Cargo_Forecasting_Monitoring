"""One-month-ahead forecasting methods as pure functions.

Contract shared by every method:

* Input ``train`` is a :class:`pandas.Series` of ``float`` values indexed by a
  monthly :class:`~pandas.DatetimeIndex` of month-start timestamps (``freq``
  "MS", i.e. the first of each month), sorted ascending.
* Output is a single ``float``: the forecast for the month immediately *after*
  the last training month (the "target month").

The functions are pure (no I/O, no global state) so they are directly testable
against hand calculations. ``METHODS`` maps a short method name to its callable.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

import pandas as pd
from pandas.tseries.offsets import DateOffset, MonthBegin

logger = logging.getLogger(__name__)

# Monthly seasonal period: 12 months in a year.
_MONTHS_PER_YEAR = 12


def _target_month(train: pd.Series) -> pd.Timestamp:
    """Return the month immediately after the last training month."""
    return train.index[-1] + MonthBegin(1)


def seasonal_naive(train: pd.Series) -> float:
    """Forecast the target month as the value from 12 months earlier.

    The target month is ``train.index[-1] + 1 month``; the forecast is the
    training value observed at ``target_month - 12 months`` (equivalently the
    value one calendar year before the month being forecast).

    Args:
        train: Monthly training series (see module contract).

    Returns:
        The forecast for the target month.

    Raises:
        ValueError: If the series has fewer than 12 months, or the required
            same-month-last-year observation is missing from the index.
    """
    if len(train) < _MONTHS_PER_YEAR:
        raise ValueError(
            f"seasonal_naive requires at least {_MONTHS_PER_YEAR} months of "
            f"history; got {len(train)}."
        )
    target = _target_month(train)
    lookup = target - DateOffset(months=_MONTHS_PER_YEAR)
    if lookup not in train.index:
        raise ValueError(
            f"seasonal_naive needs the value for {lookup.date()} "
            f"(12 months before the target month) but it is missing."
        )
    return float(train.loc[lookup])


def seasonal_naive_trend(train: pd.Series, trend_window: int = 3) -> float:
    """Seasonal naive plus an average year-over-year (YoY) trend term.

    Formula (exact, hand-checkable):

        base          = seasonal_naive(train)
                      = train[target_month - 12 months]
        yoy_change(m) = train[m] - train[m - 12 months]
        trend         = mean of yoy_change(m) over the most recent
                        ``trend_window`` training months
                        m in {last, last-1, ..., last-(trend_window-1)}
        forecast      = base + trend

    Intuition: carry last year's value for the same month forward, then nudge it
    by the recent average YoY change so a rising or falling level is tracked.

    Args:
        train: Monthly training series (see module contract).
        trend_window: Number of trailing months over which the YoY change is
            averaged (default 3).

    Returns:
        The forecast for the target month.

    Raises:
        ValueError: If ``trend_window`` is not positive, or the series is too
            short for the base forecast or for every required YoY lag.
    """
    if trend_window < 1:
        raise ValueError(f"trend_window must be >= 1; got {trend_window}.")

    base = seasonal_naive(train)

    last = train.index[-1]
    diffs: list[float] = []
    for k in range(trend_window):
        month = last - DateOffset(months=k)
        prior_year = month - DateOffset(months=_MONTHS_PER_YEAR)
        if month not in train.index or prior_year not in train.index:
            raise ValueError(
                f"seasonal_naive_trend needs values at {month.date()} and "
                f"{prior_year.date()} but one is missing; the series is too "
                f"short for trend_window={trend_window}."
            )
        diffs.append(float(train.loc[month]) - float(train.loc[prior_year]))

    trend = sum(diffs) / len(diffs)
    return base + trend


def _sarima_fallback(train: pd.Series, trend_window: int) -> float:
    """Fallback forecast when SARIMA cannot be fit.

    Per the project plan the primary fallback is ``seasonal_naive_trend`` (M2).
    If the series is too short for the trend window (so M2 cannot be computed),
    a secondary fallback to :func:`seasonal_naive` (M1) is used.
    """
    try:
        return seasonal_naive_trend(train, trend_window=trend_window)
    except ValueError as exc:
        logger.warning(
            "seasonal_naive_trend fallback unavailable (%s); "
            "using seasonal_naive instead.",
            exc,
        )
        return seasonal_naive(train)


def sarima_forecast(
    train: pd.Series,
    order: tuple[int, int, int] = (1, 1, 1),
    seasonal_order: tuple[int, int, int, int] = (0, 1, 1, 12),
    trend_window: int = 3,
) -> float:
    """One-step-ahead SARIMAX forecast, falling back to seasonal_naive_trend.

    Fits a statsmodels :class:`~statsmodels.tsa.statespace.sarimax.SARIMAX`
    model on ``train`` and returns the one-step-ahead forecast. The fit is only
    attempted when ``train`` has at least ``2 * s`` observations (``s`` is the
    seasonal period); with less history the fit is unreliable so the fallback is
    used directly. If the fit raises for any reason (non-convergence, singular
    matrix, etc.), a warning is logged and the fallback is used.

    The fallback is ``seasonal_naive_trend`` (method M2), matching the project
    plan; if the series is too short for the trend window, it falls back further
    to :func:`seasonal_naive` (M1). See :func:`_sarima_fallback`.

    Args:
        train: Monthly training series (see module contract).
        order: Non-seasonal ``(p, d, q)`` order.
        seasonal_order: Seasonal ``(P, D, Q, s)`` order; ``s`` is the period.
        trend_window: Trailing-month window passed to the ``seasonal_naive_trend``
            fallback.

    Returns:
        The forecast for the target month.
    """
    seasonal_period = seasonal_order[3]
    min_obs = 2 * seasonal_period
    if len(train) < min_obs:
        logger.warning(
            "SARIMA needs at least %d observations (2x the seasonal period) "
            "but got %d; falling back to seasonal_naive_trend.",
            min_obs,
            len(train),
        )
        return _sarima_fallback(train, trend_window)

    try:
        from statsmodels.tsa.statespace.sarimax import SARIMAX

        model = SARIMAX(
            train,
            order=order,
            seasonal_order=seasonal_order,
            enforce_stationarity=False,
            enforce_invertibility=False,
        )
        fitted = model.fit(disp=False)
        forecast = fitted.forecast(steps=1)
        return float(forecast.iloc[0])
    except Exception as exc:  # noqa: BLE001 - any fit failure must fall back safely
        logger.warning(
            "SARIMA fit failed (%s); falling back to seasonal_naive_trend.", exc
        )
        return _sarima_fallback(train, trend_window)


# Short name attributes so a method can be identified from its callable.
seasonal_naive.method_name = "seasonal_naive"  # type: ignore[attr-defined]
seasonal_naive_trend.method_name = "seasonal_naive_trend"  # type: ignore[attr-defined]
sarima_forecast.method_name = "sarima"  # type: ignore[attr-defined]

# Registry mapping method name -> callable with the module's (train) -> float
# contract. ``sarima`` is exposed with its default orders.
METHODS: dict[str, Callable[[pd.Series], float]] = {
    "seasonal_naive": seasonal_naive,
    "seasonal_naive_trend": seasonal_naive_trend,
    "sarima": sarima_forecast,
}
