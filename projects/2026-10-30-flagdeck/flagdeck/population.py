"""Deterministic synthetic contexts used to preview the blast radius of a flag change."""
from __future__ import annotations

import random

COUNTRIES = ["US", "US", "US", "GB", "DE", "IN", "BR", "CA", "FR", "JP"]
PLANS = ["free", "free", "free", "pro", "pro", "enterprise"]
DOMAINS = ["gmail.com", "outlook.com", "acme.com", "example.org", "yahoo.com"]
APP_VERSIONS = [3.9, 4.0, 4.1, 4.2, 5.0]


def synthetic_contexts(n: int, seed: int = 7) -> list[dict]:
    rng = random.Random(seed)
    out = []
    for i in range(n):
        key = f"user-{i:05d}"
        ctx = {
            "key": key,
            "email": f"{key}@{rng.choice(DOMAINS)}",
            "country": rng.choice(COUNTRIES),
            "plan": rng.choice(PLANS),
            "app_version": rng.choice(APP_VERSIONS),
            "beta": rng.random() < 0.1,
            "org": f"org-{rng.randrange(n // 20 + 1):04d}",
        }
        if rng.random() < 0.05:
            del ctx["country"]  # some contexts lack attributes, as in real traffic
        out.append(ctx)
    return out
