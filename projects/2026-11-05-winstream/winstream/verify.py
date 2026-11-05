"""Checks a finished sink database against the batch oracle."""
from __future__ import annotations

from .model import parse_event
from .oracle import compare, expected, results_as_dict


def load_events(path):
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                out.append(parse_event(line))
            except ValueError:
                continue  # invalid rows are side outputs, not part of the oracle input
    return out


def replay_changelog(changelog):
    """Materialise a changelog independently of the sink's results table."""
    state = {}
    for r in changelog:
        k = (r["key"], r["wstart"], r["wend"])
        if r["kind"] == "retract":
            if k not in state:
                raise AssertionError(f"retract of unknown window {k} at seq {r['seq']}")
            del state[k]
        elif r["kind"] == "insert":
            if k in state:
                raise AssertionError(f"insert of existing window {k} at seq {r['seq']}")
            state[k] = r
        else:
            if k not in state or r["revision"] != state[k]["revision"] + 1:
                raise AssertionError(f"update out of sequence for {k} at seq {r['seq']}")
            state[k] = r
    return {k: {c: v[c] for c in ("count", "sum", "min", "max", "mean")} for k, v in state.items()}


def check(sink, events, spec, delay, lateness) -> dict:
    want, want_late = expected(events, spec, delay, lateness)
    got = results_as_dict(sink.results())
    res = compare(got, want)
    late_got = sorted((s["event_id"], s["wstart"], s["wend"]) for s in sink.side_outputs("late"))
    changelog = sink.changelog()
    premature = [r["seq"] for r in changelog
                 if r["kind"] == "insert" and r["wm"] is not None and r["wm"] < r["wend"]]
    try:
        replayed = replay_changelog(changelog)
        replay_ok = replayed == got
        replay_error = None
    except AssertionError as exc:
        replay_ok, replay_error = False, str(exc)
    seqs = [r["seq"] for r in changelog]
    return {
        "results_match_oracle": res["ok"],
        "windows": res["windows_expected"],
        "missing": len(res["missing"]), "extra": len(res["extra"]), "wrong": len(res["wrong"]),
        "late_pairs_expected": len(want_late), "late_pairs_got": len(late_got),
        "late_pairs_match": late_got == want_late,
        "changelog_rows": len(changelog),
        "changelog_replay_matches_results": replay_ok,
        "changelog_replay_error": replay_error,
        "changelog_seq_contiguous": seqs == list(range(1, len(seqs) + 1)),
        "premature_inserts": len(premature),
        "ok": bool(res["ok"] and late_got == want_late and replay_ok and not premature
                   and seqs == list(range(1, len(seqs) + 1))),
    }
