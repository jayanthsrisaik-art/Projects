"""Unified diff formatting, parsing and application.

Lines are split on ``\n`` only and keep their terminators, so a final
line without ``\\n`` round-trips via the ``\\ No newline at end of file``
marker that GNU diff, git and ``patch`` all understand.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .myers import Block, Matcher, myers_blocks

NO_EOL = "\\ No newline at end of file\n"


class PatchError(Exception):
    """A patch could not be parsed or does not apply."""


def split_lines(text: str) -> list[str]:
    """Split after each ``\n``. Unlike ``str.splitlines`` this leaves ``\r``,
    form feeds and Unicode separators inside lines, as diff tools do."""
    parts = text.split("\n")
    out = [p + "\n" for p in parts[:-1]]
    if parts[-1]:
        out.append(parts[-1])
    return out


def opcodes(blocks: list[Block]) -> list[tuple[str, int, int, int, int]]:
    """difflib-style ``(tag, i1, i2, j1, j2)`` from matching blocks."""
    ops: list[tuple[str, int, int, int, int]] = []
    i = j = 0
    for bi, bj, ln in blocks:
        if i < bi or j < bj:
            tag = "replace" if i < bi and j < bj else ("delete" if i < bi else "insert")
            ops.append((tag, i, bi, j, bj))
        if ln:
            ops.append(("equal", bi, bi + ln, bj, bj + ln))
        i, j = bi + ln, bj + ln
    return ops


def _range(start: int, length: int) -> str:
    if length == 1:
        return f"{start + 1}"
    if length == 0:
        return f"{start},0"
    return f"{start + 1},{length}"


def _emit(prefix: str, line: str) -> str:
    if line.endswith("\n"):
        return prefix + line
    return prefix + line + "\n" + NO_EOL


def unified_diff(
    a: list[str],
    b: list[str],
    a_name: str = "a",
    b_name: str = "b",
    context: int = 3,
    matcher: Matcher = myers_blocks,
) -> str:
    """Render a unified diff; returns ``""`` when the inputs are identical."""
    ops = opcodes(matcher(a, b))
    changes = [k for k, op in enumerate(ops) if op[0] != "equal"]
    if not changes:
        return ""
    # Group changes whose separating equal run is short enough to share context.
    groups: list[list[int]] = [[changes[0]]]
    for k in changes[1:]:
        prev = groups[-1][-1]
        gap = sum(ops[t][2] - ops[t][1] for t in range(prev + 1, k))
        if gap <= 2 * context:
            groups[-1].append(k)
        else:
            groups.append([k])

    out = [f"--- {a_name}\n", f"+++ {b_name}\n"]
    for group in groups:
        first, last = ops[group[0]], ops[group[-1]]
        i1 = max(0, first[1] - context)
        j1 = first[3] - (first[1] - i1)
        i2 = min(len(a), last[2] + context)
        j2 = last[4] + (i2 - last[2])
        out.append(f"@@ -{_range(i1, i2 - i1)} +{_range(j1, j2 - j1)} @@\n")
        # The hunk spans its changes plus the (clipped) equal runs around them.
        for tag, oi1, oi2, oj1, oj2 in ops[max(0, group[0] - 1) : group[-1] + 2]:
            if tag == "equal":
                out.extend(_emit(" ", a[t]) for t in range(max(oi1, i1), min(oi2, i2)))
            else:
                out.extend(_emit("-", a[t]) for t in range(oi1, oi2))
                out.extend(_emit("+", b[t]) for t in range(oj1, oj2))
    return "".join(out)


@dataclass
class Hunk:
    old_start: int  # 0-based index of the first old line
    old: list[str] = field(default_factory=list)
    new: list[str] = field(default_factory=list)


def _parse_range(text: str) -> tuple[int, int]:
    if "," in text:
        start, length = (int(x) for x in text.split(",", 1))
    else:
        start, length = int(text), 1
    # "-5,0" means "after line 5"; "-5,3" means "starting at line 5".
    return (start if length == 0 else start - 1), length


def parse_patch(text: str) -> list[Hunk]:
    """Parse a single-file unified diff into hunks."""
    lines = split_lines(text)
    hunks: list[Hunk] = []
    k = 0
    while k < len(lines) and not lines[k].startswith("@@"):
        k += 1
    while k < len(lines):
        header = lines[k]
        if not header.startswith("@@"):
            raise PatchError(f"line {k + 1}: expected hunk header, got {header!r}")
        try:
            _, old_r, new_r, _ = header.split(" ", 3)
            old_start, old_len = _parse_range(old_r[1:])
            _, new_len = _parse_range(new_r[1:])
        except ValueError as exc:
            raise PatchError(f"line {k + 1}: malformed hunk header {header!r}") from exc
        hunk = Hunk(old_start)
        k += 1
        last: list[list[str]] = []
        while k < len(lines) and (len(hunk.old) < old_len or len(hunk.new) < new_len or lines[k] == NO_EOL):
            line = lines[k]
            tag, body = line[:1], line[1:]
            if line == NO_EOL:
                for side in last:
                    side[-1] = side[-1][:-1] if side[-1].endswith("\n") else side[-1]
            elif tag == " ":
                hunk.old.append(body)
                hunk.new.append(body)
                last = [hunk.old, hunk.new]
            elif tag == "-":
                hunk.old.append(body)
                last = [hunk.old]
            elif tag == "+":
                hunk.new.append(body)
                last = [hunk.new]
            elif line in ("\n", ""):  # some tools drop the space on empty context lines
                hunk.old.append("\n")
                hunk.new.append("\n")
                last = [hunk.old, hunk.new]
            else:
                raise PatchError(f"line {k + 1}: unexpected {line!r} inside hunk")
            k += 1
        if len(hunk.old) != old_len or len(hunk.new) != new_len:
            raise PatchError(f"hunk at line {old_start + 1}: truncated (expected -{old_len} +{new_len})")
        hunks.append(hunk)
        while k < len(lines) and not lines[k].startswith("@@"):
            k += 1
    return hunks


@dataclass
class ApplyResult:
    lines: list[str]
    offsets: list[int]  # per hunk: actual position minus position in the header


def apply_patch(original: list[str], patch: str) -> ApplyResult:
    """Apply a unified diff. Hunks may land at an offset (nearest match wins).

    There is no fuzz: every context and removed line must match exactly, which
    is the safe default for code. Hunks must apply in order without overlap.
    """
    hunks = parse_patch(patch)
    out: list[str] = []
    pos = 0  # next unconsumed line of ``original``
    drift = 0
    offsets: list[int] = []
    for idx, hunk in enumerate(hunks):
        want = hunk.old_start + drift
        size = len(hunk.old)
        found = -1
        for delta in range(len(original) + 1):
            for cand in (want - delta, want + delta) if delta else (want,):
                if pos <= cand <= len(original) - size and original[cand : cand + size] == hunk.old:
                    found = cand
                    break
            if found >= 0:
                break
        if found < 0:
            raise PatchError(f"hunk #{idx + 1} (line {hunk.old_start + 1}) does not apply")
        offsets.append(found - hunk.old_start)
        drift = found - hunk.old_start
        out.extend(original[pos:found])
        out.extend(hunk.new)
        pos = found + size
    out.extend(original[pos:])
    return ApplyResult(out, offsets)
