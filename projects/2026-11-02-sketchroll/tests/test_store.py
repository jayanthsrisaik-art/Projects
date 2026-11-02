import json
import os
from datetime import datetime, timezone

import pytest

from sketchroll import store
from sketchroll.events import read_jsonl
from sketchroll.oracle import exact

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLES = os.path.join(os.path.dirname(HERE), "samples")
DAY1 = os.path.join(SAMPLES, "events-2026-10-01.jsonl")
DAY2 = os.path.join(SAMPLES, "events-2026-10-02.jsonl")
LATE = os.path.join(SAMPLES, "late.jsonl")
BAD = os.path.join(SAMPLES, "bad_batch.jsonl")


def T(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def events(*paths):
    return [e for p in paths for _, _, e in read_jsonl(p) if not isinstance(e, Exception)]


@pytest.fixture
def conn(tmp_path):
    c = store.connect(str(tmp_path / "r.db"))
    yield c
    c.close()


def table(conn):
    return conn.execute("SELECT * FROM rollups ORDER BY endpoint, grain, start").fetchall()


def test_ingest_is_idempotent_per_file_content(conn):
    first = store.ingest_file(conn, DAY1)
    snapshot = table(conn)
    again = store.ingest_file(conn, DAY1)
    assert first.status == "ingested" and first.accepted == len(events(DAY1))
    assert again.status == "skipped_duplicate"
    assert table(conn) == snapshot
    assert store.check_invariants(conn) == []


def test_same_batch_id_with_different_content_is_rejected(conn):
    store.ingest_file(conn, DAY1, batch_id="2026-10-01")
    with pytest.raises(store.BatchConflict):
        store.ingest_file(conn, DAY2, batch_id="2026-10-01")


def test_bad_rows_are_quarantined_with_reasons(conn):
    r = store.ingest_file(conn, BAD)
    assert (r.accepted, r.rejected) == (2, 7)
    reasons = [row[0] for row in conn.execute("SELECT reason FROM quarantine ORDER BY line_no")]
    assert any("no timezone" in x for x in reasons)
    assert any("user_id" in x for x in reasons)
    assert any("endpoint" in x for x in reasons)
    assert any("latency_ms" in x for x in reasons)
    assert any("status" in x for x in reasons)
    assert any("missing field" in x for x in reasons)
    assert any("invalid JSON" in x for x in reasons)
    # The +02:00 event is normalised to 10:22 UTC and lands in the 10:00 hour.
    starts = {row[0] for row in conn.execute("SELECT start FROM rollups WHERE endpoint='/cart'")}
    assert starts == {"2026-10-02T10:00:00Z"}


def test_exact_counters_and_sketch_accuracy_vs_truth(conn):
    for p in (DAY1, DAY2, LATE):
        store.ingest_file(conn, p)
    evs = events(DAY1, DAY2, LATE)
    for scope in (None, ["/search"], ["/checkout", "/login"]):
        for s, e in [("2026-10-01T00:00:00Z", "2026-10-03T00:00:00Z"),
                     ("2026-10-01T13:00:00Z", "2026-10-02T02:00:00Z")]:
            truth = exact(evs, T(s), T(e), scope)
            r = store.query(conn, T(s), T(e), scope)
            assert (r.n, r.errors) == (truth["requests"], truth["errors"])
            assert r.lat_max == truth["latency_max_ms"] and r.lat_min == truth["latency_min_ms"]
            assert r.lat_sum == pytest.approx(truth["latency_sum_ms"])
            assert abs(r.users.estimate() - truth["distinct_users"]) <= max(2, 0.05 * truth["distinct_users"])
            for q in (0.5, 0.95, 0.99):
                t = truth[f"latency_p{int(q * 100)}_ms"]
                assert abs(r.latency.quantile(q) - t) / t <= 0.01 + 1e-12


def _all_hour_windows():
    return [(T("2026-10-01T00:00:00Z"), T("2026-10-02T00:00:00Z")),
            (T("2026-10-02T00:00:00Z"), T("2026-10-03T00:00:00Z")),
            (T("2026-10-01T00:00:00Z"), T("2026-10-03T00:00:00Z"))]


def test_compaction_preserves_day_aligned_answers_exactly(conn):
    store.ingest_file(conn, DAY1)
    store.ingest_file(conn, DAY2)
    before = [store.query(conn, s, e).summary() for s, e in _all_hour_windows()]
    written = store.compact(conn, T("2026-10-03T00:00:00Z"))
    assert written == 10  # 5 endpoints x 2 days
    after = [store.query(conn, s, e).summary() for s, e in _all_hour_windows()]
    assert before == after
    assert conn.execute("SELECT COUNT(*) FROM rollups WHERE grain='hour'").fetchone()[0] == 0
    assert store.check_invariants(conn) == []


def test_compaction_only_touches_complete_days_before_cutoff(conn):
    store.ingest_file(conn, DAY1)
    store.ingest_file(conn, DAY2)
    store.compact(conn, T("2026-10-02T12:00:00Z"))  # day 2 does not end before the cutoff
    grains = dict(conn.execute("SELECT substr(start,1,10), group_concat(DISTINCT grain) FROM rollups GROUP BY 1"))
    assert grains == {"2026-10-01": "day", "2026-10-02": "hour"}


def test_late_data_after_compaction_merges_into_day_row(conn):
    store.ingest_file(conn, DAY1)
    store.ingest_file(conn, DAY2)
    store.compact(conn, T("2026-10-02T00:00:00Z"))
    r = store.ingest_file(conn, LATE)
    assert r.accepted == len(events(LATE))
    assert store.check_invariants(conn) == []
    evs = events(DAY1, DAY2, LATE)
    whole = exact(evs, T("2026-10-01T00:00:00Z"), T("2026-10-03T00:00:00Z"))
    got = store.query(conn, T("2026-10-01T00:00:00Z"), T("2026-10-03T00:00:00Z"))
    assert got.n == whole["requests"]
    # Same final state as ingesting everything first and compacting afterwards.
    other = store.connect(":memory:")
    for p in (DAY1, DAY2, LATE):
        store.ingest_file(other, p)
    store.compact(other, T("2026-10-02T00:00:00Z"))
    assert table(other) == table(conn)


def test_partial_query_over_compacted_day_is_refused(conn):
    store.ingest_file(conn, DAY1)
    store.ingest_file(conn, DAY2)
    store.compact(conn, T("2026-10-02T00:00:00Z"))
    with pytest.raises(store.GranularityError):
        store.query(conn, T("2026-10-01T06:00:00Z"), T("2026-10-01T12:00:00Z"))
    with pytest.raises(store.GranularityError):
        store.query(conn, T("2026-10-01T06:00:00Z"), T("2026-10-02T06:00:00Z"))
    # Hourly precision is still available for the uncompacted day.
    assert store.query(conn, T("2026-10-02T06:00:00Z"), T("2026-10-02T12:00:00Z")).n > 0


def test_query_validates_bounds(conn):
    with pytest.raises(ValueError):
        store.query(conn, T("2026-10-01T00:30:00Z"), T("2026-10-01T02:00:00Z"))
    with pytest.raises(ValueError):
        store.query(conn, T("2026-10-01T02:00:00Z"), T("2026-10-01T02:00:00Z"))


def test_empty_range_summary(conn):
    store.ingest_file(conn, DAY1)
    s = store.query(conn, T("2027-01-01T00:00:00Z"), T("2027-01-02T00:00:00Z")).summary()
    assert s["requests"] == 0 and s["latency_p95_ms"] is None


def test_invariant_checker_detects_corruption(conn):
    store.ingest_file(conn, DAY1)
    conn.execute("UPDATE rollups SET n = n + 1 WHERE rowid = (SELECT MIN(rowid) FROM rollups)")
    problems = store.check_invariants(conn)
    assert any("sketch count" in p for p in problems)
    assert any("ledger" in p for p in problems)


def test_failed_batch_leaves_no_partial_state(conn, tmp_path, monkeypatch):
    store.ingest_file(conn, DAY1)
    snapshot = table(conn)
    real_put = store._put
    calls = {"n": 0}

    def flaky_put(*a, **k):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("disk full")
        return real_put(*a, **k)

    monkeypatch.setattr(store, "_put", flaky_put)
    with pytest.raises(RuntimeError):
        store.ingest_file(conn, DAY2)
    monkeypatch.setattr(store, "_put", real_put)
    assert table(conn) == snapshot
    assert conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 1
    assert store.ingest_file(conn, DAY2).status == "ingested"  # retry works


def test_sample_expected_outputs_reproduce(tmp_path):
    from sketchroll.cli import main
    import contextlib
    import io

    db = str(tmp_path / "s.db")
    with contextlib.redirect_stdout(io.StringIO()):
        main(["ingest", "--db", db, DAY1, DAY2, BAD, DAY1])
        main(["compact", "--db", db, "--before", "2026-10-02T00:00:00Z"])
        main(["ingest", "--db", db, LATE])
    for name, args in [
        ("expected_query_all.json", ["--start", "2026-10-01T00:00:00Z", "--end", "2026-10-03T00:00:00Z"]),
        ("expected_query_cart_window.json",
         ["--start", "2026-10-02T10:00:00Z", "--end", "2026-10-02T12:00:00Z", "--endpoint", "/cart"]),
    ]:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            assert main(["query", "--db", db, *args]) == 0
        with open(os.path.join(SAMPLES, name)) as fh:
            assert json.loads(buf.getvalue()) == json.load(fh)
