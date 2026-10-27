# threeway — Myers diff, patches and a diff3 merge that matches git

Every version-control system needs three text algorithms: a **diff** that
finds what changed, a **patch** format that can be applied somewhere else,
and a **three-way merge** that combines two people's edits to the same base.
The merge is the hard one. With repeated lines (blank lines, `}`, `return None`),
a minimal diff has many equally short alignments. If the two sides' diffs pick
differently, diff3 reports a conflict that isn't there or, worse, silently
merges to the wrong file.

`threeway` implements all three from scratch, using only the Python standard
library:

* **Myers O(ND) diff** with **slider compaction**: change groups are moved to
  a canonical position (up as far as possible, merging neighbours, then down),
  the same normalisation git's xdiff does. It is still a minimal diff.
* **Unified diff** output that GNU `patch` accepts, a parser, and an applier
  that finds hunks at an offset, refuses fuzz, and handles
  `\ No newline at end of file`.
* **diff3 merge** (Khanna, Kuber & Pierce, *A Formal Investigation of Diff3*)
  with git's three conflict styles: `merge` (with zealous refinement),
  `diff3` and `zdiff3`. On the bundled samples the output is **byte-identical
  to `git merge-file`** in all three styles.
* A **ground-truth benchmark** that generates synthetic merges with a known
  right answer. It scores threeway against raw Myers (ablation), a `difflib`
  matcher (baseline) and `git merge-file`.

## Example

```console
$ python -m threeway merge samples/ours.py samples/base.py samples/theirs.py     # clean, exit 0
$ python -m threeway merge -L ours -L base -L theirs --style diff3 \
      samples/ours.py samples/base.py samples/theirs_conflict.py
...
def total(items):
    """Sum price * qty over the cart."""
    result = 0
    for item in items:
<<<<<<< ours
        result += item["price"] * item.get("qty", 1)
||||||| base
        result += item["price"]
=======
        result += round(item["price"], 2)
>>>>>>> theirs
    return result
...
1 conflict(s)                                     # exit code 1 = number of conflicts
$ python -m threeway diff samples/base.py samples/ours.py > ours.patch           # exit 1: files differ
$ python -m threeway apply samples/base.py ours.patch -o out.py
$ python -m threeway bench --trials 3000 --seed 1
```

Arguments follow `git merge-file`: `ours base theirs`. The exit status is the
number of conflicts (0 means clean). Expected outputs for every command above
are in `samples/expected/`.

## Architecture

```
threeway/
  myers.py    Myers forward search + backtrack, prefix/suffix stripping,
              slider compaction; difflib matcher (baseline); matching blocks
  unified.py  opcodes, hunk grouping/rendering, parse_patch, apply_patch
  merge3.py   sync regions -> chunk classification -> conflict styles -> markers
  bench.py    scenario generator with ground truth, tool adapters, scoring
  cli.py      diff / apply / merge / bench
```

Data flow for a merge:

1. `myers_blocks(base, ours)` and `myers_blocks(base, theirs)` return
   matching blocks `(base_i, side_j, length)`, the same shape as
   `difflib.SequenceMatcher.get_matching_blocks`, so the matchers can be
   swapped.
2. `sync_regions` intersects the two block lists. Base lines matched by
   **both** diffs are stable and are copied through unchanged.
3. Each unstable chunk between sync regions is classified by comparing its
   three versions: `unchanged`, `ours`, `theirs`, `same` (identical change on
   both sides) or `conflict`.
4. Conflict styles post-process conflicts:
   * `merge`: diff ours against theirs inside the conflict and move shared
     lines out (git's zealous merge). Shared runs with no alphanumeric
     character, such as a lone `}` or a blank line, stay inside, so one
     conflict isn't split into many small ones.
   * `diff3`: ours / base / theirs, unrefined.
   * `zdiff3`: diff3, with the common prefix and suffix of ours and theirs
     moved outside the markers.

## Setup

```bash
cd projects/2026-10-27-threeway
python -m pip install -r requirements.txt    # only pytest; runtime is stdlib
python -m pytest -q
```

Python 3.10+. `git` and GNU `patch` are optional. If they're installed, the
interop tests and the `git-merge-file` row of the benchmark use them; if not,
they're skipped.

## Tests

45 tests (`python -m pytest -q`, about 0.6 s):

* **Diff correctness:** on 800 random small inputs (400 raw, 400 compacted)
  plus 300 inputs made of repeated lines, the match length equals an O(nm)
  LCS dynamic-programming oracle. Every block is
  valid. Edge cases (empty inputs, Myers' paper example with D = 5). A test
  shows the `difflib` baseline is not always minimal.
* **Compaction:** a minimal case (`bbb`, ours `bOb`, theirs `Tbb`) where raw
  Myers conflicts and compacted Myers merges to the correct `TbOb`.
  Compaction also preserves minimality on random inputs.
* **Patches:** splitting on `\n` only (CRLF and form-feed round-trips), random diff→apply round-trips at context 0/1/3 and with the
  difflib matcher. A golden patch file. A missing final newline in either
  direction. Hunk-header ranges for pure inserts and deletes. Applying at an
  offset. Rejecting mismatched context and truncated hunks. GNU `patch`
  applies our output.
* **Merge:** properties on 500 random files with both matchers (one-sided
  change → that side; identical change → that change; no change → base).
  Disjoint, adjacent, delete-vs-modify and same-point-insert cases. Zealous
  refinement, including the rule that punctuation-only runs stay inside a
  conflict. zdiff3 trimming. Markers when the last line has no newline.
  Golden outputs. **Byte-identical output and exit codes versus
  `git merge-file`** for clean, merge, diff3 and zdiff3.
* **CLI and benchmark:** exit codes, diff→apply via the CLI, apply failure,
  benchmark determinism, and a check that every generated `overlap`
  scenario is a true conflict.

## Benchmark (measured)

`bench.py` generates a 60-line code-like base file. About 35% of its lines
are common repeated lines: blank, `return None`, `}`, `pass`. Each side gets
1–3 random edits (replace, insert or delete; inserted text sometimes contains
common lines). Because the edits are generated, the correct answer is known:

* `disjoint`: every ours edit is at least one untouched line away from every
  theirs edit. The correct result is clean and equals the base with both
  edit sets applied.
* `identical`: the same, plus one identical edit on both sides. Clean, and
  the edit must appear once.
* `overlap`: both sides rewrite the same base line with different unique
  content. A conflict is required.

`silent-wrong` counts clean merges that differ from the expected file, the
outcome that actually hurts users. `spurious-conf` counts conflicts reported
where a clean merge was expected. Command:
`python -m threeway bench --trials 3000 --seed 1` (1,000 scenarios per kind;
git 2.43.0):

| tool | disjoint correct | identical correct | overlap detected | silent-wrong | spurious conflicts (of 2,000) |
|---|---:|---:|---:|---:|---:|
| **threeway** (Myers + compaction) | 0.997 | 0.979 | 1.000 | **0** | 24 |
| ablation: raw Myers | 0.997 | 0.966 | 1.000 | 2 | 35 |
| baseline: difflib matcher | 0.998 | 0.983 | 1.000 | 2 | 17 |
| git merge-file | 1.000 | 0.984 | 1.000 | 0 | 16 |

Takeaways from these numbers:

* Compaction is worth having. Compared with the same algorithm without it,
  spurious conflicts fall from 35 to 24 and silent wrong merges from 2 to 0.
* The difflib matcher has fewer spurious conflicts (17) but produced 2
  silently wrong clean merges. For a merge tool, that's the worse trade.
* git is still ahead: 16 spurious conflicts against 24. The cases left over
  are insertions next to an identical
  existing line, where any choice is a guess. git makes better guesses there
  because it also aligns change groups with groups in the other file
  (`end_matching_other`) and has an indent heuristic. threeway implements
  neither.
* Results with the default `bench` (300 trials, seed 7) are in
  `samples/expected/bench_seed7.txt`. These are synthetic files, not real
  repositories, so the absolute rates don't transfer. Read them as a
  comparison between tools.

Performance (measured on this machine): a 20,000-line file with 50 edits
diffs in 0.02 s and three-way merges in 0.04 s. Two completely different
2,000-line files (D = 4,000) take 2.0 s.

## Design decisions

* **Matching blocks as the interface.** diff, patch and merge consume one
  format, so the matcher is a parameter (`--matcher myers|myers-raw|difflib`).
  The ablation and the baseline therefore run through exactly the same merge
  code.
* **Compaction as a separate pass.** Rotating a change group by one line,
  when the line just outside it equals the line at its far end, keeps every
  unchanged line's rank and content. So compaction never breaks the alignment
  or minimality, and a test checks that against the LCS oracle.
* **No fuzz when applying.** Hunks may move to the nearest exact match (the
  offset is reported), but context lines must match. Fuzzy patching of code
  hides problems.
* **Lines split on `\n` only and keep their terminators.** CRLF, a missing
  final newline, form feeds and Unicode line separators survive without
  special cases (`str.splitlines` would split on the last two). Conflict markers add a `\n` to an
  unterminated last line so markers always start on a new line, as git does.
* **Exit codes follow tool conventions**: `diff` returns 1 when the files
  differ, `merge` returns the conflict count (capped at 127), `apply` returns
  2 when a patch doesn't apply.

## Limitations

* Memory is O(D²) for the Myers trace. That's fine for typical edits, but
  very different large files are slow. There's no linear-space
  divide-and-conquer variant and no histogram or patience diff.
* Compaction slides groups but has no indent heuristic or cross-file group
  alignment, which is why git still has fewer spurious conflicts.
* Only single-file patches. No `diff --git` headers, renames, binary files,
  or `-p` path stripping. Hunk headers have no function-name context, so the
  text after `@@` differs from `git diff`; the hunks themselves are the same.
* The merge works on whole lines. There's no word-level or syntax-aware
  merging and no rename or move detection. Conflict-marker length is fixed
  at 7.
* The benchmark data is synthetic. It controls for ground truth, but real
  merges (refactors, moved code) are harder.
