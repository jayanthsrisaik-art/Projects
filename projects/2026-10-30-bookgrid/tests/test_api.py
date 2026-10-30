import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bookgrid.api import create_app

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def client(tmp_path):
    c = TestClient(create_app(str(tmp_path / "api.db")))
    r = c.post("/api/resources", json={"name": "Thames", "timezone": "Europe/London",
                                       "buffer_minutes": 10})
    assert r.status_code == 201
    return c


def booking(**kw):
    body = {"resource_id": 1, "title": "Sync", "organizer": "ana", "date": "2026-11-02",
            "start_time": "10:00", "duration_minutes": 30}
    body.update(kw)
    return body


def test_resources_roundtrip(client):
    [res] = client.get("/api/resources").json()
    assert res["open_days"] == ["MO", "TU", "WE", "TH", "FR"] and res["buffer_minutes"] == 10
    r = client.post("/api/resources", json={"name": "Bad", "timezone": "Nowhere/Land"})
    assert r.status_code == 422 and "timezone" in r.json()["error"]


def test_book_preview_conflict_flow(client):
    pv = client.post("/api/bookings/preview", json=booking(rrule="FREQ=DAILY;COUNT=3")).json()
    assert pv["bookable"] == 3 and pv["blocked"] == 0
    assert client.post("/api/bookings", json=booking()).status_code == 201
    pv = client.post("/api/bookings/preview", json=booking(rrule="FREQ=DAILY;COUNT=3")).json()
    assert pv["blocked"] == 1 and pv["occurrences"][0]["problems"][0]["kind"] == "conflict"
    r = client.post("/api/bookings", json=booking(title="Other"))
    assert r.status_code == 409
    assert r.json()["details"][0]["problems"][0]["with"][0]["title"] == "Sync"


def test_idempotency_header(client):
    h = {"Idempotency-Key": "abc-12345"}
    a = client.post("/api/bookings", json=booking(), headers=h)
    b = client.post("/api/bookings", json=booking(), headers=h)
    assert a.status_code == b.status_code == 201
    assert b.headers.get("Idempotent-Replayed") == "true" and a.json() == b.json()
    c = client.post("/api/bookings", json=booking(start_time="15:00"), headers=h)
    assert c.status_code == 422
    assert client.post("/api/bookings", json=booking(), headers={"Idempotency-Key": "x"}).status_code == 422


def test_listing_move_cancel(client):
    sid = client.post("/api/bookings", json=booking(rrule="FREQ=WEEKLY;COUNT=3")).json()["series_id"]
    occ = client.get("/api/resources/1/occurrences", params={"start": "2026-11-01", "end": "2026-11-30"}).json()
    assert [o["recurrence_date"] for o in occ] == ["2026-11-02", "2026-11-09", "2026-11-16"]
    moved = client.patch(f"/api/occurrences/{occ[1]['id']}", json={"date": "2026-11-10", "start_time": "11:00"})
    assert moved.status_code == 200 and moved.json()["local_start"] == "2026-11-10T11:00+00:00"
    assert client.post(f"/api/occurrences/{occ[0]['id']}/cancel", json={"scope": "this"}).json()["cancelled"] == 1
    series = client.get(f"/api/series/{sid}").json()
    assert [o["status"] for o in series["occurrences"]] == ["cancelled", "active", "active"]
    assert client.post(f"/api/occurrences/{occ[0]['id']}/cancel", json={"scope": "nope"}).status_code == 422
    assert client.get("/api/occurrences/999").status_code == 404


def test_free_endpoint(client):
    client.post("/api/bookings", json=booking())
    free = client.get("/api/resources/1/free", params={"date": "2026-11-02", "duration": 60, "step": 60}).json()
    assert [s["local_start"][11:16] for s in free["slots"]] == [
        "08:00", "11:00", "12:00", "13:00", "14:00", "15:00", "16:00", "17:00"]
    assert client.get("/api/resources/1/free", params={"date": "2026-11-02", "step": 7}).status_code == 422


def test_pydantic_validation(client):
    assert client.post("/api/bookings", json=booking(on_conflict="maybe")).status_code == 422
    assert client.post("/api/bookings", json={"title": "x"}).status_code == 422


def test_samples_match_expected():
    result = subprocess.run([sys.executable, str(ROOT / "samples" / "run_samples.py")],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
