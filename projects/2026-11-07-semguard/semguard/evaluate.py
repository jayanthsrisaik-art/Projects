"""Replay a labelled query stream through cache policies; calibrate on dev topics, report on test topics."""
from __future__ import annotations

import hashlib
import time
from collections import Counter
from typing import Iterable, Optional

from .cache import CacheConfig, SemanticCache
from .embed import HashingEmbedder
from .facts import GUARDS, Lexicon

class MemoEmbedder(HashingEmbedder):
    """Replays embed the same texts many times during calibration; memoise them."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self._memo: dict = {}

    def embed(self, text: str):
        v = self._memo.get(text)
        if v is None:
            v = self._memo[text] = super().embed(text)
        return v


GRID = [round(0.50 + 0.01 * i, 2) for i in range(50)]  # 0.50 .. 0.99


def split_by_topic(records: list[dict], dev_fraction: float = 0.4, seed: int = 0) -> tuple[list[dict], list[dict]]:
    """Group split: whole topics go to dev or test, so sibling near-misses never straddle the split and the
    threshold is chosen on intents the test set has never seen."""
    topics = sorted({r["topic"] for r in records},
                    key=lambda t: hashlib.sha256(f"{seed}:{t}".encode()).hexdigest())
    n_dev = max(1, round(len(topics) * dev_fraction))
    dev_topics = set(topics[:n_dev])
    dev = [r for r in records if r["topic"] in dev_topics]
    test = [r for r in records if r["topic"] not in dev_topics]
    return dev, test


def _wrong_kind(q: dict, served: dict) -> str:
    if q["volatile"]:
        return "stale_volatile"
    if served["family"] == q["family"]:
        return "slot_value"
    if served["topic"] == q["topic"]:
        return f"sibling_{q['contrast'] or 'other'}"
    return "cross_topic"


def replay(records: Iterable[dict], config: CacheConfig, embedder: Optional[HashingEmbedder] = None,
           lexicon: Optional[Lexicon] = None, timing: bool = False) -> dict:
    """Stream queries in order. Miss/bypass => 'call the model' and (on miss) store the answer.
    A hit is correct iff the served entry has the same ground-truth key and the query is not volatile."""
    cache = SemanticCache(":memory:", config, embedder, lexicon, clock=lambda: 0.0)
    seen: set[str] = set()
    n = hits = correct = bypass = reusable = 0
    wrong = Counter()
    elapsed = 0.0
    for t, q in enumerate(records):
        n += 1
        if not q["volatile"] and q["key"] in seen:
            reusable += 1                        # an oracle cache could have answered this one
        t0 = time.perf_counter()
        lk = cache.lookup(q["text"], now=float(t))
        elapsed += time.perf_counter() - t0
        if lk.status == "hit":
            hits += 1
            served = lk.entry["metadata"]
            if served["key"] == q["key"] and not q["volatile"]:
                correct += 1
            else:
                wrong[_wrong_kind(q, served)] += 1
        else:
            bypass += lk.status == "bypass"
            if lk.status == "miss":
                cache.store(q["text"], "answer", metadata={k: q[k] for k in ("key", "family", "topic")},
                            now=float(t))
        seen.add(q["key"])
    n_wrong = sum(wrong.values())
    out = {
        "queries": n, "hits": hits, "correct_hits": correct, "wrong_hits": n_wrong, "bypassed": bypass,
        "llm_calls": n - hits, "reusable": reusable,
        "hit_rate": round(hits / n, 4) if n else 0.0,
        "hit_precision": round(correct / hits, 4) if hits else 1.0,
        "recall_of_reusable": round(correct / reusable, 4) if reusable else 0.0,
        "wrong_per_1k_queries": round(1000 * n_wrong / n, 2) if n else 0.0,
        "wrong_by_kind": dict(sorted(wrong.items())),
    }
    if timing:
        out["mean_lookup_ms"] = round(1000 * elapsed / n, 3) if n else 0.0
    return out


def calibrate(dev: list[dict], base: CacheConfig, target_precision: float = 0.99,
              grid: Iterable[float] = GRID, embedder=None, lexicon=None) -> dict:
    """Lowest threshold (=> most hits) whose dev hit precision meets the target. Uses dev records only."""
    curve = []
    chosen = None
    for th in grid:
        m = replay(dev, base.with_(threshold=th), embedder, lexicon)
        curve.append({"threshold": th, "hit_rate": m["hit_rate"], "hit_precision": m["hit_precision"]})
        if chosen is None and m["hit_precision"] >= target_precision and m["hits"] > 0:
            chosen = th
    met = chosen is not None
    return {"threshold": chosen if met else max(grid), "target_met": met, "curve": curve}


POLICIES = {
    "exact_match": CacheConfig(semantic=False, guards=()),
    "semantic_only": CacheConfig(guards=()),
    "semantic_plus_volatile": CacheConfig(guards=("volatile",)),
    "guarded": CacheConfig(),
}


def run_eval(records: list[dict], target_precision: float = 0.99, seed: int = 0, timing: bool = False) -> dict:
    emb, lex = MemoEmbedder(), Lexicon.default()
    dev, test = split_by_topic(records, seed=seed)
    report = {
        "split": {"dev_topics": sorted({r["topic"] for r in dev}), "test_topics": sorted({r["topic"] for r in test}),
                  "dev_queries": len(dev), "test_queries": len(test)},
        "target_precision": target_precision, "policies": {}, "ablation": {}, "fixed_threshold": {},
    }
    for name, cfg in POLICIES.items():
        entry: dict = {}
        if cfg.semantic:
            cal = calibrate(dev, cfg, target_precision, embedder=emb, lexicon=lex)
            cfg = cfg.with_(threshold=cal["threshold"])
            entry["calibration"] = {"threshold": cal["threshold"], "target_met_on_dev": cal["target_met"]}
        entry["dev"] = replay(dev, cfg, emb, lex)
        entry["test"] = replay(test, cfg, emb, lex, timing=timing)
        report["policies"][name] = entry
    g_th = report["policies"]["guarded"]["calibration"]["threshold"]
    # same threshold for both => isolates what the guards add
    for th in sorted({g_th, 0.70, 0.80, 0.90}):
        report["fixed_threshold"][f"{th:.2f}"] = {
            "semantic_only": _brief(replay(test, POLICIES["semantic_only"].with_(threshold=th), emb, lex)),
            "guarded": _brief(replay(test, POLICIES["guarded"].with_(threshold=th), emb, lex)),
        }
    for g in GUARDS:
        cfg = CacheConfig(threshold=g_th, guards=tuple(x for x in GUARDS if x != g))
        report["ablation"][f"without_{g}"] = {"dev": _brief(replay(dev, cfg, emb, lex)),
                                               "test": _brief(replay(test, cfg, emb, lex))}
    return report


def _brief(m: dict) -> dict:
    return {k: m[k] for k in ("hit_rate", "hit_precision", "recall_of_reusable", "wrong_hits", "wrong_by_kind")}


def format_report(report: dict) -> str:
    """Markdown summary of a run_eval() report."""
    sp = report["split"]
    lines = [f"dev topics ({sp['dev_queries']} queries): {', '.join(sp['dev_topics'])}",
             f"test topics ({sp['test_queries']} queries): {', '.join(sp['test_topics'])}",
             f"threshold chosen on dev for hit precision >= {report['target_precision']}", "",
             "| policy (test split) | threshold | hit rate | hit precision | recall of reusable | wrong hits | wrong by kind |",
             "|---|---|---|---|---|---|---|"]
    for name, e in report["policies"].items():
        t = e["test"]
        cal = e.get("calibration")
        th = "exact" if cal is None else f"{cal['threshold']:.2f}" + ("" if cal["target_met_on_dev"] else " (target unmet)")
        kinds = ", ".join(f"{k} {v}" for k, v in t["wrong_by_kind"].items()) or "-"
        lines.append(f"| {name} | {th} | {t['hit_rate']:.3f} | {t['hit_precision']:.3f} | "
                     f"{t['recall_of_reusable']:.3f} | {t['wrong_hits']} | {kinds} |")
    lines += ["", "| ablation (guarded threshold) | dev precision | dev wrong | test precision | test recall | test wrong |",
              "|---|---|---|---|---|---|"]
    for name, e in report["ablation"].items():
        d, t = e["dev"], e["test"]
        lines.append(f"| {name} | {d['hit_precision']:.3f} | {d['wrong_hits']} | {t['hit_precision']:.3f} | "
                     f"{t['recall_of_reusable']:.3f} | {t['wrong_hits']} |")
    lines += ["", "| same threshold, test split | semantic_only precision / wrong | guarded precision / wrong |",
              "|---|---|---|"]
    for th, e in report["fixed_threshold"].items():
        s, g = e["semantic_only"], e["guarded"]
        lines.append(f"| {th} | {s['hit_precision']:.3f} / {s['wrong_hits']} | {g['hit_precision']:.3f} / {g['wrong_hits']} |")
    return "\n".join(lines)
