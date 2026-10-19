"""Evaluation: dev/test split, threshold tuning on dev only, per-class metrics on test."""
import hashlib
import itertools
import json
from pathlib import Path

from .lexical import LABELS, LexicalConfig, LexicalVerifier, OverlapBaseline

DATA = Path(__file__).resolve().parent.parent / "data"


def load_cases(path: Path | None = None) -> tuple[dict[str, str], list[dict]]:
    sources = json.loads((DATA / "sources.json").read_text())
    path = path or DATA / "eval.jsonl"
    cases = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    return sources, cases


def split_case(case_id: str) -> str:
    """Deterministic, order-independent split: sha1 of the id."""
    return "dev" if int(hashlib.sha1(case_id.encode()).hexdigest(), 16) % 2 == 0 else "test"


def split_cases(cases: list[dict]) -> tuple[list[dict], list[dict]]:
    dev = [c for c in cases if split_case(c["id"]) == "dev"]
    test = [c for c in cases if split_case(c["id"]) == "test"]
    return dev, test


def predict(verifier, cases: list[dict], sources: dict[str, str]) -> list[str]:
    return [verifier.verify(c["claim"], {c["source"]: sources[c["source"]]}).label for c in cases]


def metrics(gold: list[str], pred: list[str]) -> dict:
    per = {}
    for l in LABELS:
        tp = sum(g == l and p == l for g, p in zip(gold, pred))
        fp = sum(g != l and p == l for g, p in zip(gold, pred))
        fn = sum(g == l and p != l for g, p in zip(gold, pred))
        prec = tp / (tp + fp) if tp + fp else 0.0
        rec = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
        per[l] = {"precision": round(prec, 3), "recall": round(rec, 3), "f1": round(f1, 3),
                  "support": sum(g == l for g in gold)}
    confusion = {g: {p: sum(a == g and b == p for a, b in zip(gold, pred)) for p in LABELS}
                 for g in LABELS}
    return {
        "n": len(gold),
        "accuracy": round(sum(g == p for g, p in zip(gold, pred)) / len(gold), 3),
        "macro_f1": round(sum(v["f1"] for v in per.values()) / len(LABELS), 3),
        "per_class": per,
        "confusion": confusion,
    }


def macro_f1(verifier, cases, sources) -> float:
    return metrics([c["label"] for c in cases], predict(verifier, cases, sources))["macro_f1"]


def tune(dev: list[dict], sources: dict[str, str]) -> LexicalConfig:
    """Grid search on dev macro-F1. Ties go to the first grid point (lowest thresholds)."""
    grid_s = [round(0.5 + 0.05 * i, 2) for i in range(10)]
    grid_c = [round(0.3 + 0.05 * i, 2) for i in range(10)]
    best, best_score = None, -1.0
    for s, c in itertools.product(grid_s, grid_c):
        if c > s:
            continue
        cfg = LexicalConfig(support_threshold=s, conflict_threshold=c)
        score = macro_f1(LexicalVerifier(cfg), dev, sources)
        if score > best_score:
            best, best_score = cfg, score
    return best


def run(path: Path | None = None) -> dict:
    sources, cases = load_cases(path)
    dev, test = split_cases(cases)
    tuned = tune(dev, sources)
    gold = [c["label"] for c in test]
    majority = max(LABELS, key=lambda l: sum(c["label"] == l for c in dev))
    return {
        "n_dev": len(dev), "n_test": len(test),
        "tuned_config": {"support_threshold": tuned.support_threshold,
                         "conflict_threshold": tuned.conflict_threshold},
        "test": {
            "majority-class": metrics(gold, [majority] * len(test)),
            "overlap-baseline": metrics(gold, predict(OverlapBaseline(), test, sources)),
            "lexical": metrics(gold, predict(LexicalVerifier(tuned), test, sources)),
        },
        "dev_macro_f1_lexical": macro_f1(LexicalVerifier(tuned), dev, sources),
    }
