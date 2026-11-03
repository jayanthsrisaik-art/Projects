# cronwhen: cron schedules that handle DST correctly

A job is scheduled `30 2 * * *` in `America/New_York`. On 8 March 2026 the clock
jumps from 01:59 to 03:00, so 02:30 never happens. On 1 November 01:30 happens twice.
What should the scheduler do? Many hand-rolled schedulers step a time-zone-aware
`datetime` forward with `timedelta` and never ask. In the benchmark below they
run some jobs an hour late, run others twice at the same instant, and drop a
whole hour of runs for an every-15-minutes job.

`cronwhen` is a small cron engine that makes those cases explicit:

* **Parser** for 5-field cron with names, lists, ranges, steps, macros, and the
  `L`, `L-n`, `nW`, `LW`, `nL`, `n#k` extensions. Errors name the field and the token.
* **Fast search** that skips whole months, days, and hours on the wall clock
  instead of scanning minute by minute.
* **Time-zone mapping** with explicit **gap** (`shift`/`skip`) and **overlap**
  (`once`/`twice`) policies. The default is `auto`, which follows Vixie cron: fixed-time jobs run
  once per day, and interval jobs keep real-time spacing.
* **Linter** for expressions that never fire, skip months, have no-op steps,
  combine day fields with OR, or are exposed to DST in a given zone and year.
* **DST impact report** for a job file: every run that is shifted, skipped,
  suppressed, repeated, or coalesced in a period.
* **Verification** against an independent brute-force oracle that walks the
  *UTC* timeline minute by minute.

Python standard library only.

## Architecture

```
expr ──parser.parse──► CronSpec (frozen sets + special-day tokens + star flags)
                          │
                          ▼
             matcher.LocalSearcher          naive wall-clock minutes, in order
             (month → day → hour → minute skipping, 400-year horizon)
                          │ candidate wall time t
                          ▼
             schedule.resolve_wall_time(tz, t)
               ok      → [instant]
               overlap → [first, second]          (fold=0 / fold=1)
               gap     → [transition instant]     (binary search on seconds)
                          │ Occurrence(local, instant, status)
                          ▼
             min-heap keyed by instant ──► released when no later candidate can
             precede it (|UTC offset| ≤ 14h, 26h slack) ──► dedupe → fires
```

| module | responsibility |
|---|---|
| `cronwhen/parser.py` | grammar, validation, `CronSpec` |
| `cronwhen/matcher.py` | day-of-month / day-of-week rules, `LocalSearcher` |
| `cronwhen/schedule.py` | `Schedule`, DST classification, policies, ordered occurrence stream |
| `cronwhen/oracle.py` | brute-force reference: UTC minute scan with separately written day rules |
| `cronwhen/baseline.py` | naive wall-clock-`timedelta` scheduler, used for comparison |
| `cronwhen/lint.py` | static checks and DST exposure |
| `cronwhen/crontab.py` | job file format, DST impact report |
| `cronwhen/cli.py`, `cronwhen/bench.py` | CLI and measurement harness |

### Occurrence statuses

| status | meaning | fires |
|---|---|---|
| `ok` | wall time exists once | yes |
| `overlap-first` | first of two occurrences of a repeated wall time | yes |
| `repeat` | second occurrence, `overlap=twice` | yes |
| `suppressed` | second occurrence, `overlap=once` | no |
| `shifted` | nonexistent wall time, `gap=shift`: runs at the first instant after the jump | yes |
| `skipped` | nonexistent wall time, `gap=skip` | no |
| `coalesced` | would fire at an instant that already fired (e.g. shifted 02:30 and real 03:00) | no |

`auto` policies: a job is an *interval job* when its minute **or** hour field
starts with `*`. This is Vixie cron's rule. Interval jobs use `gap=skip, overlap=twice`, so
`*/15 * * * *` keeps exactly 15 real minutes between runs. Fixed-time jobs use
`gap=shift, overlap=once`, so `30 2 * * *` runs exactly once every day.

## Setup

```bash
cd projects/2026-11-03-cronwhen
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt      # only pytest; the package itself is stdlib-only
```

Python 3.9+ with an IANA time-zone database. Linux and macOS ship one. On Windows, add `tzdata`.

## Usage

```bash
# upcoming runs; --all also lists non-firing occurrences
python -m cronwhen next "30 2 * * *" --tz America/New_York --after 2026-03-07T00:00 -n 4 --all
# 30 2 * * *  tz=America/New_York  gap=shift  overlap=once
  2026-03-07T02:30:00-05:00
  2026-03-08T03:00:00-04:00  [shifted, wall 2026-03-08 02:30]
  2026-03-09T02:30:00-04:00
  2026-03-10T02:30:00-04:00

python -m cronwhen next "30 2 * * *" --tz America/New_York --after 2026-03-07T12:00 --gap skip --json

# lint (exit 0 clean, 1 warnings, 2 errors)
python -m cronwhen lint "0 0 30 2 *" "0 0 31 * *" "30 1 * * *" --tz Europe/London --year 2026

# which jobs does DST touch this year?
python -m cronwhen dst-report examples/jobs.cron --start 2026-01-01 --end 2027-01-01

# correctness + speed measurement (takes ~80 s, mostly the brute-force oracle)
python -m cronwhen.bench --year 2026
```

Library:

```python
from datetime import datetime
from cronwhen import Schedule

s = Schedule("0 9 * * MON#1", "Europe/Berlin")           # first Monday, 09:00
s.next(datetime(2026, 11, 3), n=3)                       # aware datetimes in Berlin time
for occ in Schedule("*/30 * * * *", "America/New_York").occurrences(
        datetime(2026, 11, 1), datetime(2026, 11, 1, 3)):
    print(occ.local, occ.instant, occ.status, occ.fires)
```

Naive `after`/`until` values are read as wall time in the schedule's zone. `after` is exclusive.

### Job file format

```
# <name>          <time zone>           <cron expression>
nightly-backup    America/New_York      30 2 * * *
metrics-rollup    Europe/London         */15 1 * * *
board-pack        UTC                   0 9 * * MON#1
```

Only whole-line comments are supported, because `#` is valid inside expressions (`FRI#3`).

## Sample inputs and expected outputs

| input | command | expected output |
|---|---|---|
| `examples/jobs.cron` | `dst-report … --start 2026-01-01 --end 2027-01-01` | `examples/expected/dst_report_2026.txt` |
| `examples/expressions.txt` | `lint <each line> --tz Europe/London --year 2026` | `examples/expected/lint_london_2026.txt` |
| — | `next "30 2 * * *" …` (spring forward) | `examples/expected/next_backup_spring.txt` |
| — | `next "*/30 * * * *" …` (fall back) | `examples/expected/next_interval_fall.txt` |
| — | `python -m cronwhen.bench --year 2026` | `examples/expected/bench_2026.txt` |

The CLI tests compare their output byte for byte against the first four files.

## Verification and measured results

**Oracle.** `oracle.brute_force_fires` decides for every real UTC minute whether the job fires.
It does not reuse the fast matcher's logic:

* It uses the fold of the local time for overlaps.
* It detects gaps from a forward jump in wall time between consecutive minutes.
* Its day rules are written differently: for example, `nW` is "the closest in-month
  workday by scanning", and `n#k` is "the k-th element of the list of that weekday".

The test suite compares the engine with this oracle:

* 12 zones, including half-hour DST (Lord Howe), +12:45 (Chatham), midnight
  transitions (Santiago, Havana), negative DST (Dublin), Casablanca, Tehran,
  St John's, and Samoa's skipped day of 2011-12-30
* 4 random transitions per zone, 6 random expressions per transition, and 3 policy combinations
* About 4.3k shifted, skipped, repeated, suppressed, and coalesced occurrences in total, plus 2.4k overlap-first ones

The fast `LocalSearcher` is also checked against a naive minute scan, and the day
matcher against the oracle over 3-year spans, for 210 random expressions.

**Benchmark** (`examples/expected/bench_2026.txt`, measured in this run): 7 jobs × 4
zones, full calendar year 2026, 248,445 oracle fires, default `auto` policies.

| scheduler | missing fires | extra fires | duplicate instants | wrong job×zone pairs |
|---|---|---|---|---|
| cronwhen engine | 0 | 0 | 0 | 0 / 28 |
| naive `timedelta` baseline | 50 | 61 | 59 | 14 / 28 |

The naive baseline's errors fall into three groups:

* **Repeated hour never visited.** `*/15` loses 4 runs on fall-back night.
* **Gap minutes collide.** Nonexistent 02:xx wall times convert to the same instants as real
  03:xx, so they fire twice.
* **Fixed-time job runs late.** Nonexistent 02:30 runs at 03:30 instead of right after the jump.

Speed: the engine took about 2.8 s for all 28 pairs (about 90k fires/s, including heap
ordering and DST classification). The minute-scan oracle took about 37.6 s. Timings vary by machine.

Run the tests:

```bash
python -m pytest -q tests        # 76 tests, ~15-20 s
```

## Design decisions

* **Search on the wall clock, then map to instants.** Cron is a wall-clock
  language, so matching happens on naive local time, where field skipping is simple.
  The only time-zone question is "what instant(s) does this wall minute correspond
  to?" `zoneinfo`'s `fold` gives two readings. If they agree, the time is normal. If
  both round-trip, it is an overlap. If neither does, it is a gap.
* **Gap shift goes to the transition instant, not `fold=0`.** Converting a nonexistent
  02:30 with `fold=0` gives 03:30, an hour late. `cronwhen` binary-searches the
  instant where the clock jumps and fires there. This is also how Vixie cron behaves.
* **Ordered output despite wall-clock iteration.** In a fall-back overlap, wall time
  01:45 (first) comes before 01:15 (second) in real time. Events go into a heap and
  are released only once no future wall-clock candidate can come earlier. Real
  offsets are within ±14 h, so a 26 h slack is safe.
* **Vixie day semantics.** If either day field starts with `*`, both fields
  must match, and the starred one still applies. So `*/2` limits the job to odd days.
  Otherwise the fields combine with OR. Use `dom_dow="and"` / `--dom-dow and` for strict
  AND. An early version ignored the `*/2` set, and a unit test caught it.
* **400-year horizon.** The Gregorian calendar repeats its weekday and leap-year
  pattern every 400 years. So "no match within 400 years" is a proof that an
  expression never fires, not a heuristic.
* **Non-firing occurrences are first-class.** Skipped, suppressed, and coalesced
  occurrences go through the same stream. That is what powers the DST report and lint.

## Limitations

* Minute resolution only. There is no seconds field and no year field (Quartz has both).
* Historical offsets with seconds (LMT before ~1900, Liberia before 1972) are
  handled by the engine but are not covered by the oracle tests. The tests use 2008-2031.
* `nW` never crosses a month boundary, and `31W` does not fire in 30-day months.
  This matches Quartz, but other implementations differ.
* Wrap-around ranges (`FRI-MON`, `22-2`) are rejected rather than interpreted.
* The `auto` policy uses Vixie's "starts with `*`" rule. So `*/30 2 * * *` counts as an
  interval job and is skipped in a gap. Pass `--gap shift` if that is not wanted.
* The engine is a calculator, not a daemon. It does not handle missed runs after
  downtime, locking, or execution.
* Results depend on the installed tz database version. Future DST rules can change.
