"""Seeded synthetic request logs with the properties that break naive rollups.

* Users return across many hours (Zipf-like activity), so summing hourly
  distinct counts heavily overcounts.
* Latency is log-normal per endpoint with a few incident hours where one
  endpoint's tail blows up, so averaging hourly p95s hides or smears the tail.
* A small share of events is held back and delivered later as a late batch.
"""
from __future__ import annotations

import bisect
import math
import os
import random
from datetime import datetime, timedelta, timezone

from .events import Event, event_to_json

ENDPOINTS = {  # name: (traffic weight, log-normal mu, sigma, base error rate)
    "/search": (0.35, math.log(80), 0.55, 0.004),
    "/product": (0.25, math.log(45), 0.45, 0.002),
    "/cart": (0.15, math.log(60), 0.50, 0.003),
    "/checkout": (0.10, math.log(220), 0.60, 0.010),
    "/login": (0.15, math.log(120), 0.40, 0.005),
}
# Relative traffic by hour of day (UTC), peaks in the afternoon/evening.
DIURNAL = [0.25, 0.18, 0.14, 0.12, 0.12, 0.16, 0.28, 0.45, 0.65, 0.80, 0.90, 0.95,
           1.00, 1.00, 0.98, 0.97, 0.98, 1.00, 0.95, 0.85, 0.70, 0.55, 0.42, 0.32]


def generate(days: int = 14, events_per_day: int = 15000, users: int = 20000, seed: int = 7,
             start: datetime = datetime(2026, 10, 1, tzinfo=timezone.utc),
             late_fraction: float = 0.02, incidents: int = 6):
    """Return (on_time_events_by_day, late_events, incident_list)."""
    rng = random.Random(seed)
    names = list(ENDPOINTS)
    ep_cum = _cum([ENDPOINTS[n][0] for n in names])
    # Zipf-like user activity: a few heavy users, a long tail of occasional ones.
    user_cum = _cum([1.0 / (i + 1) ** 0.8 for i in range(users)])
    user_ids = [f"u{i:06d}" for i in range(users)]
    rng.shuffle(user_ids)
    hour_cum = _cum(DIURNAL)

    incident_set = set()
    while len(incident_set) < incidents:
        incident_set.add((rng.choice(names), rng.randrange(days * 24)))

    by_day: list[list[Event]] = []
    late: list[Event] = []
    for d in range(days):
        day_events = []
        for _ in range(events_per_day):
            h = _pick(rng, hour_cum)
            ts = start + timedelta(days=d, hours=h, seconds=rng.randrange(3600),
                                   microseconds=rng.randrange(1000) * 1000)
            ep = names[_pick(rng, ep_cum)]
            _, mu, sigma, err = ENDPOINTS[ep]
            if (ep, d * 24 + h) in incident_set:
                mu, sigma, err = mu + math.log(6), sigma + 0.3, err * 15
            latency = round(rng.lognormvariate(mu, sigma), 3)
            status = 503 if rng.random() < err else (404 if rng.random() < 0.01 else 200)
            e = Event(ts, user_ids[_pick(rng, user_cum)], ep, latency, status)
            (late if rng.random() < late_fraction else day_events).append(e)
        day_events.sort(key=lambda e: e.ts)
        by_day.append(day_events)
    late.sort(key=lambda e: e.ts)
    return by_day, late, sorted(incident_set, key=lambda x: x[1])


def write_dataset(out_dir: str, **kwargs) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    by_day, late, incidents = generate(**kwargs)
    files = []
    for events in by_day:
        if not events:
            continue
        path = os.path.join(out_dir, f"events-{events[0].ts:%Y-%m-%d}.jsonl")
        _write(path, events)
        files.append(path)
    late_path = os.path.join(out_dir, "late.jsonl")
    _write(late_path, late)
    return {"daily_files": files, "late_file": late_path,
            "events": sum(map(len, by_day)) + len(late), "late_events": len(late), "incidents": incidents}


def _write(path, events):
    with open(path, "w", encoding="utf-8") as fh:
        for e in events:
            fh.write(event_to_json(e) + "\n")


def _cum(weights):
    out, total = [], 0.0
    for w in weights:
        total += w
        out.append(total)
    return out


def _pick(rng, cum):
    return bisect.bisect_right(cum, rng.random() * cum[-1])
