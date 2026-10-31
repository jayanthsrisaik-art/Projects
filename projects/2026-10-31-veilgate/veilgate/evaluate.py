"""Evaluation: detection quality, leakage, round-trip fidelity, egress guard.

Metrics (all measured on documents with gold spans):
* strict entity P/R/F1 -- predicted (start, end, type) must equal a gold span;
* leak rate -- share of gold entities NOT fully covered by any redaction
  (type confusion does not leak; a missed or truncated span does);
* hard-negative false positives -- redactions that overlap no gold entity;
* round-trip -- with an echo model, rehydrate(redact(x)) == x;
* egress guard -- how often the second-line scan fires on docs that leaked
  vs. docs that did not.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .corpus import Doc
from .detectors import ENTITY_TYPES, Span, detect, resolve_overlaps
from .egress import scan
from .vault import Session


def baseline_detect(text: str) -> list[Span]:
    """A typical regex scrubber: no validation, no context, no names."""
    pats = [
        ("EMAIL", r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"),
        ("SSN", r"\b\d{3}-\d{2}-\d{4}\b"),
        ("CREDIT_CARD", r"\b(?:\d{4}[ -]?){3}\d{4}\b"),
        ("PHONE", r"\(?\b\d{3}\)?[ .-]\d{3}[ .-]\d{4}\b|\b\d{10}\b"),
        ("IPV4", r"\b\d{1,3}(?:\.\d{1,3}){3}\b"),
    ]
    spans = [Span(m.start(), m.end(), t, m.group(), "baseline") for t, p in pats for m in re.finditer(p, text)]
    return resolve_overlaps(spans)


@dataclass
class Counts:
    tp: int = 0
    fp: int = 0
    fn: int = 0

    def prf(self) -> tuple[float, float, float]:
        p = self.tp / (self.tp + self.fp) if self.tp + self.fp else 0.0
        r = self.tp / (self.tp + self.fn) if self.tp + self.fn else 0.0
        f = 2 * p * r / (p + r) if p + r else 0.0
        return p, r, f


def evaluate_detector(docs: list[Doc], detector) -> dict:
    per_type = {t: Counts() for t in ENTITY_TYPES}
    total = Counts()
    gold_n = leaked = hard_fp = 0
    leaked_examples: list[str] = []
    fp_examples: list[str] = []
    for d in docs:
        pred = [s for s in detector(d.text) if s.type != "LITERAL"]
        gold = {(g.start, g.end, g.type) for g in d.entities}
        predset = {(s.start, s.end, s.type) for s in pred}
        for key in predset:
            c = per_type.setdefault(key[2], Counts())
            if key in gold:
                c.tp += 1
                total.tp += 1
            else:
                c.fp += 1
                total.fp += 1
        for key in gold - predset:
            per_type[key[2]].fn += 1
            total.fn += 1
        covered = [False] * len(d.text)
        for s in pred:
            for i in range(s.start, s.end):
                covered[i] = True
        for g in d.entities:
            gold_n += 1
            if not all(covered[g.start:g.end]):
                leaked += 1
                if len(leaked_examples) < 8:
                    leaked_examples.append(f"{g.type}: {g.text!r} in {d.id}")
        for s in pred:
            if not any(s.start < g.end and g.start < s.end for g in d.entities):
                hard_fp += 1
                if len(fp_examples) < 8:
                    fp_examples.append(f"{s.type}: {s.text!r} in {d.id}")
    p, r, f = total.prf()
    return {
        "precision": p, "recall": r, "f1": f,
        "per_type": {t: dict(zip(("precision", "recall", "f1"), c.prf()), support=c.tp + c.fn)
                     for t, c in per_type.items() if c.tp + c.fn + c.fp},
        "gold_entities": gold_n,
        "leak_rate": leaked / gold_n if gold_n else 0.0,
        "leaked": leaked,
        "false_positive_redactions": hard_fp,
        "leaked_examples": leaked_examples,
        "false_positive_examples": fp_examples,
    }


def evaluate_roundtrip(docs: list[Doc]) -> dict:
    exact = restored_all = 0
    for d in docs:
        s = Session()
        red = s.redact(d.text)
        back = s.rehydrate(red.text).text  # echo model: output == outbound text
        exact += back == d.text
        restored_all += all(g.text in back for g in d.entities)
    n = len(docs)
    return {"docs": n, "exact_roundtrip_rate": exact / n, "all_values_restored_rate": restored_all / n}


def evaluate_egress(docs: list[Doc]) -> dict:
    leaky_docs = leaky_flagged = clean_docs = clean_flagged = 0
    for d in docs:
        s = Session()
        red = s.redact(d.text)
        covered = [False] * len(d.text)
        for r in red.replacements:
            for i in range(r.start, r.end):
                covered[i] = True
        leaky = any(not all(covered[g.start:g.end]) for g in d.entities)
        flagged = bool(scan(red.text))
        if leaky:
            leaky_docs += 1
            leaky_flagged += flagged
        else:
            clean_docs += 1
            clean_flagged += flagged
    return {
        "docs_with_leak": leaky_docs,
        "leaky_docs_caught": leaky_flagged,
        "clean_docs": clean_docs,
        "clean_docs_falsely_flagged": clean_flagged,
    }


def run(docs: list[Doc]) -> dict:
    return {
        "docs": len(docs),
        "veilgate": evaluate_detector(docs, detect),
        "baseline_regex": evaluate_detector(docs, baseline_detect),
        "roundtrip_echo": evaluate_roundtrip(docs),
        "egress_guard": evaluate_egress(docs),
    }


def format_report(name: str, res: dict) -> str:
    lines = [f"== {name} ({res['docs']} docs) =="]
    for label in ("baseline_regex", "veilgate"):
        r = res[label]
        lines.append(
            f"{label:15s} P={r['precision']:.3f} R={r['recall']:.3f} F1={r['f1']:.3f} "
            f"leak_rate={r['leak_rate']:.3f} ({r['leaked']}/{r['gold_entities']}) "
            f"false_pos_redactions={r['false_positive_redactions']}"
        )
    lines.append("per-type (veilgate vs baseline recall):")
    vt, bt = res["veilgate"]["per_type"], res["baseline_regex"]["per_type"]
    for t in ENTITY_TYPES:
        if t in vt:
            b = bt.get(t, {"recall": 0.0, "precision": 0.0})
            lines.append(
                f"  {t:12s} support={vt[t]['support']:4d}  veilgate P={vt[t]['precision']:.3f} R={vt[t]['recall']:.3f}"
                f"   baseline P={b['precision']:.3f} R={b['recall']:.3f}"
            )
    rt = res["roundtrip_echo"]
    lines.append(f"round-trip (echo model): exact={rt['exact_roundtrip_rate']:.3f} "
                 f"all-values-restored={rt['all_values_restored_rate']:.3f}")
    eg = res["egress_guard"]
    lines.append(f"egress guard: caught {eg['leaky_docs_caught']}/{eg['docs_with_leak']} leaky docs, "
                 f"falsely flagged {eg['clean_docs_falsely_flagged']}/{eg['clean_docs']} clean docs")
    if res["veilgate"]["leaked_examples"]:
        lines.append("veilgate leak examples: " + "; ".join(res["veilgate"]["leaked_examples"][:5]))
    if res["veilgate"]["false_positive_examples"]:
        lines.append("veilgate false-positive examples: " + "; ".join(res["veilgate"]["false_positive_examples"][:5]))
    return "\n".join(lines)
