"""Hand-calculated checks for the forecasting methods.

A 24-month synthetic monthly series (Jan 2020 .. Dec 2021) is constructed so the
seasonal-naive and seasonal-naive-plus-trend forecasts for Jan 2022 can be
worked out by hand and asserted exactly.
"""

from __future__ import annotations

import logging
import warnings

import numpy as np
import pandas as pd
import pytest

from src.forecasting import sarima_forecast, seasonal_naive, seasonal_naive_trend

# 24 monthly values, month-start index Jan 2020 .. Dec 2021.
#   2020 Jan..Dec: 100..111 (a steady +1 per month)
#   2021 Jan..Sep: 112..120 (same steady +1 per month)
#   2021 Oct, Nov, Dec: 130, 140, 150 (a deliberate late spike)
_VALUES = [
    100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111,  # 2020
    112, 113, 114, 115, 116, 117, 118, 119, 120, 130, 140, 150,  # 2021
]


def _make_series() -> pd.Series:
    index = pd.date_range("2020-01-01", periods=len(_VALUES), freq="MS")
    return pd.Series(_VALUES, index=index, dtype=float)


def test_seasonal_naive_hand_value() -> None:
    """Target month is Jan 2022; forecast is the value 12 months earlier.

    Jan 2022 - 12 months = Jan 2021, whose value is 112.
    """
    series = _make_series()
    assert seasonal_naive(series) == pytest.approx(112.0)


def test_seasonal_naive_trend_hand_value() -> None:
    """seasonal_naive (112) plus the mean YoY change over the last 3 months.

    YoY changes for the trailing 3 training months:
        Dec 2021 (150) - Dec 2020 (111) = 39
        Nov 2021 (140) - Nov 2020 (110) = 30
        Oct 2021 (130) - Oct 2020 (109) = 21
    mean = (39 + 30 + 21) / 3 = 30
    forecast = 112 + 30 = 142
    """
    series = _make_series()
    assert seasonal_naive_trend(series, trend_window=3) == pytest.approx(142.0)


def test_seasonal_naive_raises_on_short_series() -> None:
    """Fewer than 12 months of history must raise a clear ValueError."""
    index = pd.date_range("2020-01-01", periods=6, freq="MS")
    short = pd.Series([1, 2, 3, 4, 5, 6], index=index, dtype=float)
    with pytest.raises(ValueError):
        seasonal_naive(short)


def test_sarima_falls_back_to_trend_on_fit_failure(monkeypatch) -> None:
    """A failed SARIMA fit must fall back to seasonal_naive_trend (M2).

    The 24-month series clears the 2x-seasonal-period length guard, so the fit
    is attempted. We monkeypatch SARIMAX so constructing it raises; the result
    must equal the seasonal_naive_trend value exactly (142.0), proving the
    fallback is M2 and not plain seasonal_naive (which would be 112.0).
    """
    import statsmodels.tsa.statespace.sarimax as sarimax_module

    def _boom(*args: object, **kwargs: object) -> object:
        raise RuntimeError("forced SARIMA fit failure")

    monkeypatch.setattr(sarimax_module, "SARIMAX", _boom)

    series = _make_series()  # 24 months, clears the 2*s=24 length guard
    result = sarima_forecast(series, trend_window=3)

    expected = seasonal_naive_trend(series, trend_window=3)
    assert result == pytest.approx(expected)
    assert expected == pytest.approx(142.0)
    # Must NOT be the plain seasonal_naive value (112.0).
    assert result != pytest.approx(seasonal_naive(series))


def test_sarima_real_fit_is_reasonable_and_not_seasonal_naive(caplog) -> None:
    """A real SARIMA fit must demonstrably run, not silently fall back.

    This proves the fit two independent ways:

    (a) **Value divergence.** On a noisy trend+seasonal series the fitted
        one-step forecast is numerically far from the ``seasonal_naive_trend``
        (M2) fallback value, so a silent fallback could not produce it. (On the
        old noise-free series the SARIMA fit and the M2 value nearly coincided,
        so the test would have passed even if the fit never ran.)
    (b) **No fallback log.** Every fallback path in ``sarima_forecast`` logs a
        warning on the ``src.forecasting`` logger; a real fit logs nothing. We
        assert ``caplog`` captured no such warning during the call.

    The series uses a fixed seed so the result is deterministic, not flaky.
    """
    rng = np.random.default_rng(42)
    index = pd.date_range("2014-01-01", periods=72, freq="MS")
    t = np.arange(72, dtype=float)
    values = (
        100.0
        + 0.5 * t
        + 10.0 * np.sin(2.0 * np.pi * t / 12.0)
        + rng.normal(0.0, 4.0, size=72)
    )
    series = pd.Series(values, index=index, dtype=float)

    # M2 fallback value for the same series, computed the same way the fallback
    # would (default trend_window=3), so the comparison below is exact.
    m2_fallback = seasonal_naive_trend(series, trend_window=3)

    with caplog.at_level(logging.WARNING, logger="src.forecasting"):
        with warnings.catch_warnings():
            # statsmodels may emit a ConvergenceWarning here; it is a Python
            # warning, not a logging record, so silencing it does NOT touch the
            # fallback log record that assertion (b) depends on.
            warnings.simplefilter("ignore")
            result = sarima_forecast(series)

    assert np.isfinite(result)
    # Plausible band: within the observed range padded generously.
    assert series.min() - 30.0 < result < series.max() + 30.0
    # (a) The fitted value is clearly distinct from the M2 fallback value
    # (observed difference ~9 on this series), so no silent fallback produced
    # it. Tolerance of 1.0 is far below that gap but well above float noise.
    assert abs(result - m2_fallback) > 1.0
    assert result != pytest.approx(m2_fallback)
    # Also distinct from the plain seasonal_naive value.
    assert result != pytest.approx(seasonal_naive(series))
    # (b) No fallback occurred: a real fit logs no warning on this logger.
    fallback_records = [
        record
        for record in caplog.records
        if record.name == "src.forecasting" and record.levelno >= logging.WARNING
    ]
    assert not fallback_records, (
        "sarima_forecast logged a fallback/warning, so the real fit did not "
        f"run: {[record.getMessage() for record in fallback_records]}"
    )
