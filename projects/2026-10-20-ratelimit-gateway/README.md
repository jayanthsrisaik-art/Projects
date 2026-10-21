# ratelimit-gateway

Rate-limiting ASGI middleware for FastAPI with four interchangeable algorithms, per-route policies,
and an injectable clock so behaviour is deterministic and testable.

## Problem
Fixed-window counters are cheap but let a client send 2x the limit across a window boundary; exact
sliding logs are accurate but memory-hungry; token buckets allow controlled bursts. This project
implements all four behind one interface and shows the differences with measured numbers.

## Architecture
- `ratelimit/algorithms.py` – `FixedWindow`, `SlidingWindowLog`, `SlidingWindowCounter`, `TokenBucket`.
  `check(key, now, cost) -> Decision(allowed, limit, remaining, retry_after, reset_after)`; thread-safe; idle-key eviction.
- `ratelimit/policy.py` – `PolicySet`: segment-aware longest-prefix route matching, exempt paths.
- `ratelimit/middleware.py` – pure ASGI middleware; keys callers by `X-API-Key`, else (optionally trusted) forwarded IP, else peer IP;
  adds `RateLimit-Limit/Remaining/Reset`, returns 429 with `Retry-After`. Rejected requests never reach the handler.
- `ratelimit/simulate.py` – deterministic algorithm comparison. `app.py` – demo app.

## Setup
```
python -m pip install -r requirements.txt
python -m pytest -q
python -m ratelimit.simulate      # compare with samples/expected_simulation.txt
uvicorn app:app                   # see samples/requests.http
```

## Measured results (limit 10 per 10s, synthetic workloads; `samples/expected_simulation.txt`)
| workload | fixed_window | sliding_log | sliding_counter | token_bucket |
|---|---|---|---|---|
| boundary burst (accepted) | 20 | 10 | 10 | 11 |
| steady 3 req/s overload, 30s (accepted) | 30 | 30 | 28 | 39 |

The fixed window accepted 20 requests inside a 10s span; the sliding log never exceeded 10.
The token bucket's long-run rate is limit/window, but it starts full, so it admits an initial burst.

## Design decisions
- Time is passed in, never read inside algorithms; the middleware takes a `clock` callable (`time.monotonic` by default).
- `retry_after` is tested to be honest: waiting that long with no other traffic makes the next request succeed.
- Rate-limit keys include the rule prefix so route budgets are independent.
- `X-Forwarded-For` is ignored unless explicitly trusted, because clients can spoof it.

## Limitations
- State is in-process memory: not shared across workers/hosts (a Redis-backed store would be the next step).
- Identity from `X-API-Key` is not validated; any value creates a bucket (eviction bounds memory only over time).
- Sliding counter is an approximation by design (it accepted 28 vs the exact 30 above).
- Test client output has a Starlette `httpx` deprecation warning; tests pass.
