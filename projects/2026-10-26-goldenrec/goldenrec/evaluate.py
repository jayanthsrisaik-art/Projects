"""Pairwise precision/recall/F1 against ground truth, plus blocking quality."""

from __future__ import annotations

from collections import defaultdict
from itertools import combinations

Key = tuple[str, str]


def _pairs_from_groups(groups) -> set[tuple[Key, Key]]:
    out = set()
    for g in groups:
        out.update(combinations(sorted(g), 2))
    return out


def truth_pairs(truth: list[dict]) -> set[tuple[Key, Key]]:
    groups: dict[str, list[Key]] = defaultdict(list)
    for row in truth:
        groups[row["entity_id"]].append((row["source"], row["source_id"]))
    return _pairs_from_groups(groups.values())


def pairwise_metrics(clusters: list[list[Key]], truth: list[dict]) -> dict:
    predicted = _pairs_from_groups(clusters)
    actual = truth_pairs(truth)
    tp = len(predicted & actual)
    precision = tp / len(predicted) if predicted else 1.0
    recall = tp / len(actual) if actual else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    n_entities = len({r["entity_id"] for r in truth})
    return {
        "precision": round(precision, 4), "recall": round(recall, 4), "f1": round(f1, 4),
        "true_pairs": len(actual), "predicted_pairs": len(predicted), "true_positive_pairs": tp,
        "clusters": len(clusters), "true_entities": n_entities,
    }


def blocking_metrics(candidates: set[tuple[Key, Key]], truth: list[dict]) -> dict:
    """Pair completeness = share of true pairs that blocking let through (recall ceiling)."""
    actual = truth_pairs(truth)
    n = len(truth)
    all_pairs = n * (n - 1) // 2
    found = len(actual & candidates)
    return {
        "candidate_pairs": len(candidates),
        "pair_completeness": round(found / len(actual), 4) if actual else 1.0,
        "reduction_ratio": round(1 - len(candidates) / all_pairs, 6) if all_pairs else 1.0,
    }
