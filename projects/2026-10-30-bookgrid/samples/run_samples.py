"""Replay samples/requests/*.json against a fresh seeded database.

Each request is sent on top of the seed data plus the requests before it, so
the outputs are deterministic. ``--write`` regenerates samples/expected/.
Without it, the script diffs against the stored expectations and exits 1 on
any mismatch.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from fastapi.testclient import TestClient  # noqa: E402

from bookgrid.api import create_app  # noqa: E402
from bookgrid.seed import load_seed  # noqa: E402


def run() -> dict[str, dict]:
    with tempfile.TemporaryDirectory() as tmp:
        app = create_app(str(Path(tmp) / "samples.db"))
        store = app.state.store
        load_seed(store, HERE / "seed.json")
        store.create_resource("Night desk", "America/New_York", "00:00", "23:59",
                              ["MO", "TU", "WE", "TH", "FR", "SA", "SU"])
        ids = {r.name: r.id for r in store.list_resources()}
        client = TestClient(app)
        out = {}
        for path in sorted((HERE / "requests").glob("*.json")):
            body = json.loads(path.read_text())
            body["resource_id"] = ids[body.pop("resource")]
            resp = client.post("/api/bookings", json=body)
            out[path.stem] = {"status": resp.status_code, "body": resp.json()}
        free = client.get(f"/api/resources/{ids['Thames']}/free",
                          params={"date": "2026-11-04", "duration": 60, "step": 30})
        out["05_free_slots_thames"] = {"status": free.status_code, "body": free.json()}
    return out


def main() -> int:
    results = run()
    expected_dir = HERE / "expected"
    if "--write" in sys.argv:
        expected_dir.mkdir(exist_ok=True)
        for name, result in results.items():
            (expected_dir / f"{name}.json").write_text(json.dumps(result, indent=2) + "\n")
        print(f"wrote {len(results)} files to {expected_dir}")
        return 0
    bad = [n for n, r in results.items()
           if json.loads((expected_dir / f"{n}.json").read_text()) != r]
    for name in results:
        print(f"{'MISMATCH' if name in bad else 'ok':8} {name}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
