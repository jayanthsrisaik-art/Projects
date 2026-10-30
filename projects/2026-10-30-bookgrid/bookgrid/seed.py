"""Load ``samples/seed.json`` (or any file of the same shape) into a store."""
from __future__ import annotations

import json
from pathlib import Path

from .store import BookingRequest, Store


def load_seed(store: Store, path: str | Path) -> dict[str, int]:
    data = json.loads(Path(path).read_text())
    ids = {r.name: r.id for r in store.list_resources()}
    for r in data["resources"]:
        if r["name"] not in ids:
            ids[r["name"]] = store.create_resource(
                r["name"], r["timezone"], r["open_time"], r["close_time"],
                r["open_days"], r["buffer_minutes"]).id
    booked = 0
    for b in data["bookings"]:
        req = BookingRequest.from_dict({**b, "resource_id": ids[b["resource"]], "on_conflict": "skip"})
        _, body, replayed = store.book(req, idempotency_key=f"seed:{b['resource']}:{b['title']}")
        booked += 0 if replayed else len(body["booked"])
    return {"resources": len(ids), "occurrences_booked": booked}
