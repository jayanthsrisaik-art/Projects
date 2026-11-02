import json
import os
import subprocess
import sys

from sketchroll import bench, synth
from sketchroll.events import hour_floor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_synth_is_deterministic_and_has_returning_users():
    a = synth.generate(days=2, events_per_day=2000, seed=5)
    b = synth.generate(days=2, events_per_day=2000, seed=5)
    assert a == b
    by_day, late, incidents = a
    evs = [e for d in by_day for e in d] + late
    hours_per_user = {}
    for e in evs:
        hours_per_user.setdefault(e.user_id, set()).add(hour_floor(e.ts))
    summed_hourly_distincts = sum(len(h) for h in hours_per_user.values())
    # Repeat visitors are what break summed distincts: the naive answer must be well off.
    assert summed_hourly_distincts / len(hours_per_user) > 1.3  # measured 1.46 here
    assert late and len(incidents) == 6


def test_bench_sketches_beat_naive_rollups_and_stay_within_bounds():
    r = bench.run(days=4, events_per_day=4000, seed=3, keep_hourly_days=1)
    assert r["invariant_violations"] == []
    assert r["exact_counters_match"] is True
    assert r["late_ingest"]["status"] == "ingested" and r["late_replay_status"] == "skipped_duplicate"
    sk, nv = r["errors"]["sketch"], r["errors"]["naive"]
    assert sk["distinct_users"]["max"] < 0.07          # HLL p=12: 1.6% std error
    assert nv["distinct_users"]["max"] > 1.0           # summed hourly distincts overcount > 2x somewhere
    for m in ("latency_p50_ms", "latency_p95_ms", "latency_p99_ms"):
        assert sk[m]["within_alpha"] == 1.0            # DDSketch guarantee holds after merges
    assert nv["latency_p99_ms"]["median"] > sk["latency_p99_ms"]["median"]


def test_cli_end_to_end(tmp_path):
    env = {**os.environ, "PYTHONPATH": ROOT}
    data, db = tmp_path / "data", tmp_path / "x.db"

    def run(*args, ok=True):
        p = subprocess.run([sys.executable, "-m", "sketchroll", *args], capture_output=True, text=True, env=env)
        assert (p.returncode == 0) == ok, p.stderr
        return p

    meta = json.loads(run("generate", "--out", str(data), "--days", "2", "--events-per-day", "300").stdout)
    run("ingest", "--db", str(db), *meta["daily_files"])
    run("compact", "--db", str(db), "--before", "2026-10-02T00:00:00Z")
    out = json.loads(run("query", "--db", str(db), "--start", "2026-10-01T00:00:00Z",
                         "--end", "2026-10-03T00:00:00Z").stdout)
    assert out["requests"] == meta["events"] - meta["late_events"]
    p = run("query", "--db", str(db), "--start", "2026-10-01T01:00:00Z", "--end", "2026-10-01T03:00:00Z", ok=False)
    assert p.returncode == 2 and "compacted" in p.stderr
    assert json.loads(run("check", "--db", str(db)).stdout)["ok"] is True
