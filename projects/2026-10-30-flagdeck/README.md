# flagdeck: a self-hosted feature-flag service

A small full-stack feature-flag system: a **FastAPI + SQLite** control plane, a **TypeScript**
admin UI and a **TypeScript client SDK** that evaluates flags locally. The Python and TS
evaluators are tested against each other so they return the same answer for every user.

## Problem

Teams want to ship code dark and turn it on gradually: 5% of users, then 20%, then everyone,
with a kill switch, internal-staff overrides and an audit trail. Doing this with ad-hoc config
files goes wrong in a few common ways:

* **Sticky rollouts break.** If a user is in the 5% today, they must still be in the 20%
  tomorrow. Random or re-seeded bucketing flips users back and forth.
* **Server and client disagree.** If the backend evaluates in Python and the frontend in JS,
  small differences (`true == 1`, `42.0` vs `42`, UTF-8 hashing, missing attributes) send the
  same user down two different code paths.
* **Lost updates.** Two people edit the same flag in two tabs and the last save silently wins.
* **Unknown blast radius.** Nobody can say how many users a rule change will affect until
  after it ships.

flagdeck handles each of these directly.

## Features

| Area | What it does |
|---|---|
| Targeting | Per-environment (`dev`, `prod`) on/off, individual targets, ordered rules of AND-ed clauses (`in`, `startsWith`, `endsWith`, `contains`, `matches`, `lt/lte/gt/gte`, `negate`), fallthrough |
| Rollouts | Weighted percentage rollouts over any number of variations, bucketed by any attribute (`key`, `org`, `device_id`…), MurmurHash3 with per-flag salt |
| Prerequisites | A flag can require another flag to serve a given variation; cycles are rejected at write time and guarded at runtime |
| Concurrency | Every write needs `If-Match: "<version>"`; compare-and-swap inside `BEGIN IMMEDIATE` → `412` on a stale write, `428` if the header is missing |
| Audit | Append-only log of every create/update/toggle/delete with actor, comment and a path-level JSON diff |
| Impact preview | Evaluate a *draft* against the live config over a deterministic synthetic population (or contexts you send) and report who flips |
| SDK payload | `GET /api/sdk/{env}` returns one environment's ruleset with a content-hash `ETag`; polls cost a `304` |
| UI | Flag list with kill switches, JSON editor with conflict banner (reload or overwrite), impact preview, audit log, playground comparing server vs local SDK |

## Architecture

```
            ┌───────────────────────── browser ─────────────────────────┐
            │ web/src/main.ts (admin UI)                                │
            │   └─ client.ts (FlagClient SDK) ─ evaluator.ts ◄──┐       │
            └───────┬─────────────────────────────────┬─────────┼───────┘
     CRUD + If-Match│                   GET /api/sdk/env (ETag) │ same rules,
                    ▼                                 ▼         │ parity-tested
┌────────────────────────── flagdeck (FastAPI) ─────────────────┼───────┐
│ app.py      routes, ETag/If-Match, error mapping              │       │
│ schema.py   pydantic shape + semantic validation, cycle check │       │
│ evaluator.py murmur3 bucketing, clauses, prerequisites ◄──────┘       │
│ store.py    SQLite: flags(key, version, doc) + audit, CAS writes      │
│ diff.py     path-level JSON diff for the audit log                    │
│ population.py deterministic synthetic users for impact preview        │
└───────────────────────────────────────────────────────────────────────┘
```

**Evaluation order** (identical in both languages):
1. no/empty `key` in context → `ERROR` · 2. env off → `off_variation` (`OFF`) ·
3. any prerequisite off, missing, or on a different variation → `off_variation` (`PREREQUISITE_FAILED`) ·
4. context key in a target list → `TARGET_MATCH` · 5. first rule whose clauses all match → `RULE_MATCH` ·
6. `fallthrough` → `FALLTHROUGH`.

**Bucketing:** `murmur3_32(utf8("{flag_key}.{salt}.{value}")) % 100000`; a variation is chosen
by walking cumulative weights (in thousandths of a percent). Raising one variation's share only
moves the boundary, so users who already had it keep it (tested for 5→20→50→100%).

## Setup

Requires Python 3.10+ and Node 18+ (tested with Python 3.11, Node 22, FastAPI 0.162,
pydantic 2.14, TypeScript 5.6).

```bash
cd projects/2026-10-30-flagdeck
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
(cd web && npm install && npm run build)      # compiles web/src → web/dist
```

## Usage

```bash
python -m flagdeck --db flagdeck.db --seed samples/flags.json   # http://127.0.0.1:8000
```

Open the UI at `/`, or use the API (OpenAPI docs at `/docs`):

```bash
# evaluate every flag for a user
curl -s localhost:8000/api/evaluate -H 'Content-Type: application/json' \
  -d '{"env":"prod","context":{"key":"dan","email":"dan@acme.com","country":"FR"}}'

# kill switch: needs the version you last saw
curl -s localhost:8000/api/flags/new-checkout/toggle -H 'If-Match: "1"' -H 'X-Actor: ana' \
  -H 'Content-Type: application/json' -d '{"env":"prod","on":false,"comment":"incident 42"}'

# blast radius of a draft over 5000 synthetic users
curl -s localhost:8000/api/flags/new-checkout/preview -H 'Content-Type: application/json' \
  -d "{\"env\":\"prod\",\"sample_size\":5000,\"draft\":$(curl -s localhost:8000/api/flags/new-checkout | jq .flag)}"
```

| Method & path | Notes |
|---|---|
| `GET /api/flags`, `GET /api/flags/{key}` | `ETag: "<version>"` |
| `POST /api/flags` | `X-Actor`, `X-Comment` headers recorded in the audit log |
| `PUT /api/flags/{key}` | full document; `If-Match` required; no-op saves don't bump the version |
| `POST /api/flags/{key}/toggle` | `{"env","on","comment"}`; `If-Match` required |
| `DELETE /api/flags/{key}` | `If-Match`; `409` with `dependents` while it is someone's prerequisite |
| `GET /api/flags/{key}/audit` | newest first, with diffs |
| `POST /api/evaluate` | `{"env","context"}` → value, variation and reason per flag |
| `POST /api/flags/{key}/preview` | `{"env","draft","contexts"?,"sample_size"?}` → counts before/after + examples |
| `GET /api/sdk/{env}` | ruleset for local evaluation; honours `If-None-Match` |

Using the SDK from TypeScript:

```ts
import { FlagClient } from "./client.js";
const flags = new FlagClient("http://localhost:8000", "prod");
await flags.refresh();                       // call periodically; 304s are cheap
if (flags.variation("new-checkout", { key: user.id, email: user.email }, false)) { /* ... */ }
```

A flag document looks like this (see `samples/flags.json` for four realistic ones):

```json
{"key": "new-checkout", "variations": [false, true],
 "environments": {"prod": {"on": true, "off_variation": 0,
   "targets": [{"variation": 1, "values": ["qa-alice"]}],
   "rules": [{"id": "staff", "clauses": [{"attribute": "email", "op": "endsWith", "values": ["@acme.com"]}],
              "serve": {"variation": 1}}],
   "fallthrough": {"rollout": {"bucket_by": "key", "weights": [80000, 20000]}}}}}
```

## Samples

| File | What it is |
|---|---|
| `samples/flags.json` | 4 flags: boolean rollout with targets/rules, 3-way string experiment bucketed by `org`, prerequisite-gated flag, JSON-object variations with a regex rule |
| `samples/contexts.json` | 13 users incl. missing attributes, wrong types (`"4.0"`, `true`), `7.0` vs `7`, non-ASCII keys |
| `samples/parity_expected.json` | expected value/variation/reason for every flag × context × env; both test suites assert it (regenerate with `python scripts/gen_parity.py`) |
| `samples/demo_output.txt` | output of `python scripts/demo.py` (evaluate, preview, 412 conflict, cycle/delete rejection, audit, 304); a test diffs it |

## Tests

```bash
python -m pytest -q                     # 72 tests (fuzz parity test needs web/dist built)
(cd web && npm test)                    # 5 node:test tests incl. parity file + SDK ETag handling
(cd web && PYTHON=$(which python) npm run e2e)   # Playwright: real server + headless Chromium
```

Results from this build:

* `pytest`: **72 passed**. Covers MurmurHash3 reference vectors plus 2,000 random inputs checked
  against `mmh3`, evaluation order, every operator including type-confusion cases, rollout
  distribution (within ±1.5 pp at n=20,000), sticky/monotonic ramps, salt reshuffling, validation
  errors, cycle detection, the HTTP contract (428/412/409/422/304), **8 concurrent writers on
  one version → exactly one 200 and seven 412s**, two app instances sharing one DB, preview
  invariants, and the demo output.
* **Cross-language fuzz parity:** 3,000 synthetic contexts with type-confusing attributes ×
  4 flags × 2 envs, Python vs compiled TS: 0 mismatches. To check that the test can actually
  fail, I broke three things in `web/dist/evaluator.js` one at a time. Two were caught:
  treating `null` as present, and hashing Latin-1 instead of UTF-8. The third, removing the
  "last non-zero weight" fallback, was not caught. That code can't be reached when the weights
  sum to 100,000, so the change makes no difference.
* `npm test`: **5 passed**. `npm run e2e`: **all 9 steps passed**. These cover the kill switch,
  impact preview (829 of 5,000 users flip when `new-checkout` goes from 20% to 50%), saving,
  the conflict banner after another tab writes, overwriting, the audit log, the playground
  agreeing with the server on all flags, and validation errors shown on create.

## Design decisions

* **Hand-written MurmurHash3 in both languages** instead of SHA-256. Browsers only offer async
  SHA via `crypto.subtle`, which would make `variation()` async. Murmur is about 40 lines,
  checked against reference vectors, and well distributed for bucketing.
* **Type-strict equality.** `true` never equals `1`, while `1` equals `1.0`, because JSON can't
  tell `1` and `1.0` apart. Only strings and safe integers can be bucketed, and `7.0` buckets
  like `7`, so the result is the same as in JavaScript.
* **A missing attribute never matches, even in a negated clause.** "country not in [US]"
  should not quietly include users whose country is unknown. If you want to include them,
  write a separate rule for it.
* **A context without the bucketing attribute goes to bucket 0**, which means the first
  variation with a non-zero weight. That is stable and easy to explain, unlike picking at
  random.
* **Optimistic concurrency, not locks.** Editors can keep a flag open for as long as they
  like. Conflicts only come up when someone saves, and the UI keeps your draft so you can
  reload or overwrite on purpose. `BEGIN IMMEDIATE` plus `UPDATE … WHERE version = ?` makes
  this safe across processes as well as threads.
* **Validation in two layers.** Pydantic (`extra="forbid"`) checks the shape. Explicit checks
  return readable paths such as `environments.prod.fallthrough.rollout.weights sum to 90000`
  for rules a schema can't express.
* **Per-environment SDK payloads** keep `dev` rules away from production clients, and a `dev`
  change doesn't make `prod` SDKs download the payload again (tested).
* **Vanilla TypeScript UI, compiled by `tsc`.** No framework or bundler, so the only Node
  dependency is TypeScript.

## Limitations

* `dev` and `prod` are the only environments, and they are fixed in `schema.py`. There is no
  authentication: `X-Actor` is trusted as given, so put an auth proxy in front before using
  this beyond a single machine.
* `matches` uses Python `re` on the server and JS `RegExp` with the `u` flag in the client.
  Stick to the common subset (classes, anchors, quantifiers, alternation). Lookbehind,
  possessive quantifiers and some escapes behave differently. The server validates regexes
  with Python only.
* There are no reusable segments, scheduled changes, approvals or experiment analytics. The
  SDK polls; it doesn't stream.
* Impact preview runs on a synthetic population, so its counts show the shape of the change,
  not your real traffic. Send real `contexts` for exact numbers.
* SQLite allows only one writer at a time, which is fine for an admin plane but not for
  thousands of writes per second. Evaluation is meant to happen in SDKs from the cached payload.
* Bucket assignment changes if you change a flag's `salt` or `key`. This is intended, but it
  means renaming a flag reshuffles its users.
