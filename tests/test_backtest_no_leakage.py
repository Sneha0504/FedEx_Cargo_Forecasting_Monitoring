"""Direct test that the backtest never feeds a method data at/after the target.

Rather than inspecting the engine, a spy forecasting method records the maximum
timestamp of every training series it is handed. After running the backtest we
assert that each recorded max training timestamp is strictly before the target
month that produced it.
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.backtest import accuracy_table, mape, rolling_backtest, wape


def test_rolling_backtest_has_no_future_leakage() -> None:
    """Every training series must end strictly before its target month."""
    # 72 months of monthly data: Jan 2015 .. Dec 2020.
    index = pd.date_range("2015-01-01", periods=72, freq="MS")
    series = pd.Series(range(100, 100 + len(index)), index=index, dtype=float)

    recorded_max_train: list[pd.Timestamp] = []

    def spy(train: pd.Series) -> float:
        """Record the latest timestamp seen in training; return a dummy value."""
        recorded_max_train.append(train.index.max())
        return float(train.iloc[-1])

    min_train_months = 48
    eval_months = 12
    results = rolling_backtest(
        series,
        {"spy": spy},
        min_train_months=min_train_months,
        eval_months=eval_months,
    )

    # One call (and one result row) per evaluated target month.
    assert len(results) == eval_months
    assert len(recorded_max_train) == len(results)

    # Direct leakage check: max training timestamp < its target month, for each.
    for max_train, target in zip(recorded_max_train, results["month"]):
        assert max_train < target


def test_min_train_months_gate() -> None:
    """Targets without enough preceding history are not evaluated."""
    index = pd.date_range("2015-01-01", periods=60, freq="MS")
    series = pd.Series(range(60), index=index, dtype=float)

    seen_lengths: list[int] = []

    def spy(train: pd.Series) -> float:
        seen_lengths.append(len(train))
        return 0.0

    # eval window asks for 24 targets but only those with >= 50 prior months
    # may be forecast: targets at positions 50..59 -> 10 of them.
    results = rolling_backtest(
        series, {"spy": spy}, min_train_months=50, eval_months=24
    )
    assert len(results) == 10
    assert min(seen_lengths) >= 50


def test_eval_months_and_min_train_months_validated() -> None:
    """eval_months or min_train_months below 1 must raise a clear ValueError.

    eval_months=0 previously selected the whole index via ``index[-0:]``; it
    must now raise instead of evaluating all 72 months.
    """
    index = pd.date_range("2015-01-01", periods=72, freq="MS")
    series = pd.Series(range(72), index=index, dtype=float)

    def spy(train: pd.Series) -> float:
        return 0.0

    with pytest.raises(ValueError):
        rolling_backtest(series, {"spy": spy}, min_train_months=12, eval_months=0)
    with pytest.raises(ValueError):
        rolling_backtest(series, {"spy": spy}, min_train_months=0, eval_months=12)


def test_eval_months_respected_exactly() -> None:
    """Exactly eval_months target rows are produced (not the whole index)."""
    index = pd.date_range("2015-01-01", periods=72, freq="MS")
    series = pd.Series(range(72), index=index, dtype=float)

    def spy(train: pd.Series) -> float:
        return 0.0

    results = rolling_backtest(
        series, {"spy": spy}, min_train_months=12, eval_months=1
    )
    assert len(results) == 1
    assert results.loc[0, "month"] == index[-1]


def test_duplicate_index_raises() -> None:
    """A series with duplicate months must raise a clear ValueError."""
    index = pd.DatetimeIndex(
        ["2020-01-01", "2020-02-01", "2020-02-01", "2020-03-01"]
    )
    series = pd.Series([1.0, 2.0, 3.0, 4.0], index=index)

    def spy(train: pd.Series) -> float:
        return 0.0

    with pytest.raises(ValueError):
        rolling_backtest(series, {"spy": spy}, min_train_months=1, eval_months=2)


def test_wape_and_mape_hand_values() -> None:
    """WAPE and MAPE on a tiny constructed set of actuals/forecasts."""
    actual = [100.0, 200.0]
    forecast = [90.0, 230.0]
    # |diff| = [10, 30]; sum = 40; sum|actual| = 300 -> WAPE = 40/300.
    assert wape(actual, forecast) == pytest.approx(40.0 / 300.0)
    # per-point APE = [0.10, 0.15]; mean = 0.125.
    assert mape(actual, forecast) == pytest.approx(0.125)


def test_accuracy_table_sorted_by_wape() -> None:
    """accuracy_table returns one row per method, best WAPE first."""
    results = pd.DataFrame(
        {
            "month": pd.to_datetime(["2020-01-01", "2020-01-01"]),
            "method": ["good", "bad"],
            "forecast": [100.0, 150.0],
            "actual": [100.0, 100.0],
        }
    )
    table = accuracy_table(results)
    assert list(table["method"]) == ["good", "bad"]
    assert table.loc[0, "wape"] == 0.0
