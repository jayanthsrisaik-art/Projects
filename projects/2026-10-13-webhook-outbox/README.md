# Webhook Outbox

A small FastAPI service that delivers webhooks reliably: events are persisted first (transactional outbox in SQLite), fanned out to subscribed endpoints, signed with HMAC-SHA256, and retried with exponential backoff until delivered or dead-lettered.

## Problem
Sending webhooks inline with a request loses events when a receiver is down. This project shows the standard fix: write the delivery intent durably, deliver asynchronously, retry sensibly, and make duplicates harmless.

## Architecture
- `app/store.py` – SQLite schema (`endpoints`, `deliveries`), fan-out, idempotent enqueue (`UNIQUE(endpoint_id, idempotency_key)`).
- `app/worker.py` – `run_once(conn, now, transport, policy)`: picks due deliveries, signs, sends, records outcome. The transport and clock are injected, so retry logic is tested without sleeping or networking.
- `app/signing.py` – `t=<ts>,v1=<hmac>` signatures over `"<ts>.<body>"`, plus receiver-side `verify` with a replay window.
- `app/main.py` – API: `POST /endpoints`, `POST /events`, `GET /deliveries[?status=]`, `GET /deliveries/{id}`, `POST /worker/run-once`, `POST /deliveries/{id}/retry`.

Retry policy: success on 2xx; 4xx (except 408/429) is permanent → `dead` immediately; 5xx, 408, 429 and network errors retry after `min(base*2^(n-1), max_delay)` seconds (defaults 30s base, 1h cap, 6 attempts) then go `dead`. Dead deliveries can be requeued manually.

## Setup
```
pip install -r requirements.txt
python -m pytest -q
python demo.py          # real HTTP against a local flaky receiver; compare samples/expected_demo_output.txt
uvicorn app.main:app    # OUTBOX_DB=outbox.db to persist (default is in-memory)
```
`samples/requests.http` shows an example request sequence. The worker runs when `POST /worker/run-once` is called (e.g. from cron).

## Measured results
Last run: 11 tests passed (signing, fan-out, idempotency, backoff schedule/cap, dead-lettering, 4xx vs 429, network errors, API flow); `demo.py` output matched `samples/expected_demo_output.txt`.

## Design decisions
- Deterministic backoff without jitter keeps tests exact; add jitter for production thundering-herd protection.
- At-least-once delivery: receivers should dedupe on `idempotency_key` / `X-Delivery-Id`.
- Time is passed in, not read inside the worker.

## Limitations
- Single worker process only; no row-level claiming/locking, so concurrent workers could double-send.
- Delivery is sequential (no concurrency), no per-endpoint ordering guarantees or circuit breaker.
- No authentication on the API; secrets stored in plaintext in SQLite; no SSRF protection on endpoint URLs.
- Signature timestamp equals worker time, so a retry is signed afresh.
