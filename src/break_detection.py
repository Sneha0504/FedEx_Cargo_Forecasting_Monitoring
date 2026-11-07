"""Structural-break detection via binary-segmentation sup-Chow scans.

Each series (the FedEx domestic and international network freight series) is
tested for *multiple* structural breaks in the mean *level* using binary
segmentation (recursive supremum-Chow). To keep the test honest in the presence
of strong monthly seasonality and autocorrelation, two things are done:

1. **Seasonal adjustment first.** Before any break test, the series is
   seasonally adjusted: a full-series OLS of freight on a linear trend plus 11
   monthly dummies is fit, and only the estimated (mean-centered) seasonal
   component is removed -- the level and trend are kept. The break scan then
   runs on the seasonally-adjusted series, so large seasonal swings are no
   longer mistaken for structural shifts. Caveat: the seasonal factors are
   estimated over the full window, so a late break lightly contaminates them.

2. **Two bootstraps, with the conservative one as the gate.** Significance of
   each candidate sup-F is assessed by a residual bootstrap on the
   seasonally-adjusted residuals in two forms: an i.i.d. bootstrap
   (``p_bootstrap_iid``) that resamples residuals independently, and a
   moving-block bootstrap (``p_bootstrap_block``, block length
   ``break.block_length``) that preserves short-range autocorrelation. The
   **block** p-value -- the conservative one -- is the acceptance gate.

The scan regression keeps 2 parameters per regime (level + linear trend), so a
short post-break segment (e.g. ~16 months) still has adequate degrees of
freedom. Every number is derived purely from the data; no real-world or
business-event interpretation of a break is produced by this module.
"""

from __future__ import annotations

import heapq
import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from src.config import BreakConfig, get_config, setup_logging

logger = logging.getLogger(__name__)

# Number of regression parameters per regime in the scan: intercept + trend.
_N_PARAMS = 2
_MONTHS_PER_YEAR = 12

# Output column order for the break-analysis file (one row per detected break).
_RESULT_COLUMNS = [
    "series",
    "break_month",
    "f_stat",
    "p_bootstrap_iid",
    "p_bootstrap_block",
    "pre_mean",
    "post_mean",
    "pct_change",
]


@dataclass(frozen=True)
class BreakResult:
    """One detected break within a series.

    ``f_stat`` and both p-values come from the seasonally-adjusted series;
    ``pre_mean``/``post_mean``/``pct_change`` are computed on the ORIGINAL
    freight series (segment-local) so the percent change stays interpretable.
    """

    series: str
    break_month: pd.Timestamp
    break_index: int
    f_stat: float
    p_bootstrap_iid: float
    p_bootstrap_block: float
    pre_mean: float
    post_mean: float
    pct_change: float


def _design_matrix(n: int) -> np.ndarray:
    """Return the ``n x 2`` level(+linear-trend) design matrix ``[1, t]``."""
    return np.column_stack([np.ones(n), np.arange(n, dtype=float)])


def _residual_maker(x: np.ndarray) -> np.ndarray:
    """Return the symmetric idempotent residual-maker ``M = I - X (XᵀX)⁻¹ Xᵀ``.

    For any response ``y`` the OLS residual sum of squares is ``yᵀ M y``.
    """
    hat = x @ np.linalg.pinv(x)
    return np.eye(x.shape[0]) - hat


def _rss(residual_maker: np.ndarray, y: np.ndarray) -> float:
    """Residual sum of squares ``yᵀ M y`` for a single response vector."""
    return float(y @ residual_maker @ y)


def deseasonalize(series: pd.Series) -> np.ndarray:
    """Return the seasonally-adjusted series (level + trend + noise kept).

    A full-series OLS of ``freight ~ 1 + t + D_feb + ... + D_dec`` (11 monthly
    dummies, January as the baseline) is fit; the estimated monthly effects are
    mean-centered across the twelve calendar months and subtracted, so only the
    seasonal component is removed and the level and trend are preserved.

    Args:
        series: Monthly series indexed by a month-start ``DatetimeIndex``.

    Returns:
        The seasonally-adjusted values as a float array aligned with ``series``.
    """
    y = series.to_numpy(dtype=float)
    n = y.shape[0]
    months = np.asarray(series.index.month, dtype=int)
    trend = np.arange(n, dtype=float)

    dummies = np.zeros((n, _MONTHS_PER_YEAR - 1), dtype=float)
    for col, month in enumerate(range(2, _MONTHS_PER_YEAR + 1)):
        dummies[:, col] = (months == month).astype(float)
    design = np.column_stack([np.ones(n), trend, dummies])

    beta, *_ = np.linalg.lstsq(design, y, rcond=None)

    # Per-calendar-month seasonal effect (January baseline = 0), then centered.
    seasonal = np.zeros(_MONTHS_PER_YEAR + 1, dtype=float)  # index by month 1..12
    for col, month in enumerate(range(2, _MONTHS_PER_YEAR + 1)):
        seasonal[month] = beta[2 + col]
    seasonal[1 : _MONTHS_PER_YEAR + 1] -= seasonal[1 : _MONTHS_PER_YEAR + 1].mean()

    return y - seasonal[months]


def _segment_break_candidates(m: int, trim_fraction: float) -> list[int]:
    """Candidate break indices *within* a segment of length ``m``.

    A candidate ``i`` splits the segment into regime 1 (positions ``0 .. i-1``)
    and regime 2 (``i .. m-1``). Candidates are trimmed to the central portion
    (``trim_fraction`` from each end) and each regime must have more
    observations than parameters so both regressions are estimable.
    """
    lo = max(_N_PARAMS + 1, int(np.floor(trim_fraction * m)))
    hi = min(m - (_N_PARAMS + 1), int(np.ceil((1.0 - trim_fraction) * m)))
    if lo > hi:
        return []
    return list(range(lo, hi + 1))


def _f_scan(
    y: np.ndarray,
    rss_pooled: float,
    segment_makers: dict[int, tuple[np.ndarray, np.ndarray]],
    candidates: list[int],
    n: int,
) -> np.ndarray:
    """Chow F-statistic at every candidate break index for one response vector."""
    denom_df = n - 2 * _N_PARAMS
    f_stats = np.empty(len(candidates), dtype=float)
    for pos, i in enumerate(candidates):
        m1, m2 = segment_makers[i]
        rss_unrestricted = _rss(m1, y[:i]) + _rss(m2, y[i:])
        if rss_unrestricted <= 0.0:
            f_stats[pos] = np.inf
            continue
        f_stats[pos] = (
            (rss_pooled - rss_unrestricted) / _N_PARAMS
        ) / (rss_unrestricted / denom_df)
    return f_stats


def _sup_f_distribution(
    y_star: np.ndarray,
    m_full: np.ndarray,
    segment_makers: dict[int, tuple[np.ndarray, np.ndarray]],
    candidates: list[int],
    n: int,
) -> np.ndarray:
    """Vectorized sup-F for each column (bootstrap replicate) of ``y_star``."""
    denom_df = n - 2 * _N_PARAMS
    rss_pooled = np.einsum("ij,ij->j", y_star, m_full @ y_star)
    sup = np.full(y_star.shape[1], -np.inf)
    for i in candidates:
        y1 = y_star[:i]
        y2 = y_star[i:]
        rss1 = np.einsum("ij,ij->j", y1, segment_makers[i][0] @ y1)
        rss2 = np.einsum("ij,ij->j", y2, segment_makers[i][1] @ y2)
        rss_unrestricted = rss1 + rss2
        with np.errstate(divide="ignore", invalid="ignore"):
            f_stat = ((rss_pooled - rss_unrestricted) / _N_PARAMS) / (
                rss_unrestricted / denom_df
            )
        f_stat = np.where(rss_unrestricted <= 0.0, np.inf, f_stat)
        np.maximum(sup, f_stat, out=sup)
    return sup


def _iid_residuals(
    residuals: np.ndarray, m: int, reps: int, rng: np.random.Generator
) -> np.ndarray:
    """Resample residuals independently (i.i.d. bootstrap), shape ``(m, reps)``."""
    idx = rng.integers(0, m, size=(m, reps))
    return residuals[idx]


def _moving_block_residuals(
    residuals: np.ndarray,
    m: int,
    reps: int,
    block_length: int,
    rng: np.random.Generator,
) -> np.ndarray:
    """Moving-block bootstrap residuals, shape ``(m, reps)``.

    Overlapping blocks of length ``min(block_length, m)`` are drawn with
    replacement and concatenated to length ``m``, preserving short-range serial
    dependence within each block.
    """
    length = min(block_length, m)
    n_blocks = int(np.ceil(m / length))
    starts = rng.integers(0, m - length + 1, size=(n_blocks, reps))
    offsets = np.arange(length)
    block_idx = starts[:, None, :] + offsets[None, :, None]  # (n_blocks, len, reps)
    block_idx = block_idx.reshape(n_blocks * length, reps)[:m, :]
    return residuals[block_idx]


def _scan_segment(
    y: np.ndarray,
    trim_fraction: float,
    reps: int,
    block_length: int,
    rng: np.random.Generator,
) -> tuple[int, float, float, float] | None:
    """Locate the single most likely break within a (adjusted) segment.

    Returns:
        ``(local_break_index, sup_f, p_bootstrap_iid, p_bootstrap_block)`` for
        the segment, or ``None`` if the segment admits no valid candidate break.
    """
    m = len(y)
    candidates = _segment_break_candidates(m, trim_fraction)
    if not candidates:
        return None

    x = _design_matrix(m)
    m_full = _residual_maker(x)
    rss_pooled = _rss(m_full, y)
    segment_makers = {
        i: (_residual_maker(x[:i]), _residual_maker(x[i:])) for i in candidates
    }

    f_stats = _f_scan(y, rss_pooled, segment_makers, candidates, m)
    best_pos = int(np.argmax(f_stats))
    local_index = candidates[best_pos]
    sup_f = float(f_stats[best_pos])

    fitted = y - m_full @ y
    residuals = m_full @ y

    y_iid = fitted[:, None] + _iid_residuals(residuals, m, reps, rng)
    sup_iid = _sup_f_distribution(y_iid, m_full, segment_makers, candidates, m)
    p_iid = float((1 + np.sum(sup_iid >= sup_f)) / (reps + 1))

    y_block = fitted[:, None] + _moving_block_residuals(
        residuals, m, reps, block_length, rng
    )
    sup_block = _sup_f_distribution(y_block, m_full, segment_makers, candidates, m)
    p_block = float((1 + np.sum(sup_block >= sup_f)) / (reps + 1))

    return local_index, sup_f, p_iid, p_block


def detect_breaks(
    series: pd.Series,
    name: str,
    config: BreakConfig,
) -> list[BreakResult]:
    """Detect multiple level breaks in ``series`` by binary segmentation.

    The series is seasonally adjusted (see :func:`deseasonalize`), then the
    globally most significant break (largest sup-F) is accepted first if its
    **moving-block** bootstrap p-value is below ``significance_level``; the
    procedure recurses into the sub-segments on either side of each accepted
    break. Pre/post mean levels are computed on the ORIGINAL freight series,
    within each break's own segment.

    Args:
        series: Monthly series indexed by a month-start ``DatetimeIndex``.
        name: Label for the series (e.g. ``"domestic"``).
        config: Break-detection settings.

    Returns:
        A list of :class:`BreakResult`, sorted by break month (ascending).

    Raises:
        ValueError: If ``trim_fraction`` is not in ``[0, 0.5)``.
    """
    if not 0.0 <= config.trim_fraction < 0.5:
        raise ValueError(
            f"trim_fraction must be in [0, 0.5); got {config.trim_fraction}."
        )

    series = series.sort_index()
    y_original = series.to_numpy(dtype=float)
    y_adjusted = deseasonalize(series)
    n = y_original.shape[0]

    # Heap of significant candidate breaks:
    # (-sup_f, lo, hi, global_index, p_iid, p_block). Negated F so the globally
    # most significant break is popped first.
    heap: list[tuple[float, int, int, int, float, float]] = []

    def consider(lo: int, hi: int) -> None:
        """Scan segment ``[lo, hi)`` of the adjusted series; push if significant."""
        if hi - lo < config.min_segment_months:
            return
        seg_rng = np.random.default_rng(config.random_seed + lo * 10_007 + hi)
        scan = _scan_segment(
            y_adjusted[lo:hi],
            config.trim_fraction,
            config.bootstrap_reps,
            config.block_length,
            seg_rng,
        )
        if scan is None:
            return
        local_index, sup_f, p_iid, p_block = scan
        if p_block >= config.significance_level:
            return
        heapq.heappush(heap, (-sup_f, lo, hi, lo + local_index, p_iid, p_block))

    consider(0, n)

    results: list[BreakResult] = []
    while heap and len(results) < config.max_breaks:
        neg_f, lo, hi, global_index, p_iid, p_block = heapq.heappop(heap)
        sup_f = -neg_f
        pre_mean = float(np.mean(y_original[lo:global_index]))
        post_mean = float(np.mean(y_original[global_index:hi]))
        pct_change = float((post_mean - pre_mean) / pre_mean * 100.0)
        break_month = series.index[global_index]
        results.append(
            BreakResult(
                series=name,
                break_month=break_month,
                break_index=global_index,
                f_stat=sup_f,
                p_bootstrap_iid=p_iid,
                p_bootstrap_block=p_block,
                pre_mean=pre_mean,
                post_mean=post_mean,
                pct_change=pct_change,
            )
        )
        logger.info(
            "[%s] break at %s (index %d): F=%.3f, p_iid=%.4g, p_block=%.4g | "
            "original-series pre_mean=%.4g, post_mean=%.4g, pct_change=%+.2f%%.",
            name,
            break_month.date(),
            global_index,
            sup_f,
            p_iid,
            p_block,
            pre_mean,
            post_mean,
            pct_change,
        )
        consider(lo, global_index)
        consider(global_index, hi)

    results.sort(key=lambda r: r.break_index)
    if not results:
        logger.info(
            "[%s] no break passed the moving-block bootstrap gate (p < %.3g).",
            name,
            config.significance_level,
        )
    return results


def _result_row(result: BreakResult) -> dict[str, object]:
    """Flatten a :class:`BreakResult` into a row for the output frame."""
    return {
        "series": result.series,
        "break_month": result.break_month.strftime("%Y-%m-%d"),
        "f_stat": result.f_stat,
        "p_bootstrap_iid": result.p_bootstrap_iid,
        "p_bootstrap_block": result.p_bootstrap_block,
        "pre_mean": result.pre_mean,
        "post_mean": result.post_mean,
        "pct_change": result.pct_change,
    }


def main() -> None:
    """Scan the domestic and international series for breaks and persist results.

    Reads the domestic/international network split, seasonally adjusts and runs
    binary-segmentation sup-Chow on each series separately, writes
    ``break_analysis.csv`` (one row per detected break, sorted by series then
    date) to the processed directory, and logs a clear summary. When the input
    is absent this logs a clear message and returns without fabricating data.
    """
    setup_logging()
    config = get_config()
    processed_dir = Path(config.data.processed_dir)
    split_path = processed_dir / config.data.network_domestic_intl_file

    if not split_path.exists():
        logger.info(
            "Domestic/intl split not found at %s; run the aggregation step "
            "first. Nothing to analyze.",
            split_path,
        )
        return

    frame = pd.read_csv(split_path, parse_dates=["month"], index_col="month")
    series_specs = {
        "domestic": "fedex_domestic_freight",
        "international": "fedex_intl_freight",
    }

    rows: list[dict[str, object]] = []
    for name, column in series_specs.items():
        series = frame[column].astype(float).sort_index()
        for result in detect_breaks(series, name, config.break_detection):
            rows.append(_result_row(result))

    out_frame = pd.DataFrame(rows, columns=_RESULT_COLUMNS)
    out_frame = out_frame.sort_values(["series", "break_month"]).reset_index(drop=True)
    out_path = processed_dir / config.data.break_analysis_file
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_frame.to_csv(out_path, index=False)

    logger.info(
        "Break detection complete; %d break(s) written to %s.\n%s",
        len(out_frame),
        out_path,
        out_frame.to_string(index=False),
    )
    logger.info(
        "NOTE: breaks tested on the seasonally-adjusted series. p_bootstrap_block "
        "(moving-block, length %d) is the conservative acceptance gate; "
        "p_bootstrap_iid is the independent-resample figure for comparison.",
        config.break_detection.block_length,
    )


if __name__ == "__main__":
    main()
