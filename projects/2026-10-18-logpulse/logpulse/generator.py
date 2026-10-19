"""Synthetic log stream with labelled incidents (deterministic for a given seed)."""
import math
import random
from dataclasses import dataclass

from .parsing import format_line

START = 1767225600  # 2026-01-01T00:00:00Z
SERVICES = ("api", "auth", "billing")


@dataclass(frozen=True)
class Incident:
    kind: str  # error_burst | latency_spike | volume_drop
    start_min: int
    length: int


def plan_incidents(minutes: int, seed: int, n: int = 14) -> list[Incident]:
    """Non-overlapping incidents spread across the timeline (one per equal slot)."""
    rng = random.Random(seed)
    kinds = ("error_burst", "latency_spike", "volume_drop")
    slot = (minutes - 60) // n
    out = []
    for i in range(n):
        length = rng.randint(8, 18)
        start = 60 + i * slot + rng.randint(5, max(6, slot - length - 5))
        out.append(Incident(kinds[i % 3], start, length))
    return out


def _normal(rng, svc):
    r = rng.random()
    if svc == "auth" and r < 0.35:
        return "INFO", f"user {rng.randint(1, 5000)} login ok from 10.0.{rng.randint(0, 9)}.{rng.randint(1, 250)}"
    if svc == "billing" and r < 0.35:
        return "INFO", f"invoice {rng.randint(1000, 99999)} charged amount {rng.randint(5, 900)} usd"
    if r < 0.03:
        return "WARN", f"resource /items/{rng.randint(1, 9999)} not found status=404"
    if r < 0.045:
        return "ERROR", f"upstream timeout after {rng.randint(2, 4)}000ms retry={rng.randint(1, 3)}"
    path = rng.choice(("users", "orders", "items"))
    lat = max(5, int(rng.gauss(45, 12)))
    return "INFO", f"request GET /{path}/{rng.randint(1, 9999)} completed in {lat}ms status=200"


def generate(minutes: int = 4320, seed: int = 7, benign_new_after: float = 0.7):
    """Return (lines, incidents). Rate follows a daily cycle; after `benign_new_after`
    of the timeline a harmless, never-before-seen template appears (hard negative)."""
    rng = random.Random(seed)
    incidents = plan_incidents(minutes, seed)
    active = {m: inc for inc in incidents for m in range(inc.start_min, inc.start_min + inc.length)}
    lines = []
    for minute in range(minutes):
        inc = active.get(minute)
        base = 9 + 5 * math.sin(2 * math.pi * (minute % 1440) / 1440)
        rate = base * (0.1 if inc and inc.kind == "volume_drop" else 1.0)
        n = max(0, int(rng.gauss(rate, 1.5)))
        for _ in range(n):
            ts = START + minute * 60 + rng.randint(0, 59)
            svc = rng.choice(SERVICES)
            level, msg = _normal(rng, svc)
            if inc and inc.kind == "latency_spike" and "completed in" in msg:
                msg = f"request GET /{rng.choice(('users', 'orders'))}/{rng.randint(1, 9999)} completed in {rng.randint(300, 900)}ms status=200"
            lines.append(format_line(ts, svc, level, msg))
        if inc and inc.kind == "error_burst":
            for _ in range(rng.randint(6, 12)):
                ts = START + minute * 60 + rng.randint(0, 59)
                lines.append(format_line(ts, rng.choice(SERVICES), "ERROR",
                                         f"db connection pool exhausted active={rng.randint(90, 100)} waiting={rng.randint(10, 80)}"))
        if minute >= int(minutes * benign_new_after) and rng.random() < 0.2:
            lines.append(format_line(START + minute * 60 + 30, "api", "INFO",
                                     f"cache warmup finished entries={rng.randint(100, 900)}"))
    lines.sort()  # ISO timestamps sort chronologically
    return lines, incidents


def window_labels(incidents: list[Incident]) -> set[int]:
    """Set of absolute minute-window ids (epoch // 60) covered by an incident."""
    return {START // 60 + m for inc in incidents for m in range(inc.start_min, inc.start_min + inc.length)}
