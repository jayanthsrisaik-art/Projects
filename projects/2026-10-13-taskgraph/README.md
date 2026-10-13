# taskgraph – dependency-aware, cached task runner

A small `make`-like tool: define tasks and their dependencies in TOML, and it runs them in parallel, skipping any task whose inputs haven't changed. Standard library only (Python 3.11+); `pytest` for tests.

## Problem
Ad-hoc scripts rerun everything every time, and `make` relies on file mtimes (so `touch` triggers rebuilds, and tasks without file outputs can't be cached). `taskgraph` decides freshness from **content hashes**, cascades invalidation downstream, and runs independent tasks concurrently.

## Architecture
```
tasks.toml ─▶ config.py (parse/validate) ─▶ graph.py (cycle check, target selection, waves)
                                         └▶ runner.py (thread pool + subprocess) ◀▶ cache.py (fingerprints, .taskgraph/cache.json)
cli.py: list | graph | run
```
- **Fingerprint** = SHA-256 of (task name, command, contents of files matched by `inputs`, fingerprints of dependencies). An upstream change therefore changes every downstream fingerprint.
- A task is **cached** if its stored fingerprint matches and all declared `outputs` still exist.
- The scheduler launches a task as soon as all its dependencies finish. If one fails, its transitive dependents are **skipped**; unrelated branches continue. Failed tasks are never cached.

## Setup
```
pip install -r requirements.txt     # only needed for tests
python -m pytest
```

## Usage
```
python -m taskgraph -f samples/demo/tasks.toml list
python -m taskgraph -f samples/demo/tasks.toml graph [targets...]
python -m taskgraph -f samples/demo/tasks.toml run [targets...] [-j 4] [--force] [--dry-run]
```
Task file:
```toml
[task.stats]
description = "count unique words"
cmd = ["python3", "-c", "..."]   # list, or a string split with shlex (no shell)
deps = ["sort"]
inputs = ["build/sorted.txt"]    # globs relative to the task file
outputs = ["build/stats.txt"]    # optional; cache is invalid if missing
```
Exit code: 0 success, 1 a task failed/skipped, 2 config error (including dependency cycles, reported with the cycle path). See `samples/expected_output.txt` (the demo output was produced by actually running it; the test suite asserts it).

## Design decisions
- Content hashing over mtimes: robust to `touch`, git checkouts, and clock skew.
- Commands run without a shell (explicit and portable; use `["sh","-c",...]` if you need one).
- Threads + `subprocess` are enough since the work happens in child processes.
- Cache file is written atomically at the end of a run.

## Limitations
- Undeclared inputs are invisible: if a command reads a file not listed in `inputs`, changes to it won't trigger reruns. A task with no inputs/outputs is cached forever until its command or deps change (use `--force`).
- Environment variables are not part of the fingerprint.
- Outputs are checked for existence only, not content; no remote cache; no output streaming (output is shown after a task finishes, only for failures).
- Two tasks writing the same file are not detected.
- Tests for parallelism use a timing bound (3×1s tasks finish in <2.5s), which could be flaky on a heavily loaded machine.

## Tests
25 tests cover config validation, cycle detection, selection/waves, cache hit/invalidation (content change, touch-only, missing output, cascade), failure propagation, missing executables, dry-run/force, parallelism, ordering, and the CLI end-to-end on the demo.
