"""Uplift evaluation.

Observable metrics (usable on real data, need only y and t):
  * Qini curve / Qini area - incremental conversions as customers are added
    in score order, compared with random targeting.
  * Observed uplift in the top-k fraction (treated rate - control rate).

Oracle metrics (only possible on simulated data with known effects):
  * expected incremental conversions when treating the top-k fraction,
    and its share of the best achievable value,
  * share of contacted customers whose true effect is negative,
  * Spearman rank correlation between scores and true uplift.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd


def _order(score: np.ndarray) -> np.ndarray:
    # Stable sort so tied scores keep input order (deterministic).
    return np.argsort(-np.asarray(score, dtype=float), kind="stable")


def _top_k(n: int, frac: float) -> int:
    if not 0.0 < frac <= 1.0:
        raise ValueError("fraction must be in (0, 1]")
    return max(1, math.ceil(frac * n))


def qini_curve(score, y, t) -> np.ndarray:
    """Q(k) for k = 0..n: treated conversions minus control conversions scaled to treated size.

    Q(k) = Y_t(k) - Y_c(k) * N_t(k) / N_c(k)   (Y_c term is 0 while N_c(k) = 0)
    """
    y = np.asarray(y, dtype=float)
    t = np.asarray(t, dtype=float)
    if not (len(score) == len(y) == len(t)):
        raise ValueError("score, y and t must have the same length")
    o = _order(score)
    y, t = y[o], t[o]
    n_t = np.cumsum(t)
    n_c = np.cumsum(1.0 - t)
    y_t = np.cumsum(y * t)
    y_c = np.cumsum(y * (1.0 - t))
    ratio = np.divide(n_t, n_c, out=np.zeros_like(n_t), where=n_c > 0)
    return np.concatenate([[0.0], y_t - y_c * ratio])


def qini_area(score, y, t) -> float:
    """Mean gap between the Qini curve and the random-targeting line, per customer.

    0 means no better than random; positive means incremental buyers are
    found earlier than random. Units: incremental conversions / customer.
    """
    q = qini_curve(score, y, t)
    n = len(q) - 1
    random_line = np.linspace(0.0, q[-1], n + 1)
    return float(np.mean(q - random_line) / n)


def observed_uplift_at(score, y, t, frac: float) -> float:
    """Treated conversion rate minus control conversion rate in the top ``frac`` by score."""
    y = np.asarray(y, dtype=float)
    t = np.asarray(t, dtype=int)
    top = _order(score)[: _top_k(len(y), frac)]
    yt, tt = y[top], t[top]
    if tt.min() == tt.max():
        return float("nan")
    return float(yt[tt == 1].mean() - yt[tt == 0].mean())


def oracle_policy(score, true_uplift, frac: float) -> dict:
    """Expected outcome of treating the top ``frac`` by score, using known true effects."""
    u = np.asarray(true_uplift, dtype=float)
    k = _top_k(len(u), frac)
    chosen = u[_order(score)[:k]]
    best = np.sort(u)[::-1][:k].sum()
    gained = chosen.sum()
    return {
        "contacted": int(k),
        "expected_incremental": float(gained),
        "oracle_best_incremental": float(best),
        "capture_of_best": float(gained / best) if best > 0 else float("nan"),
        "sleeping_dog_share": float((chosen < 0).mean()),
    }


def spearman(a, b) -> float:
    return float(pd.Series(a).rank().corr(pd.Series(b).rank()))
