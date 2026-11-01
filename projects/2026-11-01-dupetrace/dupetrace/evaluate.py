"""Chronological replay evaluation.

Reports are streamed in arrival order.  For each report we first *query* with
it against everything filed earlier, then add it to the indexes.  The stream is
cut by position into train (first 60%), validation (next 15%) and test (last
25%) periods:

* the reranker is fit only on candidate pairs of train-period queries;
* each method's decision threshold is chosen on validation-period queries;
* all reported numbers come from test-period queries.

Because indexes only ever contain the past, IDF statistics, candidate sets and
labels for every query are exactly what a live system would have had.
"""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .engine import CANDIDATES, Candidate, candidates, rerank
from .index import BM25Index, report_raw_tokens
from .model import Reranker
from .store import Report

SPLITS = (0.60, 0.75)
KS = (1, 5, 10)


@dataclass
class QueryRecord:
    report: Report
    split: str
    truth: set[str]                 # ids of EARLIER reports of the same bug
    norm: list[Candidate]           # normalised-BM25 candidates (features precomputed)
    raw: list[tuple[str, float]]    # raw-BM25 ranking (id, score)
    signature: list[tuple[str, float]]

    @property
    def is_dup(self) -> bool:
        return bool(self.truth)


def load_labels(path: str | Path) -> dict[str, str]:
    with open(path, newline="") as f:
        return {row["report_id"]: row["bug_id"] for row in csv.DictReader(f)}


def _signature(rep: Report) -> str | None:
    """Exact-match baseline key: exception + innermost 3 frames, else exception + message."""
    if rep.frames:
        return f"{rep.exc_type}|{'|'.join(rep.frames[-3:])}"
    if rep.exc_type and rep.norm_message:
        return f"{rep.exc_type}|{rep.norm_message}"
    return None


def replay(reports: list[Report], labels: dict[str, str], k: int = CANDIDATES) -> list[QueryRecord]:
    reports = sorted(reports, key=lambda r: (r.created_at, r.id))
    n = len(reports)
    cut1, cut2 = int(n * SPLITS[0]), int(n * SPLITS[1])
    norm_idx, raw_idx = BM25Index(), BM25Index(tokenizer=report_raw_tokens)
    sig_map: dict[str, list[str]] = defaultdict(list)
    seen_by_bug: dict[str, list[str]] = defaultdict(list)
    out: list[QueryRecord] = []
    for i, rep in enumerate(reports):
        split = "train" if i < cut1 else "val" if i < cut2 else "test"
        bug = labels.get(rep.id)
        truth = set(seen_by_bug[bug]) if bug else set()
        sig = _signature(rep)
        sig_hits = [(rid, 1.0) for rid in reversed(sig_map[sig])][:k] if sig else []
        out.append(QueryRecord(
            report=rep, split=split, truth=truth,
            norm=candidates(norm_idx, rep, k),
            raw=[(c.id, s) for c, s in raw_idx.search(rep, k)],
            signature=sig_hits,
        ))
        norm_idx.add(rep)
        raw_idx.add(rep)
        if sig:
            sig_map[sig].append(rep.id)
        if bug:
            seen_by_bug[bug].append(rep.id)
    return out


def training_pairs(records: list[QueryRecord]) -> tuple[np.ndarray, np.ndarray]:
    X, y = [], []
    for r in records:
        for c in r.norm:
            X.append(c.features)
            y.append(int(c.report.id in r.truth))
    return np.array(X, dtype=float), np.array(y, dtype=int)


# --- metrics --------------------------------------------------------------------

def ranking_metrics(ranked: list[list[str]], truths: list[set[str]]) -> dict[str, float]:
    """Recall@k (any true duplicate in top k) and MRR over duplicate queries only."""
    pairs = [(r, t) for r, t in zip(ranked, truths) if t]
    if not pairs:
        return {f"recall@{k}": 0.0 for k in KS} | {"mrr": 0.0, "n": 0}
    res: dict[str, float] = {}
    for k in KS:
        res[f"recall@{k}"] = sum(any(x in t for x in r[:k]) for r, t in pairs) / len(pairs)
    rr = []
    for r, t in pairs:
        rank = next((i + 1 for i, x in enumerate(r) if x in t), None)
        rr.append(1 / rank if rank else 0.0)
    res["mrr"] = float(np.mean(rr))
    res["n"] = len(pairs)
    return res


def decision_metrics(top: list[tuple[str | None, float]], truths: list[set[str]], threshold: float) -> dict[str, float]:
    """Suggest 'duplicate of top-1' when top score >= threshold.

    A suggestion is correct only if the query really is a duplicate AND the top-1
    candidate is one of its true earlier reports.
    """
    suggested = correct = 0
    false_alarm_new = 0
    n_dup = sum(1 for t in truths if t)
    n_new = len(truths) - n_dup
    for (cid, s), t in zip(top, truths):
        if cid is None or s < threshold:
            continue
        suggested += 1
        if t and cid in t:
            correct += 1
        if not t:
            false_alarm_new += 1
    p = correct / suggested if suggested else 0.0
    r = correct / n_dup if n_dup else 0.0
    f1 = 2 * p * r / (p + r) if p + r else 0.0
    return {"threshold": threshold, "precision": p, "recall": r, "f1": f1, "suggested": suggested,
            "false_alarm_rate_on_new": false_alarm_new / n_new if n_new else 0.0}


def best_threshold(top: list[tuple[str | None, float]], truths: list[set[str]]) -> float:
    """Threshold maximising F1 on the given (validation) queries; ties -> higher threshold."""
    cands = sorted({s for _, s in top if _ is not None} | {float("inf")})
    best_t, best_f1 = float("inf"), -1.0
    for t in cands:
        f1 = decision_metrics(top, truths, t)["f1"]
        if f1 > best_f1 or (f1 == best_f1 and t > best_t):
            best_t, best_f1 = t, f1
    return best_t


def _method_rankings(records: list[QueryRecord], model: Reranker) -> dict[str, list[list[tuple[str, float]]]]:
    out: dict[str, list[list[tuple[str, float]]]] = {"signature_exact": [], "bm25_raw": [], "bm25_normalised": [],
                                                     "reranker": []}
    for r in records:
        out["signature_exact"].append(r.signature)
        out["bm25_raw"].append(r.raw)
        out["bm25_normalised"].append([(c.report.id, c.bm25) for c in r.norm])
        out["reranker"].append([(c.report.id, c.score) for c in rerank(model, [*r.norm])])
    return out


def run(reports: list[Report], labels: dict[str, str], seed: int = 0) -> tuple[dict, Reranker]:
    records = replay(reports, labels)
    by_split = {s: [r for r in records if r.split == s] for s in ("train", "val", "test")}
    X, y = training_pairs(by_split["train"])
    model = Reranker.fit(X, y, seed=seed)

    val_rank = _method_rankings(by_split["val"], model)
    test_rank = _method_rankings(by_split["test"], model)
    val_truth = [r.truth for r in by_split["val"]]
    test_truth = [r.truth for r in by_split["test"]]

    methods = {}
    for name in test_rank:
        def top1(lists):
            return [(l[0][0], l[0][1]) if l else (None, 0.0) for l in lists]
        thr = best_threshold(top1(val_rank[name]), val_truth)
        ids = [[i for i, _ in l] for l in test_rank[name]]
        with_trace = [r.report.has_trace for r in by_split["test"]]
        methods[name] = {
            "ranking": ranking_metrics(ids, test_truth),
            "recall@1_by_query_trace": {
                seg: ranking_metrics([x for x, w in zip(ids, with_trace) if w == flag],
                                     [t for t, w in zip(test_truth, with_trace) if w == flag])["recall@1"]
                for seg, flag in (("with_trace", True), ("without_trace", False))
            },
            "decision": decision_metrics(top1(test_rank[name]), test_truth, thr),
        }
        if name == "reranker":
            model.threshold = thr

    test_dups = [r for r in by_split["test"] if r.truth]
    cand_recall = sum(any(c.report.id in r.truth for c in r.norm) for r in test_dups) / max(1, len(test_dups))
    report = {
        "data": {
            "reports": len(records),
            "bugs": len(set(labels.get(r.report.id) for r in records)),
            **{f"{s}_queries": len(v) for s, v in by_split.items()},
            **{f"{s}_duplicate_queries": sum(r.is_dup for r in v) for s, v in by_split.items()},
            "train_pairs": int(len(y)),
            "train_positive_pairs": int(y.sum()),
            "test_duplicate_queries_without_trace": sum(1 for r in by_split["test"] if r.truth and not r.report.has_trace),
            "test_period": [by_split["test"][0].report.created_at, by_split["test"][-1].report.created_at],
        },
        "candidate_recall@50_normalised": cand_recall,
        "methods": methods,
        "reranker_weights": [[f, round(w, 3)] for f, w in model.weights()],
    }
    return report, model


def format_report(rep: dict) -> str:
    lines = [f"reports={rep['data']['reports']} bugs={rep['data']['bugs']} "
             f"test queries={rep['data']['test_queries']} (duplicates={rep['data']['test_duplicate_queries']})",
             f"candidate recall@50 (normalised BM25): {rep['candidate_recall@50_normalised']:.3f}", "",
             f"{'method':<17} {'R@1':>6} {'R@5':>6} {'R@10':>6} {'MRR':>6} | {'prec':>6} {'recall':>6} {'F1':>6} {'FA(new)':>7}"]
    for name, m in rep["methods"].items():
        r, d = m["ranking"], m["decision"]
        lines.append(f"{name:<17} {r['recall@1']:6.3f} {r['recall@5']:6.3f} {r['recall@10']:6.3f} {r['mrr']:6.3f} | "
                     f"{d['precision']:6.3f} {d['recall']:6.3f} {d['f1']:6.3f} {d['false_alarm_rate_on_new']:7.3f}")
    lines += ["", "Recall@1 by query type (duplicate queries):"]
    for name, m in rep["methods"].items():
        seg = m["recall@1_by_query_trace"]
        lines.append(f"  {name:<17} with trace {seg['with_trace']:.3f}   without trace {seg['without_trace']:.3f}")
    return "\n".join(lines)


def save_report(rep: dict, path: str | Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(rep, indent=2))
