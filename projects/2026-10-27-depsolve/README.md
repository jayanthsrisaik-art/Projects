# depsolve — a semver dependency resolver that explains itself

Package managers have to answer a hard question: given a manifest (`web ^2.0.0`,
`orm ^3.0.0`, …) and a registry where every release declares its own version
ranges, which exact version of every package should be installed? A naive
installer that grabs the newest match for each package breaks as soon as two
packages disagree three levels down — and when no answer exists, users get
`ERESOLVE` walls of text instead of the one line that matters.

`depsolve` is a small, dependency-free resolver for that problem:

* **npm-style ranges** – `^`, `~`, `x`/`*` wildcards, partial versions, hyphen
  ranges, `||`, and npm's prerelease rule; full SemVer 2.0.0 precedence.
* **Backtracking search with conflict-directed backjumping** – MRV variable
  ordering, forward checking of each candidate's dependencies, and jumps over
  decisions that did not cause a failure.
* **Lockfiles** – deterministic JSON with `required_by`; existing locks are
  preferred so re-resolving is stable, `--upgrade PKG` relaxes only `PKG`.
* **Yanked and prerelease releases** – never picked unless locked / asked for.
* **Explanations** – a minimal set of conflicting root requirements plus the
  concrete incompatibility, grouped across versions.
* **`check` / `why`** – independently verify a lockfile, and show the chain that
  pulls a package in.

Python standard library only; `pytest` for tests.

## Example

`samples/registry.json` is a small web-stack registry. `web 2.3.0` (newest)
needs `http ^2` → `tls ^3`, but every `orm 3.x` needs `tls ^2`.

```console
$ python -m depsolve resolve --registry samples/registry.json --manifest samples/app.json --out app.lock.json
resolved 7 packages (10 candidates tried, 1 backtracks, 0 backjumps)

$ python -m depsolve why --lock app.lock.json crypto
shop-api -> orm@3.1.0 -> tls@2.4.0 -> crypto@3.2.0
```

The resolver settles on `web 2.2.1` / `http 1.8.4` / `tls 2.4.0`, skips the
yanked `log 1.4.0`, `crypto 3.1.5` and the `orm 3.2.0-beta.1` /
`json 2.0.0-rc.1` prereleases. The greedy baseline fails on this manifest.
Full output: [`samples/expected/app.lock.json`](samples/expected/app.lock.json).

Asking for `web ^2.3.0` makes it unsatisfiable
([`samples/app-conflict.json`](samples/app-conflict.json)):

```console
$ python -m depsolve resolve --registry samples/registry.json --manifest samples/app-conflict.json
Resolution failed.
These root requirements cannot be installed together:
  - orm ^3.0.0
  - web ^2.3.0
Because:
  - http@2.0.0/2.1.0 requires tls ^3.0.0, but orm@3.0.0/3.1.0 requires tls ^2.0.0/^2.1.0; no version of tls satisfies both
(searched 11 candidates, 4 backjumps)
```

(`log ^1.0.0` was in the manifest too; the explanation drops it because it is
not part of the conflict.)

Re-resolving against an old lock keeps every locked version (even the yanked
`crypto 3.1.5`, as a real lock would) and only moves what you ask:

```console
$ python -m depsolve resolve --registry samples/registry.json --manifest samples/app.json \
    --lock samples/old.lock.json --upgrade log --out app.lock.json
resolved 7 packages (7 candidates tried, 0 backtracks, 0 backjumps)
  ↑ log 1.2.0 -> 1.3.2
```

Other commands:

```console
$ python -m depsolve check --registry samples/registry.json --manifest samples/app.json --lock app.lock.json
ok: 7 locked packages satisfy the manifest and registry
$ python -m depsolve match "^1.2.0 || >=2.0.0-rc.0 <2.0.0" 1.9.9 2.0.0-rc.1 2.0.0 1.5.0-beta
1.9.9       yes
2.0.0-rc.1  yes
2.0.0       no
1.5.0-beta  no
```

Exit codes: `0` success, `1` unsatisfiable / failed check, `2` invalid input or
step budget exceeded.

## Setup

```bash
cd projects/2026-10-27-depsolve
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt      # only pytest; the tool itself is stdlib
python -m pytest -q                  # 339 tests
python bench.py                      # ~3 s; output in samples/expected/bench.md
```

## Architecture

```
depsolve/
  semver.py     Version parsing + SemVer 2.0.0 precedence (build metadata ignored)
  ranges.py     npm range grammar -> OR of comparator sets; prerelease rule
  registry.py   JSON registry / manifest / lock loading
  resolver.py   search (MRV + forward checking + backjumping), verify()
  explain.py    minimal root core + grouped incompatibilities
  greedy.py     baseline: newest-first, no backtracking
  oracle.py     exhaustive solver for small registries (tests, bench)
  lockfile.py   lock output, lock diff, why()
  synth.py      seeded synthetic registries
  cli.py        resolve | check | why | match
```

**Search.** Each package is a variable; it becomes *pending* when the root or
a chosen release depends on it. The resolver repeatedly picks the pending
package with the fewest surviving candidates, tries candidates in preference
order (locked, then newest), and forward-checks each candidate: a dependency on
an already-chosen package must accept that version, and a dependency on a
pending package must leave it at least one candidate.

**Backjumping.** Every failure returns a *conflict set*: the chosen packages
responsible. For a package that runs out of candidates that is the packages
whose requirements narrowed it, plus whatever each tried candidate's failure
blamed. If the package currently being decided is not in the set returned by a
deeper failure, its other candidates cannot help, so the search returns
immediately to an earlier decision instead of enumerating siblings.

**Explanations.** A deletion filter over the root requirements produces a
minimal unsatisfiable core (each probe is a bounded resolve). The search on that
core records every "no candidates left" event; each is reduced to its simplest
cause (missing package → one constraint matching nothing → a pair of
constraints with no common version) and pairs are merged across versions of
the same packages.

## Results (measured, `python bench.py`)

1000 seeded random registries (3–5 packages, 1–3 versions each, random ranges,
yanked releases, locks, occasional missing packages), every one checked
against brute-force enumeration:

| strategy | solvable found | candidates tried |
|---|---:|---:|
| oracle (brute force) | 228/228 | - |
| greedy, no backtracking | 163/228 | - |
| chronological backtracking | 228/228 | 944 |
| backjumping (default) | 228/228 | 932 |

The resolver returned 0 invalid solutions and agreed with the oracle on all 772
unsatisfiable seeds. Those registries are too small for backjumping to matter
much. On a constructed deep conflict (`a@2 → q → w ^2` against root `w ^1`,
with *k* unrelated 4-version packages decided in between) it does:

| k | chronological | backjumping |
|---:|---:|---:|
| 3 | 410 | 14 |
| 5 | 6,492 | 18 |
| 7 | 103,774 | 22 |

## Tests

* `test_semver.py` – the SemVer spec's precedence example, invalid inputs.
* `test_ranges.py` – 52 range/version cases covering every operator, partials,
  hyphens, `||`, and the prerelease rule.
* `test_resolver.py` – sample resolution, greedy failure, yanked/prerelease/lock
  behaviour, cycles, missing packages, reachability, backjump step counts,
  step budget, `verify()`.
* `test_property.py` – 250 seeded random registries: backjumping and
  chronological search must both match the exhaustive oracle, return verified
  solutions, and agree with each other; greedy must never return an invalid
  solution.
* `test_explain.py` – minimal core, reason wording, missing / no-match cases.
* `test_cli.py` – golden outputs in `samples/expected/`, lock preference,
  `--upgrade`, `check`, `why`, error exit codes.

## Design decisions

* **Backtracking + backjumping rather than SAT or full PubGrub.** It is easy to
  read, provably matches brute force on the property suite, and backjumping
  removes the exponential blow-up from unrelated decisions. PubGrub's learned
  incompatibilities would also prune *repeated* conflicts and produce
  derivation-tree explanations; that is the natural next step.
* **Preference order = lock, then newest.** The first solution found is the
  answer; there is no global optimisation (e.g. "newest overall" or
  "fewest changes"), which keeps behaviour predictable.
* **Explanations are separate from search.** The core is computed by re-running
  bounded resolves, so explanation quality never slows down the success path.
* **`verify()` is independent of the search** and is used by tests, the bench
  and `depsolve check`, so a resolver bug cannot also hide itself.
* **Counts, not timings, in the benchmark**, so the numbers are reproducible.

## Limitations

* One version per package (no npm-style nested duplicates), no optional/peer/dev
  dependencies, no platform markers or features.
* The registry is a local JSON file; no network fetching, integrity hashes or
  caching.
* No clause learning: hard instances with many *distinct* conflicts can still
  be exponential. A `--max-steps` budget (default 200,000) turns this into an
  error instead of a hang.
* The explanation core is minimal, not minimum, and reasons describe the
  conflicts the search hit on the core, not a full proof tree. If a core probe
  exceeds its budget the requirement is conservatively kept.
* Range grammar follows npm semantics; `!=`, Python-style `~=` and Cargo's
  default-caret bare versions are not supported (a bare `1.2.3` means exactly
  that version).
