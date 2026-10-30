"""Deterministic end-to-end demo against the real FastAPI app (fixed clock).

    python scripts/demo.py            # print the walkthrough
    python scripts/demo.py --write    # also refresh samples/expected_*.json
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from slotwise.app import create_app  # noqa: E402
from slotwise.availability import parse_instant  # noqa: E402

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
SAMPLES = ROOT / "samples"


def local(iso: str, tz: str) -> str:
    return parse_instant(iso).astimezone(ZoneInfo(tz)).strftime("%a %m-%d %H:%M %Z")


def run(write: bool = False) -> str:
    out: list[str] = []
    p = out.append
    with tempfile.TemporaryDirectory() as tmp:
        app = create_app(f"{tmp}/demo.db", now=lambda: NOW, seed_file=str(SAMPLES / "event_types.json"))
        c = TestClient(app)
        p(f"now = {NOW.isoformat()} (Fri 08:00 in New York)")

        p("\n# 1. Fall-back Sunday: the 00:00-04:00 night window holds 5 real hours")
        r = c.get("/api/event-types/night-support/slots",
                  params={"start": "2026-11-01T00:00:00Z", "end": "2026-11-02T00:00:00Z"}).json()
        for s in r["slots"]:
            p(f"  {s['start']}  host {local(s['start'], 'America/New_York')}")
        if write:
            (SAMPLES / "expected_night_support_2026-11-01.json").write_text(json.dumps(r, indent=2) + "\n")

        p("\n# 2. Intro call, Mon 2026-11-02, seen from Tokyo")
        req = {"start": "2026-11-02T05:00:00Z", "end": "2026-11-03T05:00:00Z"}
        r = c.get("/api/event-types/intro-call/slots", params=req).json()
        p(f"  {len(r['slots'])} slots; first {local(r['slots'][0]['start'], 'Asia/Tokyo')}, "
          f"last {local(r['slots'][-1]['start'], 'Asia/Tokyo')}")
        if write:
            (SAMPLES / "slots_request.json").write_text(json.dumps({"event_type": "intro-call", **req}, indent=2) + "\n")
            (SAMPLES / "expected_slots_intro_call_2026-11-02.json").write_text(json.dumps(r, indent=2) + "\n")

        p("\n# 3. Twelve clients race for Mon 10:00 New York")
        codes: list[int] = []
        barrier = threading.Barrier(12)

        def racer(i: int) -> None:
            barrier.wait()
            res = c.post("/api/bookings", json={"event_type": "intro-call", "start": "2026-11-02T15:00:00Z",
                                                "name": f"racer{i}", "email": f"r{i}@example.com"})
            codes.append(res.status_code)

        ts = [threading.Thread(target=racer, args=(i,)) for i in range(12)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        p(f"  201 Created: {codes.count(201)}   409 slot_unavailable: {codes.count(409)}")
        after = c.get("/api/event-types/intro-call/slots", params=req).json()["slots"]
        p(f"  slots left that day: {len(after)} (10:00 taken, 09:30 and 10:30 lost to the 15-min buffer)")

        p("\n# 4. Retry with the same Idempotency-Key replays instead of double-booking")
        body = {"event_type": "intro-call", "start": "2026-11-03T14:00:00Z", "name": "Ada", "email": "ada@example.com"}
        first = c.post("/api/bookings", json=body, headers={"Idempotency-Key": "demo-1"})
        again = c.post("/api/bookings", json=body, headers={"Idempotency-Key": "demo-1"})
        p(f"  first: {first.status_code}  retry: {again.status_code} replayed={again.json()['replayed']} "
          f"same id={first.json()['booking']['id'] == again.json()['booking']['id']}")

        p("\n# 5. Reschedule Ada atomically to Tue 13:00, then cancel")
        b = first.json()
        moved = c.post(f"/api/bookings/{b['booking']['id']}/reschedule",
                       json={"token": b["token"], "start": "2026-11-03T18:00:00Z"}).json()
        old = c.get(f"/api/bookings/{b['booking']['id']}").json()
        p(f"  old status={old['status']} -> new start {local(moved['booking']['start'], 'America/New_York')}")
        bad = c.post(f"/api/bookings/{moved['booking']['id']}/cancel", json={"token": "guess"})
        ok = c.post(f"/api/bookings/{moved['booking']['id']}/cancel", json={"token": moved["token"]})
        p(f"  cancel with wrong token: {bad.status_code}; with token: {ok.status_code} status={ok.json()['status']}")

        p("\n# 6. Office hours (host in London, 15:00-17:00 GMT) seen from New York")
        r = c.get("/api/event-types/office-hours/slots",
                  params={"start": "2026-11-03T00:00:00Z", "end": "2026-11-04T00:00:00Z"}).json()
        p("  " + ", ".join(local(s["start"], "America/New_York")[-9:] for s in r["slots"]))
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    write = "--write" in sys.argv
    text = run(write)
    print(text, end="")
    if write:
        (SAMPLES / "demo_output.txt").write_text(text)
