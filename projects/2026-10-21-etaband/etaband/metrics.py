from __future__ import annotations

import numpy as np


def coverage(y, lo, hi) -> float:
    y = np.asarray(y)
    return float(np.mean((y >= lo) & (y <= hi)))


def mean_width(lo, hi) -> float:
    return float(np.mean(np.asarray(hi) - np.asarray(lo)))


def interval_score(y, lo, hi, alpha: float) -> float:
    """Winkler interval score (lower is better): width + (2/alpha) * miss distance."""
    y, lo, hi = map(np.asarray, (y, lo, hi))
    pen = (2 / alpha) * (np.maximum(lo - y, 0) + np.maximum(y - hi, 0))
    return float(np.mean(hi - lo + pen))


def mae(y, p) -> float:
    return float(np.mean(np.abs(np.asarray(y) - np.asarray(p))))


def segment_coverage(y, lo, hi, labels) -> dict:
    y, lo, hi, labels = map(np.asarray, (y, lo, hi, labels))
    return {str(k): coverage(y[labels == k], lo[labels == k], hi[labels == k]) for k in np.unique(labels)}
