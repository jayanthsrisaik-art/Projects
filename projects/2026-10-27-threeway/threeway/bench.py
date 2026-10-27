"""Synthetic three-way merge benchmark with known ground truth.

Each trial builds a code-like base file (with the repeated lines real code has:
blank lines, ``return None``, closing braces), then gives "ours" and "theirs"
their own edits. Because we generate the edits, we know the right answer:

* ``disjoint``  -- every ours edit is separated from every theirs edit by at
  least one untouched base line. The correct merge is clean and equals the base
  with both edit sets applied.
* ``identical`` -- like disjoint, but both sides also make one identical edit.
  Still clean; the shared edit must appear once.
* ``overlap``   -- one ours edit and one theirs edit delete the same base line
  and replace it with different content. A correct merge must conflict.

Merge tools are scored on how often they get each case right and, most
importantly, on *silent wrong merges*: a clean result that is not the
expected file.
"""

from __future__ import annotations

import os
import random
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import Callable

from .merge3 import merge3
from .myers import difflib_blocks, myers_blocks, myers_raw_blocks
from .unified import split_lines

COMMON = ["\n", "    return None\n", "}\n", "    pass\n", "\n"]
Edit = tuple[int, int, list[str]]  # replace base[start:end] with lines


def make_base(rng: random.Random, n: int) -> list[str]:
    lines = []
    for i in range(n):
        if rng.random() < 0.35:
            lines.append(rng.choice(COMMON))
        else:
            lines.append(f"    value_{i} = compute({rng.randint(0, 999)})\n")
    return lines


def _new_lines(rng: random.Random, tag: str, count: int, allow_common: bool = True) -> list[str]:
    out = [f"    {tag}_{k} = patch({rng.randint(0, 99999)})\n" for k in range(count)]
    if allow_common and count and rng.random() < 0.3:
        out.insert(rng.randint(0, len(out)), rng.choice(COMMON))
    return out


def _random_edit(rng: random.Random, n: int, tag: str) -> Edit:
    start = rng.randint(0, n)
    delete = rng.randint(0, min(3, n - start))
    add = rng.randint(1 if delete == 0 else 0, 3)
    return (start, start + delete, _new_lines(rng, tag, add))


def _separated(e: Edit, others: list[Edit]) -> bool:
    """At least one untouched base line between e and every edit in others."""
    s, t, _ = e
    return all(s - ot >= 1 or os_ - t >= 1 for os_, ot, _ in others)


def _pick_edits(rng: random.Random, n: int, tag: str, count: int, avoid: list[Edit]) -> list[Edit]:
    edits: list[Edit] = []
    for _ in range(count * 20):
        if len(edits) == count:
            break
        e = _random_edit(rng, n, tag)
        if _separated(e, edits + avoid):
            edits.append(e)
    return sorted(edits, key=lambda e: (e[0], e[1]))


def apply_edits(base: list[str], edits: list[Edit]) -> list[str]:
    out: list[str] = []
    pos = 0
    for s, t, new in sorted(edits, key=lambda e: (e[0], e[1])):
        out.extend(base[pos:s])
        out.extend(new)
        pos = t
    out.extend(base[pos:])
    return out


@dataclass
class Scenario:
    kind: str
    base: list[str]
    ours: list[str]
    theirs: list[str]
    expected: list[str] | None  # None when a conflict is required


def make_scenario(rng: random.Random, kind: str, n: int = 60) -> Scenario:
    base = make_base(rng, n)
    ours_e = _pick_edits(rng, n, "ours", rng.randint(1, 3), [])
    if kind == "overlap":
        # Both sides rewrite the same base line(s) differently.
        s = rng.randint(0, n - 1)
        t = min(n, s + rng.randint(1, 2))
        ours_e = [e for e in ours_e if _separated(e, [(s, t, [])])]
        # Only unique new lines: re-inserting a deleted common line would turn the
        # "rewrite" into a plain insertion that legitimately merges clean.
        ours_e.append((s, t, _new_lines(rng, "ours_x", rng.randint(1, 2), allow_common=False)))
        theirs_e = _pick_edits(rng, n, "theirs", rng.randint(0, 2), [(s, t, [])] + ours_e)
        theirs_e.append((s, t, _new_lines(rng, "theirs_x", rng.randint(1, 2), allow_common=False)))
        return Scenario(kind, base, apply_edits(base, ours_e), apply_edits(base, theirs_e), None)
    theirs_e = _pick_edits(rng, n, "theirs", rng.randint(1, 3), ours_e)
    shared: list[Edit] = []
    if kind == "identical":
        shared = _pick_edits(rng, n, "both", 1, ours_e + theirs_e)
    ours = apply_edits(base, ours_e + shared)
    theirs = apply_edits(base, theirs_e + shared)
    expected = apply_edits(base, ours_e + theirs_e + shared)
    return Scenario(kind, base, ours, theirs, expected)


Tool = Callable[[list[str], list[str], list[str]], tuple[bool, list[str]]]


def tool_myers(base, ours, theirs):
    r = merge3(base, ours, theirs, matcher=myers_blocks)
    return r.clean, r.lines


def tool_myers_raw(base, ours, theirs):
    r = merge3(base, ours, theirs, matcher=myers_raw_blocks)
    return r.clean, r.lines


def tool_difflib(base, ours, theirs):
    r = merge3(base, ours, theirs, matcher=difflib_blocks)
    return r.clean, r.lines


def make_git_tool(workdir: str) -> Tool | None:
    git = shutil.which("git")
    if git is None:
        return None

    def tool_git(base, ours, theirs):
        paths = []
        for name, lines in (("ours", ours), ("base", base), ("theirs", theirs)):
            p = os.path.join(workdir, name)
            with open(p, "w", encoding="utf-8", newline="") as fh:
                fh.writelines(lines)
            paths.append(p)
        proc = subprocess.run([git, "merge-file", "-p", *paths], capture_output=True, text=True)
        if proc.returncode < 0 or proc.returncode > 127:
            raise RuntimeError(proc.stderr)
        return proc.returncode == 0, split_lines(proc.stdout)

    return tool_git


@dataclass
class Score:
    total: dict[str, int] = field(default_factory=dict)
    correct: dict[str, int] = field(default_factory=dict)
    silent_wrong: int = 0
    spurious_conflicts: int = 0

    def add(self, sc: Scenario, clean: bool, lines: list[str]) -> None:
        self.total[sc.kind] = self.total.get(sc.kind, 0) + 1
        if sc.expected is None:
            ok = not clean
        else:
            ok = clean and lines == sc.expected
            if clean and lines != sc.expected:
                self.silent_wrong += 1
            if not clean:
                self.spurious_conflicts += 1
        self.correct[sc.kind] = self.correct.get(sc.kind, 0) + int(ok)

    def rate(self, kind: str) -> float:
        return self.correct.get(kind, 0) / self.total[kind] if self.total.get(kind) else 0.0


KINDS = ("disjoint", "identical", "overlap")


def run_bench(trials: int = 300, seed: int = 7, use_git: bool = True) -> dict[str, Score]:
    rng = random.Random(seed)
    scenarios = [make_scenario(rng, KINDS[i % len(KINDS)]) for i in range(trials)]
    tools: dict[str, Tool] = {
        "threeway": tool_myers,
        "ablation-raw": tool_myers_raw,
        "baseline-difflib": tool_difflib,
    }
    with tempfile.TemporaryDirectory() as tmp:
        git_tool = make_git_tool(tmp) if use_git else None
        if git_tool is not None:
            tools["git-merge-file"] = git_tool
        scores = {name: Score() for name in tools}
        for sc in scenarios:
            for name, tool in tools.items():
                clean, lines = tool(sc.base, sc.ours, sc.theirs)
                scores[name].add(sc, clean, lines)
    return scores


def format_scores(scores: dict[str, Score]) -> str:
    rows = [f"{'tool':<18} {'disjoint':>9} {'identical':>10} {'overlap':>8} {'silent-wrong':>13} {'spurious-conf':>14}"]
    for name, s in scores.items():
        rows.append(
            f"{name:<18} {s.rate('disjoint'):>9.3f} {s.rate('identical'):>10.3f} {s.rate('overlap'):>8.3f}"
            f" {s.silent_wrong:>13d} {s.spurious_conflicts:>14d}"
        )
    return "\n".join(rows)
