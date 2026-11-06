"""Hand-calculated and behavioral checks for the error monitor.

(a) The rolling normal-range math (roll_mean, roll_std, lower, upper) is
    asserted exactly on a small constructed error series.
(b) A planted spike after a stable period is flagged, while ordinary small
    variation in the stable region is not.
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.monitoring import rolling_error_monitor


def _errors_frame(values: list[float], start: str = "2020-01-01") -> pd.DataFrame:
    """Build a month/error DataFrame with a month-start index."""
    months = pd.date_range(start, periods=len(values), freq="MS")
    return pd.DataFrame({"month": months, "error": values})


def test_rolling_math_exact() -> None:
    """Check roll_mean/roll_std/lower/upper against hand calculations.

    errors = [2, 4, 6, 5, 100], window = 3, threshold_sigma = 2.
    The range at month t uses the 3 errors strictly before t.

    Month index 3 (error 5): prior window [2, 4, 6]
        mean = 4
        std (ddof=1) = sqrt(((2-4)^2 + (4-4)^2 + (6-4)^2)/2) = sqrt(8/2) = 2
        lower = 4 - 2*2 = 0 ; upper = 4 + 2*2 = 8 ; 5 within -> not flagged
    Month index 4 (error 100): prior window [4, 6, 5]
        mean = 5
        std (ddof=1) = sqrt(((4-5)^2 + (6-5)^2 + (5-5)^2)/2) = sqrt(2/2) = 1
        lower = 5 - 2 = 3 ; upper = 5 + 2 = 7 ; 100 > 7 -> flagged
    The first 3 months lack a full prior window -> NaN range, not flagged.
    """
    frame = _errors_frame([2.0, 4.0, 6.0, 5.0, 100.0])
    monitor = rolling_error_monitor(frame, window=3, threshold_sigma=2.0)

    # First three rows have no full prior window.
    assert monitor.loc[0:2, "roll_mean"].isna().all()
    assert not monitor.loc[0:2, "flagged"].any()

    row3 = monitor.loc[3]
    assert row3["roll_mean"] == pytest.approx(4.0)
    assert row3["roll_std"] == pytest.approx(2.0)
    assert row3["lower"] == pytest.approx(0.0)
    assert row3["upper"] == pytest.approx(8.0)
    assert not bool(row3["flagged"])

    row4 = monitor.loc[4]
    assert row4["roll_mean"] == pytest.approx(5.0)
    assert row4["roll_std"] == pytest.approx(1.0)
    assert row4["lower"] == pytest.approx(3.0)
    assert row4["upper"] == pytest.approx(7.0)
    assert bool(row4["flagged"])


def test_flags_planted_anomaly_not_normal_variation() -> None:
    """A spike after a stable period is flagged; normal variation is not.

    errors = [5, 6, 5, 6, 5, 6, 80, 5], window = 4, threshold_sigma = 3.
    Index 6 (error 80): prior window [5, 6, 5, 6], mean 5.5, std ~0.577,
        upper ~= 5.5 + 3*0.577 = 7.23 -> 80 is flagged.
    Index 5 (error 6): prior window [6, 5, 6, 5], same range -> 6 within,
        not flagged.
    """
    frame = _errors_frame([5.0, 6.0, 5.0, 6.0, 5.0, 6.0, 80.0, 5.0])
    monitor = rolling_error_monitor(frame, window=4, threshold_sigma=3.0)

    assert bool(monitor.loc[6, "flagged"]) is True
    assert bool(monitor.loc[5, "flagged"]) is False
    # Exactly one month flagged (the spike).
    assert int(monitor["flagged"].sum()) == 1


def test_accepts_actual_forecast_frame() -> None:
    """Error is derived as actual - forecast when no 'error' column is given."""
    months = pd.date_range("2020-01-01", periods=4, freq="MS")
    frame = pd.DataFrame(
        {
            "month": months,
            "actual": [10.0, 12.0, 11.0, 13.0],
            "forecast": [10.0, 11.0, 12.0, 12.0],
        }
    )
    monitor = rolling_error_monitor(frame, window=2, threshold_sigma=3.0)
    # error = [0, 1, -1, 1]
    assert list(monitor["error"]) == [0.0, 1.0, -1.0, 1.0]


def test_duplicate_months_raise() -> None:
    """A multi-method (duplicate-month) error stream must raise a ValueError.

    Two methods share the same three months, so after sorting the same-month
    rows interleave and the trailing window is no longer strictly earlier than
    the month. The monitor must reject this rather than silently mix methods.
    """
    months = pd.date_range("2020-01-01", periods=3, freq="MS")
    frame = pd.DataFrame(
        {
            "month": list(months) + list(months),
            "method": ["m1", "m1", "m1", "m2", "m2", "m2"],
            "error": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0],
        }
    )
    with pytest.raises(ValueError):
        rolling_error_monitor(frame, window=2, threshold_sigma=3.0)


def test_invalid_window_and_threshold_raise() -> None:
    """window < 2 and threshold_sigma <= 0 must each raise a clear ValueError."""
    frame = _errors_frame([1.0, 2.0, 3.0, 4.0])
    with pytest.raises(ValueError):
        rolling_error_monitor(frame, window=1, threshold_sigma=3.0)
    with pytest.raises(ValueError):
        rolling_error_monitor(frame, window=3, threshold_sigma=0.0)
