"""Tests for the binary-segmentation sup-Chow structural-break detector.

The main test plants TWO known level shifts in an otherwise-stationary
synthetic series and asserts the detector locates BOTH within +/- 1 month.
Further tests check the candidate-trimming helper and input validation.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.break_detection import _segment_break_candidates, detect_breaks
from src.config import BreakConfig


def _series_with_two_breaks(
    n: int,
    first_break: int,
    second_break: int,
    levels: tuple[float, float, float],
    seed: int,
) -> pd.Series:
    """Monthly series with two mean-level shifts plus small noise."""
    rng = np.random.default_rng(seed)
    values = np.empty(n, dtype=float)
    values[:first_break] = levels[0]
    values[first_break:second_break] = levels[1]
    values[second_break:] = levels[2]
    values += rng.normal(0.0, 1.0, size=n)
    index = pd.date_range("2000-01-01", periods=n, freq="MS")
    return pd.Series(values, index=index)


def _config(**overrides: object) -> BreakConfig:
    """BreakConfig with fast, deterministic defaults for tests."""
    base: dict[str, object] = {
        "trim_fraction": 0.15,
        "bootstrap_reps": 200,
        "random_seed": 0,
        "max_breaks": 3,
        "min_segment_months": 24,
        "significance_level": 0.05,
    }
    base.update(overrides)
    return BreakConfig(**base)  # type: ignore[arg-type]


def test_detects_two_planted_level_shifts_within_one_month() -> None:
    """Both planted level shifts are located within +/- 1 month of the truth."""
    n = 180
    first_break = 60
    second_break = 120
    series = _series_with_two_breaks(
        n, first_break, second_break, levels=(100.0, 140.0, 90.0), seed=7
    )

    results = detect_breaks(series, "planted", _config())

    indices = [r.break_index for r in results]
    assert any(abs(i - first_break) <= 1 for i in indices), indices
    assert any(abs(i - second_break) <= 1 for i in indices), indices
    # Every reported break passes the conservative moving-block bootstrap gate
    # (the synthetic series has no seasonality/autocorrelation, so it should).
    assert all(r.p_bootstrap_block < 0.05 for r in results)
    # The i.i.d. bootstrap p-value is also reported for each break.
    assert all(r.p_bootstrap_iid < 0.05 for r in results)
    # Results are returned in ascending date order.
    assert indices == sorted(indices)


def test_stops_on_a_stationary_series() -> None:
    """A series with no level shift yields no significant break."""
    rng = np.random.default_rng(3)
    index = pd.date_range("2000-01-01", periods=120, freq="MS")
    series = pd.Series(100.0 + rng.normal(0.0, 1.0, size=120), index=index)

    results = detect_breaks(series, "flat", _config())

    assert results == []


def test_candidate_indices_respect_trim_and_param_count() -> None:
    """Trimming excludes the segment ends and keeps each regime estimable."""
    candidates = _segment_break_candidates(100, 0.15)
    # 15% trim on 100 obs -> start no earlier than 15, end no later than 85.
    assert min(candidates) >= 15
    assert max(candidates) <= 85
    # Each regime must have more observations than parameters (2).
    assert all(2 < i < 100 - 2 for i in candidates)


def test_invalid_trim_fraction_raises() -> None:
    """A trim fraction outside [0, 0.5) must raise a clear ValueError."""
    index = pd.date_range("2000-01-01", periods=60, freq="MS")
    series = pd.Series(np.arange(60, dtype=float), index=index)
    with pytest.raises(ValueError):
        detect_breaks(series, "x", _config(trim_fraction=0.5))
    with pytest.raises(ValueError):
        detect_breaks(series, "x", _config(trim_fraction=-0.1))
