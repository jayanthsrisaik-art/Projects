import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dupetrace import store
from dupetrace.api import create_app
from dupetrace.cli import main

SAMPLES = Path(__file__).resolve().parents[1] / "samples"


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "t.db"
    store.ingest_file(store.connect(p), SAMPLES / "existing_reports.jsonl")
    return p


@pytest.fixture
def client(db):
    return TestClient(create_app(db, SAMPLES / "reranker.json"))


def _new(i):
    return [json.loads(l) for l in (SAMPLES / "new_reports.jsonl").read_text().splitlines()][i]


def test_health(client):
    assert client.get("/health").json() == {"status": "ok", "reports_indexed": 6, "model_loaded": True}


def test_create_returns_suggestions_then_indexes(client):
    r = client.post("/reports", json=_new(0))
    assert r.status_code == 201
    body = r.json()
    assert body["likely_duplicate"] is True
    assert body["suggestions"][0]["id"] == "S001"
    assert "same innermost frame" in body["suggestions"][0]["reasons"]
    assert body["parsed"]["frames"][-1] == "checkout/pricing:lookup_tax_rate"
    assert client.get("/health").json()["reports_indexed"] == 7
    assert client.post("/reports", json=_new(0)).status_code == 409
    assert client.get("/reports/Q001").json()["exc_type"] == "KeyError"


def test_preview_does_not_store(client):
    r = client.post("/reports/preview", json=_new(2))
    assert r.status_code == 200 and r.json()["likely_duplicate"] is False
    assert client.get("/reports/Q003").status_code == 404


def test_sibling_bug_resolved_to_correct_report(client):
    body = client.post("/reports/preview", json=_new(3)).json()
    assert body["suggestions"][0]["id"] == "S002"  # discount_pct, not the currency_code sibling


def test_validation_errors(client):
    bad = {**_new(0), "id": "Z1", "created_at": "2026-04-10T09:00:00"}
    assert client.post("/reports", json=bad).status_code == 422
    assert client.post("/reports", json={"id": "Z2", "title": "", "body": "x"}).status_code == 422


def test_duplicate_link_feedback(client):
    assert client.post("/reports/S002/duplicate-of", json={"target_id": "S001"}).status_code == 201
    assert client.post("/reports/S002/duplicate-of", json={"target_id": "NOPE"}).status_code == 404
    assert client.post("/reports/S002/duplicate-of", json={"target_id": "S002"}).status_code == 422


def test_works_without_model(db):
    c = TestClient(create_app(db, None))
    body = c.post("/reports/preview", json=_new(0)).json()
    assert body["likely_duplicate"] is False and body["threshold"] is None
    assert body["suggestions"][0]["id"] == "S001"


def test_cli_query_matches_expected_sample_output(db, capsys):
    main(["query", str(SAMPLES / "new_reports.jsonl"), "--db", str(db), "--model", str(SAMPLES / "reranker.json")])
    got = json.loads(capsys.readouterr().out)
    assert got == json.loads((SAMPLES / "expected_suggestions.json").read_text())


def test_cli_end_to_end(tmp_path, capsys):
    main(["generate", "--out", str(tmp_path), "--bugs", "120", "--seed", "2"])
    main(["ingest", str(tmp_path / "reports.jsonl"), "--db", str(tmp_path / "x.db")])
    capsys.readouterr()
    main(["evaluate", "--db", str(tmp_path / "x.db"), "--labels", str(tmp_path / "labels.csv"),
          "--report", str(tmp_path / "m.json"), "--model", str(tmp_path / "r.json")])
    out = capsys.readouterr().out
    assert "reranker" in out and (tmp_path / "r.json").exists()
    assert set(json.loads((tmp_path / "m.json").read_text())["methods"]) == {
        "signature_exact", "bm25_raw", "bm25_normalised", "reranker"}
