# tallyup: group expense splitting that stays correct to the cent

A small full-stack app for splitting shared costs (trips, flats, dinners): a **FastAPI + SQLite**
backend and a **TypeScript** single-page UI. A CRUD tutorial version of this app is easy to build.
tallyup focuses on the places where real expense splitters go wrong:

| Problem | What usually happens | What tallyup does |
|---|---|---|
| €100 split three ways | floats give 33.333…, or shares add up to 99.99 | integer minor units; largest-remainder allocation; shares always sum to the total |
| Who pays the odd cent? | the same person (first in the list) every time | **drift-aware rounding**: the leftover cent goes to whoever is furthest behind |
| `0.001` USD, `1.5` JPY, `1e3` | silently rounded or accepted | rejected; exponent per currency (USD 2, JPY 0, KWD 3) |
| A double-click or a retry after a timeout | duplicate expense | `Idempotency-Key`: replays the stored response; a reused key with a different body gets 422 |
| Two people edit the same expense | last write silently wins | `If-Match` versions: the loser gets 412 and the UI loads the winner's version |
| "Who pays whom?" | n−1 transfers from a greedy match | **minimum number of transfers** (exact subset DP), with greedy shown next to it |

## Architecture

```
web/ (TypeScript, no framework)            tallyup/ (Python)
  src/money.ts   parse/format, same rules   money.py   decimal string <-> int minor units, allocate()
  src/api.ts     typed client, retry with   splits.py  equal / shares / percent / exact -> shares
                 the same Idempotency-Key   settle.py  greedy plan, optimal plan (subset DP)
  src/app.ts     group page, live preview,  store.py   SQLite: one transaction per call,
                 edit-conflict handling                BEGIN IMMEDIATE, balances in one SQL query
        |   fetch /api/...                  api.py     FastAPI routes, error -> status mapping
        +---------------------------------> bench.py   measured comparisons; demo.py replays a scenario
```

Data model (SQLite): `groups`, `members`, `expenses` (with `version` and a soft `deleted` flag),
`expense_shares` (the frozen share per member plus its rounding bias as an exact fraction),
`payments`, and `idempotency_keys` (request hash + stored response, keyed by group and key).

The net balance per member is computed by one SQL query: *paid − owed + payments sent − payments
received*. The balances endpoint also returns `sum_minor`, which is always 0. Tests check this
after every flow.

### API

| Method | Path | Notes |
|---|---|---|
| POST | `/api/groups` | `{name, currency}` |
| GET | `/api/groups/{id}` | group + members |
| POST | `/api/groups/{id}/members` | `{name}`, unique per group |
| POST | `/api/groups/{id}/preview` | same body as an expense; returns the shares it would get |
| GET/POST | `/api/groups/{id}/expenses` | POST honours `Idempotency-Key`; response carries `ETag: "<version>"` |
| GET/PUT/DELETE | `/api/expenses/{id}` | PUT/DELETE require `If-Match: "<version>"` (428 if missing, 412 if stale) |
| GET/POST | `/api/groups/{id}/payments` | record a settle-up payment; honours `Idempotency-Key` |
| GET | `/api/groups/{id}/balances` | balances, rounding drift per member, `sum_minor` |
| GET | `/api/groups/{id}/settle?method=optimal\|greedy` | transfer plan + greedy count for comparison |

Expense body (amounts are **strings**; JSON numbers are rejected with 422):

```json
{"description": "Dinner", "payer_id": 1, "amount": "187.45",
 "split": {"kind": "shares", "parts": [{"member_id": 1, "value": 1}, {"member_id": 2, "value": 2}]}}
```

`kind` is `equal` (values ignored), `shares` (positive integers), `percent` (decimal strings that
must add up to exactly 100) or `exact` (amounts that must add up to the total).

## Setup

Requires Python 3.10+ and Node 18+ (only needed to build the UI and run the TS tests).

```bash
cd projects/2026-11-06-tallyup
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
(cd web && npm ci && npm run build)        # compiles web/src -> web/dist
```

## Usage

```bash
TALLYUP_DB=tallyup.db uvicorn --factory tallyup.api:create_app --port 8000
# open http://localhost:8000  (interactive API docs at /docs)

python -m tallyup.demo samples/scenario.json   # replay a 5-person trip through the API
python -m tallyup.bench                        # settle-up and rounding measurements
```

Tests:

```bash
python -m pytest -q          # Python: money, splits, settle oracle, API, concurrency, samples
(cd web && npm test)         # TypeScript: shared money vectors, retry/idempotency client
```

### Sample inputs and expected outputs

- `samples/scenario.json` → `samples/scenario_expected.txt`. A trip with all four split kinds, a
  retried POST (replayed, same id), an edit plus a stale retry (412), balances, both settle plans,
  and zero balances after the payments are recorded. `tests/test_samples.py` regenerates the output
  and compares it exactly.
- `samples/bench_expected.md`: output of `python -m tallyup.bench --no-timing`, also compared exactly.
- `samples/money_vectors.json`: parse cases shared by the Python and TypeScript test suites, so the
  client and server cannot drift apart on what a valid amount is.

## Measured results

From `python -m tallyup.bench` (seed 2026). Balances are produced by random equal-split expenses.
In the *mixed* rows everyone spends with everyone. In the *sub-groups* rows the members form
clusters that mostly spend among themselves, like two cars or two flats on one trip.

| members | balances | trials | greedy mean | optimal mean | optimal < greedy | max saved |
|---:|---|---:|---:|---:|---:|---:|
| 4 | mixed | 300 | 3.00 | 3.00 | 0.0% | 0 |
| 6 | mixed | 300 | 5.00 | 5.00 | 0.0% | 0 |
| 8 | mixed | 300 | 7.00 | 7.00 | 0.0% | 0 |
| 10 | mixed | 300 | 9.00 | 8.99 | 1.0% | 1 |
| 12 | mixed | 300 | 10.99 | 10.98 | 1.7% | 1 |
| 4 | sub-groups | 300 | 1.99 | 1.99 | 0.0% | 0 |
| 6 | sub-groups | 300 | 3.07 | 2.99 | 8.3% | 1 |
| 8 | sub-groups | 300 | 5.38 | 4.97 | 37.3% | 2 |
| 10 | sub-groups | 300 | 6.57 | 5.96 | 50.0% | 2 |
| 12 | sub-groups | 300 | 7.92 | 6.96 | 67.3% | 3 |

What the numbers show:

- When everyone spends with everyone, greedy is almost always already optimal (it was beaten in
  at most 1.7% of trials). The exact planner pays off when the group has hidden sub-groups that
  balance internally. With 12 members it saved transfers in 67% of trials, and up to 3 per trial.
- Rounding drift over 2,000 random equal and weighted splits (largest absolute
  *allocated − exact* total for any member, in minor units): fixed-order largest-remainder
  reached **239.03 / 240.87 / 232.71** for 3 / 4 / 6 members, which is over €2 for one person.
  Drift-aware allocation stayed at **0.82 / 1.05 / 0.96**. Every individual share is still within
  one minor unit of its exact value in both modes, and tests check that property on 3,000 random
  allocations.
- The optimal planner took a median of 0.1 ms / 2.2 ms / 52.7 ms for 8 / 12 / 16 members on the
  machine that produced this README. These times vary between runs and machines, so they are
  left out of the exact-diff file.

## Design decisions

- **Integer minor units end to end.** Amounts cross the API as decimal strings and are stored as
  `INTEGER`. Exact fractions (`fractions.Fraction`) are used only for weights and rounding bias.
- **Shares are frozen at write time.** Each expense stores its computed shares, so later
  expenses never change the history of an earlier one. Editing an expense recomputes only that
  expense, excluding its own previous bias.
- **Drift-aware leftover cents.** After flooring, the *k* leftover units go to the candidates
  whose cumulative bias would be lowest after receiving one. Ties go to the larger remainder, then
  to the earlier position. This keeps the "within one unit of exact" guarantee of largest-remainder
  and stops one member from absorbing every odd cent. It can be switched off with
  `create_app(..., drift_aware=False)`; the tests compare both modes.
- **Optimal settle-up = n − (max zero-sum groups).** Each zero-sum group of size *s* needs
  exactly *s − 1* transfers. The DP over subsets is O(2ⁿ·n). It is verified against an
  independent set-partition oracle on 400 random cases. Above 16 non-zero members it falls back
  to greedy and labels the plan `greedy_fallback`.
- **Idempotency in the same transaction as the write.** The key lookup, the insert and the
  stored response commit together under `BEGIN IMMEDIATE`. Eight concurrent requests with one
  key create exactly one expense, and a test checks this. Only successful requests are stored, so
  a request that failed validation can be fixed and resent with the same key.
- **Optimistic concurrency, not locks.** `version` increments on every update and delete. Six
  concurrent PUTs with the same `If-Match` give exactly one 200 and five 412s, and a test checks
  this.
- **The client mirrors only the parsing rules.** The browser validates amounts with the same rules
  as the server, and both are tested against the same vectors. Allocation stays on the server, and
  the UI shows the server's `/preview`, so there is only one source of truth for shares.
- **No framework in the UI.** It is about 420 lines of TypeScript compiled with `tsc`, with no
  bundler and no runtime dependencies.

## Limitations

- There are no accounts or authentication. Anyone who knows a group id can read and change it.
  Treat it as a local or LAN tool.
- One currency per group, with no FX conversion.
- Members cannot be removed or renamed, and payments cannot be edited or deleted.
- Idempotency keys never expire. A production system would set a TTL.
- A preview can differ from the stored shares if another expense lands in between, because the
  drift ledger changed. The create response always returns the stored shares.
- The "minimum transfers" objective ignores amount size and who is comfortable paying whom. The
  optimal plan can route one larger payment where greedy would use two smaller ones.
- Benchmark balances are synthetic, generated from random expenses. Real groups may contain more
  or fewer hidden sub-groups.
- The UI was checked with a scripted headless-Chromium run: create a group, add members, preview,
  reject a bad amount, an edit conflict with 412 recovery, and settle to zero. There are no
  automated browser tests in the repo.
