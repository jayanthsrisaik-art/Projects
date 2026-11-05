"""Batch reference implementation, written separately from the operator.

It shares no code with ``engine.py``: no heaps, no state, no firing, no
merging-in-place. It answers two questions for a stream in arrival order:

1. Which (event, window) pairs must the streaming job drop as late?  This is a
   single replay of the watermark formula plus the documented drop rule (for
   sessions, "still held" is decided by re-sessionising everything accepted
   so far from scratch: slow, but obviously right).
2. What is the GROUP BY over everything that was *not* dropped (after
   de-duplicating re-delivered ids)?  For sessions this is a sort-and-sweep.

``truth(...)`` is the same GROUP BY over *all* unique events: what a batch
job that waited forever would report.
"""
from __future__ import annotations

from collections import defaultdict


def _fixed_windows(kind, size, slide, ts):
    step = size if kind == "tumbling" else slide
    out = []
    # brute force: every aligned start within one window length before ts
    start = (ts // step) * step
    while start > ts - size:
        out.append((start, start + size))
        start -= step
    return sorted(out)


def _agg(values):
    n = len(values)
    s = sum(values)
    return {"count": n, "sum": s, "min": min(values), "max": max(values),
            "mean": round(s / n, 6)}


def _group_fixed(pairs):
    groups = defaultdict(list)
    for key, w, value in pairs:
        groups[(key, w[0], w[1])].append(value)
    return {k: _agg(v) for k, v in groups.items()}


def _group_sessions(events, gap):
    """events: iterable of (key, ts, value). Sessions split where the next
    event is at or after (max ts so far + gap)."""
    by_key = defaultdict(list)
    for key, ts, value in events:
        by_key[key].append((ts, value))
    out = {}
    for key, rows in by_key.items():
        rows.sort()
        cur_start, cur_last, vals = None, None, []
        for ts, value in rows:
            if cur_start is not None and ts < cur_last + gap:
                vals.append(value)
                cur_last = max(cur_last, ts)
                continue
            if cur_start is not None:
                out[(key, cur_start, cur_last + gap)] = _agg(vals)
            cur_start, cur_last, vals = ts, ts, [value]
        if cur_start is not None:
            out[(key, cur_start, cur_last + gap)] = _agg(vals)
    return out


def _inside_live_session(accepted, key, ts, gap, wm, lateness):
    """Recompute the key's sessions from scratch over what was accepted so
    far; sessions with end + lateness <= wm are the ones the job has purged."""
    sessions = _group_sessions(((k, t, v) for k, t, v in accepted if k == key), gap)
    return any(s <= ts and ts + gap <= end and end + lateness > wm
               for (_k, s, end) in sessions)


def expected(events, spec, delay, lateness):
    """Return (results, late_pairs) the streaming job must produce.

    results: {(key, wstart, wend): {count,sum,min,max,mean}}
    late_pairs: sorted list of (event_id, wstart, wend) dropped as late.
    """
    max_ts = None
    late = []
    accepted_fixed = []
    accepted_session = []
    seen_pairs = set()
    seen_ids = set()
    for e in events:
        wm = None if max_ts is None else max_ts - delay
        if spec.kind == "session":
            if wm is not None and e.ts < wm - lateness and not _inside_live_session(
                    accepted_session, e.key, e.ts, spec.gap, wm, lateness):
                late.append((e.id, e.ts, e.ts + spec.gap))
            elif (e.key, e.id) not in seen_ids:
                seen_ids.add((e.key, e.id))
                accepted_session.append((e.key, e.ts, e.value))
        else:
            for w in _fixed_windows(spec.kind, spec.size, spec.slide, e.ts):
                if wm is not None and w[1] + lateness <= wm:
                    late.append((e.id, w[0], w[1]))
                elif (e.key, e.id, w) not in seen_pairs:
                    seen_pairs.add((e.key, e.id, w))
                    accepted_fixed.append((e.key, w, e.value))
        max_ts = e.ts if max_ts is None else max(max_ts, e.ts)
    if spec.kind == "session":
        res = _group_sessions(accepted_session, spec.gap)
    else:
        res = _group_fixed(accepted_fixed)
    return res, sorted(late)


def truth(events, spec):
    """GROUP BY over all unique events, ignoring lateness entirely."""
    uniq = {}
    for e in events:
        uniq.setdefault((e.key, e.id), e)
    if spec.kind == "session":
        return _group_sessions(((e.key, e.ts, e.value) for e in uniq.values()), spec.gap)
    pairs = [(e.key, w, e.value) for e in uniq.values()
             for w in _fixed_windows(spec.kind, spec.size, spec.slide, e.ts)]
    return _group_fixed(pairs)


def results_as_dict(rows):
    """Sink.results() rows -> same shape as expected()."""
    return {(k, s, e): {"count": c, "sum": sm, "min": mn, "max": mx, "mean": mean}
            for k, s, e, c, sm, mn, mx, mean, _rev in rows}


def compare(got: dict, want: dict) -> dict:
    """Cell-level diff summary between two result dicts."""
    missing = sorted(set(want) - set(got))
    extra = sorted(set(got) - set(want))
    wrong = sorted(k for k in set(got) & set(want) if got[k] != want[k])
    return {"windows_expected": len(want), "windows_got": len(got),
            "missing": missing, "extra": extra, "wrong": wrong,
            "ok": not (missing or extra or wrong)}
