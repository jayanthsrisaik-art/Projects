"""Benchmark: detection quality, noise-structure estimates and downstream accuracy.

Protocol per (noise kind, rate, seed):
  1. stratified train/test split; inject noise into TRAIN labels only
  2. out-of-fold probabilities on the noisy train set (K-fold, no row scores itself)
  3. every detector flags rows of the train set; score against the known flips
  4. retrain on train minus flagged rows; accuracy on the CLEAN test labels
The test split is never seen by any detector.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass

import numpy as np

from .confident import find_label_issues
from .data import make_split
from .models import in_sample_proba, make_model, out_of_fold_proba


@dataclass
class Detection:
    flagged: int
    precision: float
    recall: float
    f1: float


def score_flags(mask: np.ndarray, truth: np.ndarray) -> Detection:
    tp = int((mask & truth).sum())
    fp = int((mask & ~truth).sum())
    fn = int((~mask & truth).sum())
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    f = 2 * p * r / (p + r) if p + r else 0.0
    return Detection(int(mask.sum()), round(p, 4), round(r, 4), round(f, 4))


def empirical_noise_matrix(given: np.ndarray, true: np.ndarray, k: int) -> np.ndarray:
    """P(given=i | true=j) actually realised in the data."""
    m = np.zeros((k, k))
    np.add.at(m, (given, true), 1)
    col = m.sum(axis=0, keepdims=True)
    return np.divide(m, col, out=np.zeros_like(m), where=col > 0)


def baseline_flags(labels: np.ndarray, probs: np.ndarray, n_true: int, n_est: int,
                   rng: np.random.Generator) -> dict[str, np.ndarray]:
    n = len(labels)
    self_conf = probs[np.arange(n), labels]
    out = {
        "argmax_disagree": probs.argmax(axis=1) != labels,
        "self_conf_lt_0.5": self_conf < 0.5,
    }
    oracle = np.zeros(n, dtype=bool)
    oracle[np.argsort(self_conf, kind="stable")[:n_true]] = True
    out["lowest_conf_oracle_k"] = oracle
    rnd = np.zeros(n, dtype=bool)
    rnd[rng.choice(n, size=min(n_est, n), replace=False)] = True
    out["random_same_k"] = rnd
    return out


def downstream_accuracy(x_tr, y_tr, x_te, y_te, keep: np.ndarray, model: str, seed: int) -> float:
    m = make_model(model, seed).fit(x_tr[keep], y_tr[keep])
    return round(float((m.predict(x_te) == y_te).mean()), 4)


def run_once(dataset: str, kind: str, rate: float, seed: int, model: str = "logreg",
             folds: int = 5) -> dict:
    s = make_split(dataset, rate, kind, seed=seed)
    y = s.y_train_noisy
    truth = s.is_flipped
    k = int(max(y.max(), s.y_train_true.max())) + 1
    probs, _ = out_of_fold_proba(s.x_train, y, model, folds, seed)
    audits = {m: find_label_issues(y, probs, m) for m in ("confident_joint", "prune_by_noise_rate", "both")}
    pbnr = audits["prune_by_noise_rate"]

    flags = {f"cl_{m}": a.issue_mask for m, a in audits.items()}
    leaky = find_label_issues(y, in_sample_proba(s.x_train, y, model, seed), "prune_by_noise_rate")
    flags["cl_pbnr_IN_SAMPLE_leaky"] = leaky.issue_mask
    flags.update(baseline_flags(y, probs, int(truth.sum()), pbnr.n_issues,
                                np.random.default_rng(seed + 7)))

    detection = {name: asdict(score_flags(mask, truth)) for name, mask in flags.items()}
    everyone = np.ones(len(y), dtype=bool)
    acc = {
        "noisy_labels": downstream_accuracy(s.x_train, y, s.x_test, s.y_test, everyone, model, seed),
        "clean_labels_oracle": downstream_accuracy(s.x_train, s.y_train_true, s.x_test, s.y_test,
                                                   everyone, model, seed),
    }
    for name in ("cl_prune_by_noise_rate", "cl_both", "argmax_disagree", "random_same_k"):
        acc[f"pruned_{name}"] = downstream_accuracy(s.x_train, y, s.x_test, s.y_test, ~flags[name], model, seed)

    true_nm = empirical_noise_matrix(y, s.y_train_true, k)
    return {
        "dataset": dataset, "noise_kind": kind, "noise_rate": rate, "seed": seed, "model": model,
        "n_train": int(len(y)), "n_test": int(len(s.y_test)), "true_flips": int(truth.sum()),
        "true_noise_rate": round(s.true_noise_rate, 4),
        "est_noise_rate": round(pbnr.estimated_noise_rate, 4),
        "noise_matrix_mae": round(float(np.abs(pbnr.noise_matrix - true_nm).mean()), 4),
        "detection": detection,
        "test_accuracy": acc,
    }


def summarise(runs: list[dict]) -> list[dict]:
    """Mean and std over seeds for each (kind, rate)."""
    groups: dict[tuple, list[dict]] = {}
    for r in runs:
        groups.setdefault((r["noise_kind"], r["noise_rate"]), []).append(r)
    out = []
    for (kind, rate), rs in sorted(groups.items()):
        row = {"noise_kind": kind, "noise_rate": rate, "seeds": len(rs)}

        def ms(vals):
            a = np.asarray(vals, dtype=float)
            return [round(float(a.mean()), 4), round(float(a.std()), 4)]
        row["est_noise_rate"] = ms([r["est_noise_rate"] for r in rs])
        row["true_noise_rate"] = ms([r["true_noise_rate"] for r in rs])
        row["noise_matrix_mae"] = ms([r["noise_matrix_mae"] for r in rs])
        row["f1"] = {d: ms([r["detection"][d]["f1"] for r in rs]) for d in rs[0]["detection"]}
        row["precision"] = {d: ms([r["detection"][d]["precision"] for r in rs]) for d in rs[0]["detection"]}
        row["recall"] = {d: ms([r["detection"][d]["recall"] for r in rs]) for d in rs[0]["detection"]}
        row["test_accuracy"] = {a: ms([r["test_accuracy"][a] for r in rs]) for a in rs[0]["test_accuracy"]}
        out.append(row)
    return out


def bench(dataset: str = "digits", kinds=("asymmetric", "uniform"), rates=(0.1, 0.2, 0.3),
          seeds=(0, 1, 2), model: str = "logreg", folds: int = 5) -> dict:
    runs = [run_once(dataset, k, r, s, model, folds) for k in kinds for r in rates for s in seeds]
    return {"config": {"dataset": dataset, "kinds": list(kinds), "rates": list(rates),
                       "seeds": list(seeds), "model": model, "folds": folds},
            "summary": summarise(runs), "runs": runs}


def dumps(obj) -> str:
    return json.dumps(obj, indent=2, sort_keys=False)
