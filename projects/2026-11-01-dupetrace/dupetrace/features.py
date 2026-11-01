"""Pairwise features for (query report, candidate report).

All features are similarity measures between two reports; none encode report or
bug identity, so a model trained on early bugs transfers to bugs it never saw.
"""

from __future__ import annotations

import math
import re

from .normalize import tokens
from .store import Report

FEATURES = [
    "bm25_rel",          # candidate BM25 score / best score in this candidate list
    "bm25_self",         # candidate BM25 score / the query's self-match score (corpus-size robust)
    "inv_rank",          # 1 / BM25 rank
    "title_jaccard",
    "msg_both",          # both reports carry an exception message
    "msg_jaccard",       # token Jaccard of placeholder-normalised messages (0 if not msg_both)
    "msg_exact",         # identical normalised messages
    "exc_both",
    "exc_match",
    "exc_conflict",      # both have an exception type and they differ
    "frames_both",
    "frame_jaccard",
    "top_frame_match",
    "frame_overlap_q",   # share of the query's frames found in the candidate (robust to truncation)
    "component_match",   # both known and equal
    "component_conflict",
    "log_days_gap",
    "version_gap",
]


def _jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def _minor(version: str | None) -> int | None:
    m = re.match(r"\s*(\d+)\.(\d+)", version or "")
    return int(m[1]) * 100 + int(m[2]) if m else None


def pair_features(q: Report, c: Report, score: float, best: float, rank: int, self_score: float) -> list[float]:
    msg_both = bool(q.norm_message and c.norm_message)
    exc_both = bool(q.exc_type and c.exc_type)
    frames_both = bool(q.frames and c.frames)
    qf, cf = set(q.frames), set(c.frames)
    comp_known = q.component != "unknown" and c.component != "unknown"
    qm, cm = _minor(q.version), _minor(c.version)
    gap_days = abs((q.ts - c.ts).total_seconds()) / 86400
    return [
        score / best if best > 0 else 0.0,
        min(score / self_score, 2.0) if self_score > 0 else 0.0,
        1.0 / rank,
        _jaccard(set(tokens(q.title)), set(tokens(c.title))),
        float(msg_both),
        _jaccard(set(tokens(q.norm_message)), set(tokens(c.norm_message))) if msg_both else 0.0,
        float(msg_both and q.norm_message == c.norm_message),
        float(exc_both),
        float(exc_both and q.exc_type == c.exc_type),
        float(exc_both and q.exc_type != c.exc_type),
        float(frames_both),
        _jaccard(qf, cf) if frames_both else 0.0,
        float(frames_both and q.top_frame == c.top_frame),
        len(qf & cf) / len(qf) if frames_both else 0.0,
        float(comp_known and q.component == c.component),
        float(comp_known and q.component != c.component),
        math.log1p(gap_days),
        float(abs(qm - cm)) if qm is not None and cm is not None else 0.0,
    ]


def explain(q: Report, c: Report) -> list[str]:
    """Human-readable reasons shown to the triager next to a suggestion."""
    out = []
    if q.exc_type and q.exc_type == c.exc_type:
        out.append(f"same exception type {q.exc_type}")
    if q.norm_message and q.norm_message == c.norm_message:
        out.append(f"identical normalised message: {q.norm_message!r}")
    shared = [f for f in q.frames if f in set(c.frames)]
    if shared:
        out.append(f"{len(shared)} shared frame(s), e.g. {shared[-1]}")
    if q.frames and q.top_frame == c.top_frame:
        out.append("same innermost frame")
    if q.component != "unknown" and q.component == c.component:
        out.append(f"same component {q.component}")
    return out
