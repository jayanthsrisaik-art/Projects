import json
import subprocess
import sys

from conftest import ROOT, SAMPLES


def test_demo_output_matches_sample():
    proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "demo.py")], capture_output=True, text=True, check=True)
    assert proc.stdout == (SAMPLES / "demo_output.txt").read_text()


def test_expected_slot_samples_match_api(tmp_path):
    from datetime import datetime, timezone
    from fastapi.testclient import TestClient
    from slotwise.app import create_app

    now = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)
    c = TestClient(create_app(str(tmp_path / "s.db"), now=lambda: now, seed_file=str(SAMPLES / "event_types.json")))
    req = json.loads((SAMPLES / "slots_request.json").read_text())
    got = c.get(f"/api/event-types/{req['event_type']}/slots", params={"start": req["start"], "end": req["end"]}).json()
    assert got == json.loads((SAMPLES / "expected_slots_intro_call_2026-11-02.json").read_text())
    got = c.get("/api/event-types/night-support/slots",
                params={"start": "2026-11-01T00:00:00Z", "end": "2026-11-02T00:00:00Z"}).json()
    assert got == json.loads((SAMPLES / "expected_night_support_2026-11-01.json").read_text())
