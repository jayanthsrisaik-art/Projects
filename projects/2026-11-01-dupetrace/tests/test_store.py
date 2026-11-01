import json
from pathlib import Path

import pytest

from dupetrace import store

SAMPLES = Path(__file__).resolve().parents[1] / "samples"


@pytest.fixture
def conn():
    return store.connect(":memory:")


def test_ingest_is_idempotent(conn):
    first = store.ingest_file(conn, SAMPLES / "existing_reports.jsonl")
    again = store.ingest_file(conn, SAMPLES / "existing_reports.jsonl")
    assert first.as_dict() == {"inserted": 6, "unchanged": 0, "conflicts": 0, "quarantined": 0}
    assert again.as_dict() == {"inserted": 0, "unchanged": 6, "conflicts": 0, "quarantined": 0}
    assert len(store.load_reports(conn)) == 6


def test_bad_rows_quarantined_and_conflicts_keep_first_write(conn):
    store.ingest_file(conn, SAMPLES / "existing_reports.jsonl")
    stats = store.ingest_file(conn, SAMPLES / "bad_reports.jsonl")
    assert stats.as_dict() == {"inserted": 0, "unchanged": 1, "conflicts": 1, "quarantined": 3}
    reasons = [r[0] for r in conn.execute("SELECT reason FROM quarantine ORDER BY line_no")]
    assert reasons[0].startswith("bad created_at")
    assert reasons[1] == "missing or empty field: title"
    assert reasons[2].startswith("invalid JSON")
    assert store.get_report(conn, "S001").title == "Coupon fails with KeyError at checkout"


def test_parsed_fields_persisted(conn):
    store.ingest_file(conn, SAMPLES / "existing_reports.jsonl")
    r = store.get_report(conn, "S003")
    assert r.exc_type == "TypeError"
    assert r.frames[-1] == "search/filters:parseFacet"
    assert r.component == "search"


def test_load_reports_in_arrival_order(conn):
    lines = [json.dumps({"id": i, "created_at": t, "title": "x", "body": "y"})
             for i, t in [("b", "2026-01-02T00:00:00Z"), ("a", "2026-01-01T00:00:00+02:00"), ("c", "2026-01-01T12:00:00Z")]]
    store.ingest_lines(conn, lines)
    assert [r.id for r in store.load_reports(conn)] == ["a", "c", "b"]
    assert store.get_report(conn, "a").created_at == "2025-12-31T22:00:00Z"


def test_link_duplicate_rejects_self(conn):
    store.ingest_file(conn, SAMPLES / "existing_reports.jsonl")
    with pytest.raises(ValueError):
        store.link_duplicate(conn, "S001", "S001")
    store.link_duplicate(conn, "S002", "S001")
    store.link_duplicate(conn, "S002", "S001")
    assert store.duplicate_links(conn) == [("S002", "S001")]
