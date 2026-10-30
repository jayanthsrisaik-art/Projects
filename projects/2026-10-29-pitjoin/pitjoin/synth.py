"""Deterministic synthetic feature store: card transactions with three views.

* ``customer_daily`` - daily batch aggregates.  The window closing at midnight
  lands at ~06:00; some days land 1-3 days late (job failures); ~5% of rows
  get a backfilled correction days later.  TTL 3 days.
* ``merchant_risk`` - irregularly refreshed scores, landing within 2 hours.
  TTL 7 days.
* ``customer_profile`` - slowly changing attributes, no created timestamp,
  no TTL.
* ``transactions.csv`` - the entity spine: timestamped labels, a few of them
  for customers with no features at all.
"""

from __future__ import annotations

import csv
import json
import random
from pathlib import Path

from .repo import MICROS, format_ts, parse_ts

HOUR = 3600 * MICROS
DAY = 24 * HOUR

REPO_SPEC = {
    "feature_views": [
        {
            "name": "customer_daily", "entity": "customer_id", "source": "customer_daily.csv",
            "timestamp_field": "event_timestamp", "created_timestamp_field": "created_timestamp",
            "ttl_seconds": 3 * 86400,
            "features": [{"name": "txn_count_7d", "dtype": "int"}, {"name": "spend_7d", "dtype": "float"}],
        },
        {
            "name": "merchant_risk", "entity": "merchant_id", "source": "merchant_risk.csv",
            "timestamp_field": "event_timestamp", "created_timestamp_field": "created_timestamp",
            "ttl_seconds": 7 * 86400,
            "features": [{"name": "risk_score", "dtype": "float"}, {"name": "risk_tier", "dtype": "str"}],
        },
        {
            "name": "customer_profile", "entity": "customer_id", "source": "customer_profile.csv",
            "timestamp_field": "event_timestamp",
            "features": [{"name": "segment", "dtype": "str"}, {"name": "is_verified", "dtype": "bool"}],
        },
    ]
}


def _write(path: Path, header: list[str], rows: list[list]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(header)
        w.writerows(rows)


def generate(out_dir: str | Path, seed: int = 7, customers: int = 300, merchants: int = 80,
             days: int = 60, transactions: int = 5000, start: str = "2026-01-01T00:00:00Z") -> dict:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    t0 = parse_ts(start)

    daily = []
    for c in range(customers):
        cid = f"C{c:04d}"
        base = rng.uniform(0.5, 6.0)
        for d in range(days):
            ev = t0 + (d + 1) * DAY  # window [d, d+1) closes at midnight
            created = ev + 6 * HOUR + rng.randrange(0, 90) * 60 * MICROS
            if rng.random() < 0.03:
                created += rng.randint(1, 3) * DAY  # late batch
            cnt = max(0, int(rng.gauss(base * 7, 3)))
            spend = round(cnt * rng.uniform(15, 80), 2)
            daily.append([cid, format_ts(ev), format_ts(created), cnt, spend])
            if rng.random() < 0.05:  # backfilled correction of the same window
                fix = created + rng.randint(2, 10) * DAY
                cnt2 = cnt + rng.randint(1, 6)
                daily.append([cid, format_ts(ev), format_ts(fix), cnt2, round(spend * 1.3 + 10, 2)])
    rng.shuffle(daily)  # sources are not assumed to be sorted
    _write(out / "customer_daily.csv",
           ["customer_id", "event_timestamp", "created_timestamp", "txn_count_7d", "spend_7d"], daily)

    risk = []
    for m in range(merchants):
        mid = f"M{m:03d}"
        t = t0 + rng.randrange(0, 3 * DAY)
        while t < t0 + days * DAY:
            score = round(rng.betavariate(2, 8), 4)
            tier = "high" if score > 0.4 else "medium" if score > 0.2 else "low"
            created = t + rng.randrange(0, 2 * HOUR)
            risk.append([mid, format_ts(t), format_ts(created), score, tier])
            t += rng.randrange(DAY, 12 * DAY)  # gaps longer than TTL happen
    _write(out / "merchant_risk.csv",
           ["merchant_id", "event_timestamp", "created_timestamp", "risk_score", "risk_tier"], risk)

    profile = []
    for c in range(customers):
        cid = f"C{c:04d}"
        t = t0 - rng.randrange(0, 30 * DAY)
        for _ in range(rng.randint(1, 3)):
            profile.append([cid, format_ts(t), rng.choice(["retail", "smb", "premium"]),
                            rng.choice(["true", "false"])])
            t += rng.randrange(5 * DAY, 40 * DAY)
    _write(out / "customer_profile.csv", ["customer_id", "event_timestamp", "segment", "is_verified"], profile)

    spine = []
    for i in range(transactions):
        cid = f"C{rng.randrange(customers + 10):04d}"  # ~3% unknown customers
        mid = f"M{rng.randrange(merchants):03d}"
        ts = t0 + rng.randrange(0, days * 86400) * MICROS
        spine.append([f"T{i:06d}", cid, mid, format_ts(ts), int(rng.random() < 0.08)])
    _write(out / "transactions.csv", ["txn_id", "customer_id", "merchant_id", "event_timestamp", "label"], spine)

    (out / "feature_repo.json").write_text(json.dumps(REPO_SPEC, indent=2) + "\n", encoding="utf-8")
    return {"customer_daily": len(daily), "merchant_risk": len(risk),
            "customer_profile": len(profile), "transactions": len(spine)}
