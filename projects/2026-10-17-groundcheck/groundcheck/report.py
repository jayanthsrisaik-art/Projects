"""Answer-level checking and report rendering."""
from dataclasses import asdict, dataclass

from .lexical import CONTRADICTED, SUPPORTED, UNSUPPORTED
from .text import split_sentences


@dataclass
class Verdict:
    claim: str
    label: str
    reason: str
    source: str | None
    evidence: str | None


def check_answer(answer: str, sources: dict[str, str], verifier) -> dict:
    verdicts = []
    for claim in split_sentences(answer):
        j = verifier.verify(claim, sources)
        verdicts.append(Verdict(claim, j.label, j.reason,
                                j.evidence.source if j.evidence else None,
                                j.evidence.sentence if j.evidence else None))
    n = len(verdicts)
    counts = {l: sum(v.label == l for v in verdicts) for l in (SUPPORTED, UNSUPPORTED, CONTRADICTED)}
    return {
        "verifier": verifier.name,
        "faithfulness": round(counts[SUPPORTED] / n, 4) if n else None,
        "counts": counts,
        "claims": [asdict(v) for v in verdicts],
    }


def render_text(report: dict) -> str:
    mark = {SUPPORTED: "OK ", UNSUPPORTED: "?? ", CONTRADICTED: "XX "}
    lines = [f"verifier={report['verifier']} faithfulness={report['faithfulness']} {report['counts']}"]
    for c in report["claims"]:
        lines.append(f"{mark[c['label']]}{c['label']:<12} {c['claim']}  ({c['reason']})")
    return "\n".join(lines)
