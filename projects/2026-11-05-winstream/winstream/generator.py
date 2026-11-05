"""Synthetic IoT-style stream with realistic disorder.

Each device (key) emits readings in bursts of activity (so session windows
have something to find). Every reading gets a network delay:

* most arrive within a few seconds (exponential, mean ``base_delay``),
* ``straggler_rate`` of them are held 30-180 s (retries, slow links),
* devices occasionally go offline and flush a buffered backlog minutes later,
* ``dup_rate`` of readings are re-delivered (same id) a little later.

Output is sorted by arrival time, which is the order a consumer sees.
"""
from __future__ import annotations

import random

from .model import Event


def generate(n_keys=8, duration=3 * 3600, start=1_767_225_600, seed=7, base_delay=3.0,
             straggler_rate=0.04, outage_rate=0.03, dup_rate=0.02, mean_interval=20.0):
    rng = random.Random(seed)
    events = []
    counter = 0
    for k in range(n_keys):
        key = f"dev-{k:02d}"
        t = start + rng.randint(0, 120)
        end = start + duration
        while t < end:
            # an active burst followed by an idle gap
            burst_end = t + rng.randint(300, 1500)
            outage_until = None
            while t < min(burst_end, end):
                counter += 1
                eid = f"e{counter:06d}"
                value = int(rng.gauss(500, 120)) + 30 * k
                if outage_until is None and rng.random() < outage_rate / 10:
                    outage_until = t + rng.randint(120, 600)
                if outage_until is not None and t < outage_until:
                    delay = (outage_until - t) + rng.expovariate(1 / base_delay)
                else:
                    outage_until = None
                    delay = rng.expovariate(1 / base_delay)
                    if rng.random() < straggler_rate:
                        delay += rng.uniform(30, 180)
                arrival = t + int(delay)
                events.append(Event(eid, key, t, value, arrival))
                if rng.random() < dup_rate:
                    events.append(Event(eid, key, t, value, arrival + rng.randint(1, 90)))
                t += max(1, int(rng.expovariate(1 / mean_interval)))
            t = burst_end + rng.randint(200, 1800)
    events.sort(key=lambda e: (e.arrival, e.key, e.id))
    return events
