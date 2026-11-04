"""Confident learning (Northcutt, Jiang & Chuang, 2021) implemented from scratch.

Inputs everywhere are
  labels      int array (n,) of *given* (possibly noisy) labels in 0..K-1
  pred_probs  float array (n, K) of *out-of-sample* predicted probabilities

Out-of-sample matters: a model scored on the rows it was trained on has
memorised their (wrong) labels, so the wrong label looks confident.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

METHODS = ("confident_joint", "prune_by_noise_rate", "both")


def _check(labels: np.ndarray, pred_probs: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    labels = np.asarray(labels)
    pred_probs = np.asarray(pred_probs, dtype=float)
    if pred_probs.ndim != 2:
        raise ValueError("pred_probs must be 2-D (n, K)")
    if labels.shape != (pred_probs.shape[0],):
        raise ValueError("labels and pred_probs disagree on n")
    if not np.issubdtype(labels.dtype, np.integer):
        raise ValueError("labels must be integers 0..K-1")
    if labels.size and (labels.min() < 0 or labels.max() >= pred_probs.shape[1]):
        raise ValueError("label outside 0..K-1")
    if np.any(pred_probs < 0) or not np.allclose(pred_probs.sum(axis=1), 1.0, atol=1e-6):
        raise ValueError("each row of pred_probs must be a probability distribution")
    return labels.astype(int), pred_probs


def per_class_thresholds(labels: np.ndarray, pred_probs: np.ndarray) -> np.ndarray:
    """t_j = mean predicted probability of class j over rows *labelled* j.

    A class with no labelled rows gets +inf so nothing can be confidently assigned to it.
    """
    labels, pred_probs = _check(labels, pred_probs)
    k = pred_probs.shape[1]
    t = np.full(k, np.inf)
    for j in range(k):
        mask = labels == j
        if mask.any():
            t[j] = pred_probs[mask, j].mean()
    return t


def confident_assignment(labels: np.ndarray, pred_probs: np.ndarray,
                         thresholds: np.ndarray | None = None) -> np.ndarray:
    """For each row, the class it *confidently* belongs to, or -1 if none.

    A row is a confident member of class j when p(j) >= t_j; ties among several
    qualifying classes go to the one with the largest probability.
    """
    labels, pred_probs = _check(labels, pred_probs)
    if thresholds is None:
        thresholds = per_class_thresholds(labels, pred_probs)
    above = pred_probs >= thresholds[None, :] - 1e-12
    masked = np.where(above, pred_probs, -1.0)
    best = masked.argmax(axis=1)
    best[~above.any(axis=1)] = -1
    return best


def confident_joint(labels: np.ndarray, pred_probs: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Raw confident joint C[given, true_guess] (counts) and the per-row assignment."""
    labels, pred_probs = _check(labels, pred_probs)
    k = pred_probs.shape[1]
    assign = confident_assignment(labels, pred_probs)
    c = np.zeros((k, k), dtype=int)
    ok = assign >= 0
    np.add.at(c, (labels[ok], assign[ok]), 1)
    return c, assign


def calibrate_joint(c: np.ndarray, labels: np.ndarray, k: int) -> np.ndarray:
    """Rescale each row of C so it sums to the number of rows with that given label,
    then normalise to a joint distribution Q(given, true) summing to 1."""
    counts = np.bincount(labels, minlength=k).astype(float)
    row = c.sum(axis=1).astype(float)
    cal = np.zeros_like(c, dtype=float)
    for i in range(k):
        if row[i] > 0:
            cal[i] = c[i] / row[i] * counts[i]
        elif counts[i] > 0:  # no confident rows at all: assume this label is clean
            cal[i, i] = counts[i]
    total = cal.sum()
    return cal / total if total > 0 else cal


@dataclass
class Audit:
    thresholds: np.ndarray        # (K,)
    confident_joint: np.ndarray   # (K, K) raw counts, rows = given label
    joint: np.ndarray             # (K, K) calibrated Q(given, true), sums to 1
    noise_matrix: np.ndarray      # (K, K) P(given=i | true=j); columns sum to 1
    issue_mask: np.ndarray        # (n,) bool
    suggested: np.ndarray         # (n,) best guess at the true label
    self_confidence: np.ndarray   # (n,) p(given label)
    margin: np.ndarray            # (n,) p(given) - max other p; < 0 means model disagrees
    ranking: np.ndarray           # indices of flagged rows, most suspicious first
    method: str

    @property
    def estimated_noise_rate(self) -> float:
        return float(1.0 - np.trace(self.joint))

    @property
    def n_issues(self) -> int:
        return int(self.issue_mask.sum())


def noise_matrix_from_joint(joint: np.ndarray) -> np.ndarray:
    col = joint.sum(axis=0, keepdims=True)
    out = np.divide(joint, col, out=np.zeros_like(joint), where=col > 0)
    k = joint.shape[0]
    for j in range(k):  # a class never estimated as "true": treat as noiseless
        if col[0, j] == 0:
            out[j, j] = 1.0
    return out


def normalized_margin(labels: np.ndarray, pred_probs: np.ndarray) -> np.ndarray:
    n = len(labels)
    given = pred_probs[np.arange(n), labels]
    other = pred_probs.copy()
    other[np.arange(n), labels] = -np.inf
    return given - other.max(axis=1)


def _prune_by_noise_rate(labels: np.ndarray, pred_probs: np.ndarray, joint: np.ndarray) -> np.ndarray:
    """For each off-diagonal cell (i, j), flag the round(n * Q[i, j]) rows labelled i
    with the largest p(j) - p(i). A row picked by several cells is flagged once."""
    n, k = pred_probs.shape
    flagged = np.zeros(n, dtype=bool)
    for i in range(k):
        rows = np.flatnonzero(labels == i)
        if rows.size == 0:
            continue
        budget_left = rows.size - 1  # never prune a class to nothing
        for j in range(k):
            if i == j:
                continue
            m = int(round(n * joint[i, j]))
            m = min(m, budget_left)
            if m <= 0:
                continue
            gap = pred_probs[rows, j] - pred_probs[rows, i]
            order = np.argsort(-gap, kind="stable")[:m]
            flagged[rows[order]] = True
            budget_left -= m
    return flagged


def find_label_issues(labels: np.ndarray, pred_probs: np.ndarray,
                      method: str = "prune_by_noise_rate") -> Audit:
    """Estimate the noise structure and flag likely mislabeled rows.

    method:
      confident_joint      rows counted in an off-diagonal cell of C
      prune_by_noise_rate  per-cell budget from the calibrated joint, largest margin first
      both                 intersection of the two (higher precision)
    """
    if method not in METHODS:
        raise ValueError(f"unknown method {method!r}; choose from {METHODS}")
    labels, pred_probs = _check(labels, pred_probs)
    n, k = pred_probs.shape
    thresholds = per_class_thresholds(labels, pred_probs)
    c, assign = confident_joint(labels, pred_probs)
    joint = calibrate_joint(c, labels, k)

    off_diag = (assign >= 0) & (assign != labels)
    if method == "confident_joint":
        mask = off_diag
    else:
        pbnr = _prune_by_noise_rate(labels, pred_probs, joint)
        mask = pbnr if method == "prune_by_noise_rate" else (pbnr & off_diag)

    self_conf = pred_probs[np.arange(n), labels]
    margin = normalized_margin(labels, pred_probs)
    other = pred_probs.copy()
    other[np.arange(n), labels] = -np.inf
    # prefer the confident assignment; fall back to the runner-up class
    suggested = np.where(mask & off_diag, assign, np.where(mask, other.argmax(axis=1), labels))
    flagged = np.flatnonzero(mask)
    ranking = flagged[np.lexsort((flagged, margin[flagged]))]  # most negative margin first
    return Audit(thresholds, c, joint, noise_matrix_from_joint(joint), mask, suggested,
                 self_conf, margin, ranking, method)
