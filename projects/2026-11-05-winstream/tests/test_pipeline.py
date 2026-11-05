import os
import shutil

import pytest

from winstream.generator import generate
from winstream.model import WindowSpec, event_to_json
from winstream.pipeline import run, run_until_done
from winstream.sink import CrashInjected, Sink
from winstream.verify import check, load_events, replay_changelog

T60 = WindowSpec("tumbling", size=60)
S90 = WindowSpec("session", gap=90)


@pytest.fixture
def stream(tmp_path):
    events = generate(n_keys=4, duration=2400, seed=5)
    p = tmp_path / "events.jsonl"
    lines = [event_to_json(e) for e in events]
    lines.insert(40, "{broken")
    lines.insert(90, "")
    p.write_text("\n".join(lines) + "\n")
    return str(p)


def dump(db):
    s = Sink(db)
    try:
        return s.changelog(), s.side_outputs(), s.results()
    finally:
        s.close()


@pytest.mark.parametrize("spec", [T60, S90], ids=["tumbling", "session"])
def test_clean_run_matches_oracle(stream, tmp_path, spec):
    db = str(tmp_path / "out.db")
    rep = run(stream, db, spec, 15, 120, checkpoint_every=50)
    assert rep.flushed and rep.open_windows == 0 and rep.commits > 5
    s = Sink(db)
    summary = check(s, load_events(stream), spec, 15, 120)
    assert summary["ok"], summary
    assert [r["detail"] for r in s.side_outputs("invalid")] == ["bad json: Expecting property name enclosed in double quotes"]
    s.close()


@pytest.mark.parametrize("spec", [T60, S90], ids=["tumbling", "session"])
def test_crash_anywhere_gives_identical_output(stream, tmp_path, spec):
    ref = str(tmp_path / "ref.db")
    run(stream, ref, spec, 15, 120, checkpoint_every=37)
    want = dump(ref)
    n = sum(1 for _ in open(stream))
    records = n - 1  # one blank line in the fixture
    for crash_at in list(range(1, 80, 7)) + [records // 2, records]:  # records: crash at the very last one
        db = str(tmp_path / f"c{crash_at}.db")
        crashes, _ = run_until_done(stream, db, spec, 15, 120, checkpoint_every=37,
                                    crash_schedule=[("after", crash_at), ("commit", 2), ("after", 5)])
        assert crashes >= (2 if crash_at < records // 2 else 1)
        assert dump(db) == want, crash_at


def test_crash_inside_commit_rolls_back_everything(stream, tmp_path):
    db = str(tmp_path / "x.db")
    run(stream, db, T60, 15, 0, checkpoint_every=100, flush=False, crash_after=None)
    before = dump(db)
    shutil.copy(db, str(tmp_path / "copy.db"))
    with open(stream, "a") as fh:
        fh.write(event_to_json(load_events(stream)[-1].__class__("new1", "dev-00", 10 ** 10, 1, None)) + "\n")
    with pytest.raises(CrashInjected):
        run(stream, db, T60, 15, 0, checkpoint_every=100, flush=False, crash_in_commit=1)
    assert dump(db) == before


def test_incremental_runs_equal_one_shot(stream, tmp_path):
    lines = open(stream).read().splitlines(keepends=True)
    part = tmp_path / "growing.jsonl"
    db = str(tmp_path / "inc.db")
    for cut in (100, 333, 700, len(lines)):
        part.write_text("".join(lines[:cut]))
        run(str(part), db, S90, 15, 120, checkpoint_every=64, flush=cut == len(lines))
    ref = str(tmp_path / "ref.db")
    run(stream, ref, S90, 15, 120, checkpoint_every=64)
    a, b = dump(db), dump(ref)
    assert a[2] == b[2]          # identical results
    assert [r["kind"] for r in a[0]] == [r["kind"] for r in b[0]]


def test_refuses_mismatched_config_and_growth_after_flush(stream, tmp_path):
    db = str(tmp_path / "cfg.db")
    run(stream, db, T60, 15, 0)
    with pytest.raises(ValueError, match="refusing to resume"):
        run(stream, db, T60, 30, 0)
    run(stream, db, T60, 15, 0)  # re-running a finished job is a no-op
    with open(stream, "a") as fh:
        fh.write('{"id":"z","key":"k","ts":1,"value":1}\n')
    with pytest.raises(ValueError, match="flushed"):
        run(stream, db, T60, 15, 0)


def test_replay_changelog_detects_corruption(stream, tmp_path):
    db = str(tmp_path / "r.db")
    run(stream, db, S90, 15, 600)
    log = Sink(db).changelog()
    assert any(r["kind"] == "retract" for r in log)
    replay_changelog(log)
    i = next(i for i, r in enumerate(log) if r["kind"] == "retract")
    with pytest.raises(AssertionError, match="retract of unknown"):
        replay_changelog(log[:i - 30] + log[i:] if i >= 30 else [log[i]])

    db2 = str(tmp_path / "r2.db")
    run(stream, db2, T60, 15, 600)
    log = Sink(db2).changelog()
    bad = [dict(r) for r in log]
    i = next(i for i, r in enumerate(bad) if r["kind"] == "update")
    bad[i]["revision"] += 1
    with pytest.raises(AssertionError, match="out of sequence"):
        replay_changelog(bad)
    assert os.path.exists(db)
