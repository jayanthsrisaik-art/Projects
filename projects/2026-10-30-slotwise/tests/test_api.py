from datetime import timedelta

import pytest
from conftest import NOW, SAMPLES, utc
from fastapi.testclient import TestClient

from slotwise.app import create_app


@pytest.fixture
def client(tmp_path):
    clock = {"now": NOW}
    app = create_app(str(tmp_path / "api.db"), now=lambda: clock["now"], seed_file=str(SAMPLES / "event_types.json"))
    c = TestClient(app)
    c.clock = clock
    return c


def week(c, slug="intro-call", start="2026-11-02T05:00:00Z", end="2026-11-09T05:00:00Z"):
    r = c.get(f"/api/event-types/{slug}/slots", params={"start": start, "end": end})
    assert r.status_code == 200, r.text
    return r.json()


def book(c, start, key=None, **kw):
    body = {"event_type": "intro-call", "start": start, "name": "Ada", "email": "ada@example.com", **kw}
    return c.post("/api/bookings", json=body, headers={"Idempotency-Key": key} if key else {})


def test_event_types_and_slots_payload(client):
    assert [e["slug"] for e in client.get("/api/event-types").json()] == ["intro-call", "night-support", "office-hours"]
    data = week(client)
    assert data["host_tz"] == "America/New_York" and data["duration_min"] == 30
    first = data["slots"][0]
    assert first == {"start": "2026-11-02T14:00:00Z", "end": "2026-11-02T14:30:00Z"}
    # Mon 14 + Tue 14 + Wed 6 + Thu 14 + Fri 6 + Sat override 4 = 58
    assert len(data["slots"]) == 58


def test_book_then_slot_disappears_and_ics(client):
    r = book(client, "2026-11-02T15:00:00Z")
    assert r.status_code == 201
    body = r.json()
    assert body["booking"]["end"] == "2026-11-02T15:30:00Z" and body["replayed"] is False
    starts = [s["start"] for s in week(client)["slots"]]
    assert "2026-11-02T15:00:00Z" not in starts
    ics = client.get(f"/api/bookings/{body['booking']['id']}/ics")
    assert ics.headers["content-type"].startswith("text/calendar")
    assert "DTSTART:20261102T150000Z\r\n" in ics.text and ics.text.endswith("END:VCALENDAR\r\n")


def test_double_book_conflict_and_idempotent_retry(client):
    assert book(client, "2026-11-02T15:00:00Z", key="abc").status_code == 201
    replay = book(client, "2026-11-02T15:00:00Z", key="abc")
    assert replay.status_code == 200 and replay.json()["replayed"] is True
    other = book(client, "2026-11-02T15:00:00Z", name="Bo")
    assert other.status_code == 409 and other.json()["error"] == "slot_unavailable"
    misuse = book(client, "2026-11-03T15:00:00Z", key="abc")
    assert misuse.status_code == 409 and misuse.json()["error"] == "conflict"


def test_offset_inputs_are_normalised(client):
    r = book(client, "2026-11-02T10:00:00-05:00")
    assert r.status_code == 201 and r.json()["booking"]["start"] == "2026-11-02T15:00:00Z"


@pytest.mark.parametrize("params", [
    {"start": "2026-11-02T00:00:00", "end": "2026-11-03T00:00:00Z"},   # naive
    {"start": "2026-11-03T00:00:00Z", "end": "2026-11-02T00:00:00Z"},  # reversed
    {"start": "2026-11-01T00:00:00Z", "end": "2026-12-15T00:00:00Z"},  # too long
    {"start": "garbage", "end": "2026-11-02T00:00:00Z"},
])
def test_bad_slot_queries(client, params):
    assert client.get("/api/event-types/intro-call/slots", params=params).status_code == 422


def test_validation_and_not_found(client):
    assert book(client, "2026-11-02T15:00:00Z", email="not-an-email").status_code == 422
    assert book(client, "2026-11-02T15:00:00Z", name="   ").status_code == 422
    assert book(client, "2026-11-02T15:00:00Z", event_type="missing").status_code == 404
    assert client.get("/api/event-types/missing/slots", params={"start": "2026-11-02T00:00:00Z", "end": "2026-11-03T00:00:00Z"}).status_code == 404
    assert client.get("/api/bookings/nope").status_code == 404


def test_cancel_and_reschedule_flow(client):
    b = book(client, "2026-11-02T15:00:00Z").json()
    bid, token = b["booking"]["id"], b["token"]
    assert client.post(f"/api/bookings/{bid}/cancel", json={"token": "bad"}).status_code == 403
    moved = client.post(f"/api/bookings/{bid}/reschedule", json={"token": token, "start": "2026-11-03T19:00:00Z"})
    assert moved.status_code == 200, moved.text
    new = moved.json()
    assert client.get(f"/api/bookings/{bid}").json()["status"] == "cancelled"
    starts = [s["start"] for s in week(client)["slots"]]
    assert "2026-11-02T15:00:00Z" in starts and "2026-11-03T19:00:00Z" not in starts
    c = client.post(f"/api/bookings/{new['booking']['id']}/cancel", json={"token": new["token"]})
    assert c.json()["status"] == "cancelled"


def test_clock_moves_slots_into_notice_window(client):
    s = "2026-11-02T14:00:00Z"
    assert s in [x["start"] for x in week(client)["slots"]]
    client.clock["now"] = utc(2026, 11, 2, 11, 0)  # 3h before -> inside 4h notice
    assert s not in [x["start"] for x in week(client)["slots"]]
    assert book(client, s).status_code == 409


def test_put_event_type_validates(client):
    cfg = {"title": "Quick", "host_tz": "Asia/Kolkata", "duration_min": 15, "weekly": {"mon": [["09:00", "10:00"]]}}
    assert client.put("/api/event-types/quick", json=cfg).status_code == 200
    data = week(client, "quick", "2026-11-01T18:30:00Z", "2026-11-08T18:30:00Z")  # Kolkata week
    assert data["slots"][0]["start"] == "2026-11-02T03:30:00Z" and len(data["slots"]) == 4
    assert client.put("/api/event-types/quick", json={**cfg, "host_tz": "Nowhere/City"}).status_code == 422


def test_index_page_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "slotwise" in r.text
