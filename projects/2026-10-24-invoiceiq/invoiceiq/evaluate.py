"""Measured comparison of three pipeline modes on seeded synthetic sets.

single_pass : strict extraction, accept everything (no validation)
validated   : strict extraction, accept only if validation passes
repair      : full loop (up to 3 attempts, errors fed back)

'correct' means the normalized output equals the gold label exactly.
The local rules were written while looking at the dev set (seed 1);
test (seed 2) was generated afterwards and never inspected.
"""
from collections import defaultdict

from .gen import make_set
from .pipeline import Result, normalize, run

SETS = {"dev": (1, 60), "test": (2, 200)}
FIELDS = ["invoice_number", "invoice_date", "vendor", "currency", "line_items", "subtotal", "tax", "total"]


def _modes(extractor, text):
    first = run(extractor, text, max_attempts=1)
    single = Result(first.data, [], 1, "ok")
    return {"single_pass": single, "validated": first, "repair": run(extractor, text, max_attempts=3)}


def evaluate(extractor, set_name: str = "test", corrupt_p: float = 0.25) -> dict:
    seed, n = SETS[set_name]
    docs = make_set(seed, n, corrupt_p)
    agg = {m: defaultdict(int) for m in ("single_pass", "validated", "repair")}
    fields = defaultdict(int)
    for d in docs:
        for mode, r in _modes(extractor, d.text).items():
            ok = r.data == normalize(d.gold)
            a = agg[mode]
            a["correct"] += ok
            a["accepted"] += r.status == "ok"
            a["accepted_correct"] += ok and r.status == "ok"
            a["silent_errors"] += (not ok) and r.status == "ok"
            if d.corrupted:
                a["corrupted"] += 1
                a["corrupted_flagged"] += r.status != "ok"
            if mode == "repair":
                for f in FIELDS:
                    fields[f] += r.data.get(f) == normalize(d.gold)[f]
    out = {"set": set_name, "n": n, "extractor": extractor.name, "modes": {}}
    for mode, a in agg.items():
        acc = a["accepted"]
        out["modes"][mode] = {
            "accuracy": a["correct"] / n,
            "coverage": acc / n,
            "accepted_precision": (a["accepted_correct"] / acc) if acc else None,
            "silent_errors": a["silent_errors"],
            "corrupted_docs": a["corrupted"],
            "corrupted_flagged": a["corrupted_flagged"],
        }
    out["field_accuracy_repair"] = {f: fields[f] / n for f in FIELDS}
    return out


def format_report(rep: dict) -> str:
    L = [f"set={rep['set']} n={rep['n']} extractor={rep['extractor']}",
         f"{'mode':<12}{'accuracy':>9}{'coverage':>9}{'acc.prec':>9}{'silent':>7}{'corrupt flagged':>17}"]
    for m, v in rep["modes"].items():
        p = "n/a" if v["accepted_precision"] is None else f"{v['accepted_precision']:.3f}"
        L.append(f"{m:<12}{v['accuracy']:>9.3f}{v['coverage']:>9.3f}{p:>9}{v['silent_errors']:>7}"
                 f"{v['corrupted_flagged']:>9}/{v['corrupted_docs']:<7}")
    L.append("repair per-field accuracy: " + ", ".join(f"{k}={v:.3f}" for k, v in rep["field_accuracy_repair"].items()))
    return "\n".join(L)
