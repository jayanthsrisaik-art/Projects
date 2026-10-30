"""The browser groups slots into visitor-local days with Intl; the server
computes them with zoneinfo. Both must agree, including across DST weeks."""
import json
import shutil
import subprocess
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from conftest import ROOT

from slotwise.availability import compute_slots, iso_z

WEB = ROOT / "web"
VISITOR_TZS = ["America/New_York", "America/Los_Angeles", "Europe/London", "Asia/Kolkata",
               "Asia/Tokyo", "Australia/Lord_Howe", "Pacific/Kiritimati", "America/Santiago", "UTC"]
WEEKS = ["2026-03-02", "2026-03-23", "2026-09-07", "2026-10-26", "2026-11-02"]


@pytest.fixture(scope="module")
def node_dist():
    if not shutil.which("node"):
        pytest.skip("node not installed")
    tsc = WEB / "node_modules" / ".bin" / "tsc"
    if tsc.exists():
        subprocess.run([str(tsc), "-p", str(WEB / "tsconfig.json")], check=True)
    if not (WEB / "dist" / "time.js").exists():
        pytest.skip("web/dist not built (run npm install && npm run build in web/)")


def start_of_local_day(d: date, z: ZoneInfo) -> datetime:
    """First UTC minute whose local date is d (brute force, UTC->local only)."""
    t = datetime(d.year, d.month, d.day, tzinfo=timezone.utc) - timedelta(hours=15)
    while t.astimezone(z).date() < d:
        t += timedelta(minutes=15)  # every zone offset is a multiple of 15 min
    return t


def expected(slots, tz, week):
    z = ZoneInfo(tz)
    w = date.fromisoformat(week)
    cols = []
    for i in range(7):
        d = w + timedelta(days=i)
        labels = [s.astimezone(z).strftime("%H:%M") for s in slots if s.astimezone(z).date() == d]
        cols.append({"date": d.isoformat(), "labels": labels})
    rng = {"start": start_of_local_day(w, z), "end": start_of_local_day(w + timedelta(days=7), z)}
    return {"range": {k: v.strftime("%Y-%m-%dT%H:%M:%S.000Z") for k, v in rng.items()}, "cols": cols}


def test_ts_grouping_matches_zoneinfo(node_dist, ets):
    cases, wants = [], []
    for slug, et in ets.items():
        for week in WEEKS:
            d0 = date.fromisoformat(week)
            a = datetime(d0.year, d0.month, d0.day, tzinfo=timezone.utc) - timedelta(days=1)
            slots = compute_slots(et, range_start=a, range_end=a + timedelta(days=9),
                                  now=a - timedelta(days=1), busy=[])
            payload = [{"start": iso_z(s), "end": iso_z(s)} for s in slots]
            for tz in VISITOR_TZS:
                cases.append({"tz": tz, "week": week, "slots": payload})
                wants.append((slug, tz, week, expected(sorted(slots), tz, week)))
    proc = subprocess.run(["node", str(WEB / "test" / "group_stdin.mjs")], input=json.dumps(cases),
                          capture_output=True, text=True, check=True)
    got = json.loads(proc.stdout)
    assert len(got) == len(wants) > 100
    for g, (slug, tz, week, want) in zip(got, wants):
        assert g == want, (slug, tz, week)
