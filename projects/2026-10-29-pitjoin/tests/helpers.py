import random
from pathlib import Path

from pitjoin.repo import MICROS, FeatureRow, FeatureView


def random_view(rng: random.Random, n_rows: int, keys: list[str], ttl_seconds: int | None) -> FeatureView:
    """Timestamps on a coarse 30-second grid so ties, TTL boundaries and created < ts all occur."""
    rows = []
    for i in range(n_rows):
        ts = rng.randrange(0, 30)
        created = ts + rng.randrange(-3, 8)
        rows.append(FeatureRow(i, rng.choice(keys), ts * MICROS, created * MICROS, (i,)))
    return FeatureView(name="v", entity="k", source=Path("unused.csv"), timestamp_field="ts",
                       features=[("x", "int")], created_timestamp_field="created",
                       ttl_seconds=ttl_seconds, rows=rows)


def random_spine(rng: random.Random, n: int, keys: list[str]) -> tuple[list, list[int]]:
    ks = [rng.choice(keys + [None, "unknown"]) for _ in range(n)]
    ts = [rng.randrange(-2, 40) * MICROS for _ in range(n)]
    return ks, ts
