# slotwise — DST-correct scheduling with race-safe booking

A small full-stack booking page (think "pick a time to meet me"): a host defines
weekly availability in **their** timezone, a visitor browses open slots in
**theirs**, and books one. The project focuses on the two places where scheduling
apps usually break:

1. **Time.** A wall-clock rule like "Sundays 00:00–04:00 New York" contains
   3 real hours on spring-forward day and 5 on fall-back day, and 01:30 happens
   twice. Visitors in other zones see days split differently, and Europe and
   the US change clocks on different weekends.
2. **Concurrency.** Two people clicking the same slot at once, a double-submit,
   or a retry after a dropped response must not create two bookings. Buffers
   and daily caps must hold under the same race.

## Architecture

```
 browser (TypeScript, no framework)            FastAPI (Python)                 SQLite
 ┌──────────────────────────────┐   UTC ISO   ┌───────────────────────────┐   ┌──────────────┐
 │ time.ts  visitor-local weeks │ ──────────▶ │ GET  /slots?start&end      │──▶│ event_types  │
 │          day grouping (Intl) │             │ POST /bookings (Idem-Key)  │   │ bookings     │
 │ main.ts  grid, form, cancel  │ ◀────────── │ POST /cancel /reschedule   │   │ busy_blocks  │
 └──────────────────────────────┘             │ GET  /bookings/{id}/ics    │   │ idempotency  │
                                              └─────────────┬─────────────┘   └──────────────┘
                                                            │
                                         availability.py: pure compute_slots()
                                         (used by both the read path and the
                                          write path inside BEGIN IMMEDIATE)
```

| File | Role |
|---|---|
| `slotwise/availability.py` | Pure slot engine: weekly rules + date overrides → resolved UTC windows (DST rules below) → slots, filtered by buffers, busy intervals, min notice, horizon, daily cap. |
| `slotwise/store.py` | SQLite schema, booking/cancel/reschedule transactions, idempotency keys, hashed manage-tokens. |
| `slotwise/app.py` | FastAPI routes, input validation, error mapping (404/409/403/422), static UI. |
| `slotwise/ics.py` | RFC 5545 VEVENT export (UTC, CRLF, escaping, 75-octet folding). |
| `web/src/time.ts` | Visitor-side timezone math with `Intl` only: start of a local day (23h/25h days and zones where midnight is skipped), ISO weeks, grouping slots into local days. |
| `web/src/main.ts`, `api.ts` | The booking page. |

**The API speaks only UTC instants.** The server never needs to know the
visitor's timezone: the browser turns "the visitor's week" into a
`[start, end)` UTC range, the server returns slots in it, and the browser
groups them into local days. Offsets in input (`2026-11-02T10:00:00-05:00`) are
normalised; naive timestamps are rejected with 422 rather than guessed.

## DST rules (host side)

| Situation | Rule | Example (America/New_York) |
|---|---|---|
| Wall time doesn't exist (spring-forward gap) | resolve to the transition instant | window `02:30–04:00` on 2026-03-08 → 03:00–04:00 EDT, 2 slots |
| Wall time is ambiguous (fall-back overlap) | window **start** takes the earlier instant, **end** the later | `01:30` start = 05:30Z, `01:30` end = 06:30Z |
| Slot stepping | in real minutes from the resolved window start | `00:00–04:00` on 2026-11-01 → 5 hourly slots (01:00 EDT *and* 01:00 EST) |
| `24:00` | next day's midnight | |

## Booking correctness

* `book()` runs under `BEGIN IMMEDIATE` (SQLite's write lock is taken *before*
  reading), then re-runs `compute_slots` against bookings visible in that
  transaction and inserts only if the requested start is in the result. The
  read API and the writer share one definition of "available", so off-grid
  starts, lunch breaks, notice, horizon, buffers and the daily cap are all
  enforced at write time too.
* Each booking stores its **blocked interval** (start − buffer_before,
  end + buffer_after). A new slot is free iff its own blocked interval overlaps
  no stored blocked interval or external busy block, so back-to-back meetings
  get both the previous meeting's after-buffer and the next one's
  before-buffer.
* `Idempotency-Key`: same key + same body replays the original response
  (`200`, `replayed: true`, same id and token); same key + different body is
  `409 conflict`. The UI generates a key when a slot is picked, so a
  double-click or retry can't double-book.
* Reschedule is one transaction: the new slot is checked *as if the old booking
  were already free* (so you can shift into your own buffer), then the old row
  is cancelled with `rescheduled_to`, and the new row is inserted. A failed
  reschedule leaves the original confirmed.
* Cancel/reschedule require the manage-token returned at booking; only its
  SHA-256 is stored on the booking row.

## Setup

Python 3.10+ and Node 18+.

```bash
cd projects/2026-10-30-slotwise
python -m pip install -r requirements.txt
(cd web && npm install && npm run build)    # compiles TS to web/dist/
python -m slotwise --db slotwise.db          # http://127.0.0.1:8000
```

The server seeds the event types from `samples/event_types.json` on start
(`--seed` to change, upserts by slug).

## Usage

UI: open `http://127.0.0.1:8000/`, choose a meeting and your timezone, click a
slot, book, download the `.ics`, cancel. Query params `?type=&tz=&week=YYYY-MM-DD`
deep-link a view.

API:

```bash
curl 'localhost:8000/api/event-types/intro-call/slots?start=2026-11-02T05:00:00Z&end=2026-11-03T05:00:00Z'
curl -X POST localhost:8000/api/bookings -H 'Content-Type: application/json' \
     -H 'Idempotency-Key: 7f1c' \
     -d '{"event_type":"intro-call","start":"2026-11-02T15:00:00Z","name":"Ada","email":"ada@example.com"}'
# -> 201 {"booking": {...}, "token": "...", "replayed": false}; same request again -> 200 replayed
curl -X POST localhost:8000/api/bookings/<id>/reschedule -H 'Content-Type: application/json' \
     -d '{"token":"<token>","start":"2026-11-03T18:00:00Z"}'
curl -X POST localhost:8000/api/bookings/<id>/cancel -H 'Content-Type: application/json' -d '{"token":"<token>"}'
curl localhost:8000/api/bookings/<id>/ics
curl -X PUT localhost:8000/api/event-types/quick -H 'Content-Type: application/json' \
     -d '{"title":"Quick","host_tz":"Asia/Kolkata","duration_min":15,"weekly":{"mon":[["09:00","10:00"]]}}'
```

Event type fields: `host_tz`, `duration_min`, `step_min` (default = duration),
`buffer_before_min`, `buffer_after_min`, `min_notice_min`, `max_days_ahead`
(host-local days), `daily_cap` (bookings per host-local date), `weekly`
(`mon`..`sun` → list of `[HH:MM, HH:MM]`), `overrides` (`YYYY-MM-DD` → windows
replacing that day; `[]` = day off).

## Samples

* `samples/event_types.json` — three event types: a New York intro call with
  lunch break, buffer, notice, cap, a holiday and a Saturday override; a
  New York night window that crosses the DST change; London office hours.
* `samples/slots_request.json` → `samples/expected_slots_intro_call_2026-11-02.json`
* `samples/expected_night_support_2026-11-01.json` — the 5-slot fall-back night.
* `samples/demo_output.txt` — output of `python scripts/demo.py` (fixed clock
  2026-10-30T12:00Z): DST night, Tokyo view, a 12-client race, idempotent
  retry, reschedule/cancel. `python scripts/demo.py --write` regenerates all
  expected files.

## Tests

```bash
python -m pytest -q                 # 49 tests (Python)
(cd web && npm test)                # 7 node:test tests of time.ts
(cd web && npm run e2e)             # Chromium end-to-end via Playwright (optional)
```

* `test_availability.py` — weekly windows, both DST directions, gap/overlap
  resolution, London vs New York, notice/horizon, buffers, cap, overrides,
  `24:00`, config validation, plus a **brute-force oracle** that rebuilds slots
  minute by minute using only UTC→local conversion and must match the engine
  over six DST-adjacent windows in New York, London and Lord Howe (30-minute DST).
* `test_store.py` — 12 threads with separate connections race for one slot
  (exactly one wins), adjacent-slot race under buffers, write-time cap,
  idempotency replay/misuse, cancel, atomic reschedule, busy blocks, hashed tokens.
* `test_api.py` — HTTP contract: payloads, 201/200/409/403/404/422, clock-driven notice.
* `test_parity.py` — runs the compiled TypeScript `groupWeek`/`weekRangeIso`
  for every sample event type × 5 weeks (including both DST weekends and
  Chile's skipped midnight) × 9 visitor zones and compares to `zoneinfo`
  (skips if Node or the build is missing).
* `test_demo.py` — demo output and sample JSON files match the live app.
* `web/e2e/e2e.mjs` — books from a Europe/Berlin view in a Tokyo-timezone
  browser, checks the slot and its buffer neighbour disappear, fetches the
  .ics, cancels, and checks the slot returns.

## Design decisions

* **One pure function for availability.** Reads and writes call the same
  `compute_slots`; there is no separate "is this slot free?" check that could drift.
* **Serialise writes rather than add exclusion constraints.** SQLite has no
  range-exclusion constraint, and buffers/caps aren't expressible as a unique
  index anyway. `BEGIN IMMEDIATE` makes check-then-insert atomic; on Postgres
  the equivalent is a per-event-type advisory lock or an `EXCLUDE USING gist`
  constraint on the blocked range plus a cap check.
* **Store blocked intervals, not just meeting times,** so changing an event
  type's buffers later doesn't retroactively break existing bookings.
* **No timezone DB in the browser.** `Intl.DateTimeFormat` already ships IANA
  data; `startOfLocalDay` uses two offset fixed-point passes and falls back to
  a minute bisection when midnight doesn't exist.

## Limitations

* Single host per event type; no round-robin, no group events, no recurring bookings.
* No auth for the host side (`PUT /api/event-types/*` is open) — fine locally,
  not for deployment. No email delivery; the manage-token is returned in the
  response and shown in the page only.
* External calendars aren't synced; busy time is entered via `Store.add_busy_block`.
* SQLite's single writer caps booking throughput (fine for one host's page).
  Idempotency rows keep the raw token and never expire.
* Availability windows can't cross midnight (split them at `24:00`).
* The browser's `Intl` data and Python's `zoneinfo`/`tzdata` can disagree if
  one is outdated for a zone whose rules recently changed; the parity test
  only covers the installed versions.
* With Starlette ≥ 1.x, `fastapi.testclient` prints a deprecation warning about
  `httpx`; it is harmless.
