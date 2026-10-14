import numpy as np


def _arr(a):
    return np.asarray(a, dtype=float)


def mae(y, p) -> float:
    return float(np.mean(np.abs(_arr(y) - _arr(p))))


def rmse(y, p) -> float:
    return float(np.sqrt(np.mean((_arr(y) - _arr(p)) ** 2)))


def wape(y, p) -> float:
    y = _arr(y)
    total = y.sum()
    if total == 0:
        raise ValueError("WAPE undefined when actuals sum to zero")
    return float(np.abs(y - _arr(p)).sum() / total)


def pinball(y, p, q: float) -> float:
    d = _arr(y) - _arr(p)
    return float(np.mean(np.maximum(q * d, (q - 1) * d)))


def coverage(y, lo, hi) -> float:
    y = _arr(y)
    return float(np.mean((y >= _arr(lo)) & (y <= _arr(hi))))


def point_scores(y, p) -> dict:
    return {"mae": mae(y, p), "rmse": rmse(y, p), "wape": wape(y, p)}
