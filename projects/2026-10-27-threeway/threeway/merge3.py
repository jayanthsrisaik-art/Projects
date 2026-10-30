"""diff3-style three-way merge.

The algorithm follows Khanna, Kuber & Pierce, *A Formal Investigation of
Diff3* (2007):

1. Diff base->ours and base->theirs.
2. A base line is *stable* when both diffs match it. Maximal runs of stable
   lines are sync regions; they are copied through unchanged.
3. Each unstable chunk between two sync regions is classified by comparing
   its three versions: unchanged, changed on one side only (take that side),
   changed identically on both (take either), or a conflict.

Conflicts can then be presented in three styles, mirroring git:

* ``merge``  -- ours/theirs only. Conflicts are *refined*: ours and theirs are
  diffed against each other and lines they share are moved out of the
  conflict (git's "zealous" merge). Shared runs with no alphanumeric content
  (blank lines, lone braces) are left inside so a conflict is not shredded.
* ``diff3``  -- ours/base/theirs, unrefined.
* ``zdiff3`` -- ours/base/theirs with the common prefix and suffix of ours and
  theirs hoisted out of the conflict.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .myers import Block, Matcher, myers_blocks

STYLES = ("merge", "diff3", "zdiff3")


@dataclass
class Chunk:
    kind: str  # unchanged | ours | theirs | same | conflict
    base: list[str] = field(default_factory=list)
    ours: list[str] = field(default_factory=list)
    theirs: list[str] = field(default_factory=list)

    def resolved(self) -> list[str]:
        if self.kind in ("unchanged",):
            return self.base
        if self.kind in ("ours", "same"):
            return self.ours
        if self.kind == "theirs":
            return self.theirs
        raise ValueError("a conflict has no single resolution")


def sync_regions(a_blocks: list[Block], b_blocks: list[Block], n_base: int, n_a: int, n_b: int):
    """Base ranges matched by *both* diffs, with their positions in a and b.

    Yields ``(base_lo, base_hi, a_lo, a_hi, b_lo, b_hi)`` and a final empty
    sentinel at the end of all three files.
    """
    regions = []
    ia = ib = 0
    while ia < len(a_blocks) and ib < len(b_blocks):
        a_base, a_pos, a_len = a_blocks[ia]
        b_base, b_pos, b_len = b_blocks[ib]
        lo = max(a_base, b_base)
        hi = min(a_base + a_len, b_base + b_len)
        if lo < hi:
            a_lo = a_pos + (lo - a_base)
            b_lo = b_pos + (lo - b_base)
            regions.append((lo, hi, a_lo, a_lo + hi - lo, b_lo, b_lo + hi - lo))
        if a_base + a_len < b_base + b_len:
            ia += 1
        else:
            ib += 1
    regions.append((n_base, n_base, n_a, n_a, n_b, n_b))
    return regions


def merge_chunks(base: list[str], ours: list[str], theirs: list[str], matcher: Matcher = myers_blocks) -> list[Chunk]:
    regions = sync_regions(matcher(base, ours), matcher(base, theirs), len(base), len(ours), len(theirs))
    chunks: list[Chunk] = []
    z = a = b = 0
    for z_lo, z_hi, a_lo, a_hi, b_lo, b_hi in regions:
        cb, ca, ct = base[z:z_lo], ours[a:a_lo], theirs[b:b_lo]
        if cb or ca or ct:
            if ca == ct:
                kind = "unchanged" if ca == cb else "same"
            elif ca == cb:
                kind = "theirs"
            elif ct == cb:
                kind = "ours"
            else:
                kind = "conflict"
            chunks.append(Chunk(kind, cb, ca, ct))
        if z_hi > z_lo:
            stable = base[z_lo:z_hi]
            chunks.append(Chunk("unchanged", stable, stable, stable))
        z, a, b = z_hi, a_hi, b_hi
    return chunks


def _has_alnum(lines: list[str]) -> bool:
    return any(ch.isalnum() for line in lines for ch in line)


def _refine(chunk: Chunk, matcher: Matcher) -> list[Chunk]:
    """Split a conflict on lines that ours and theirs share (zealous merge)."""
    ours, theirs = chunk.ours, chunk.theirs
    pieces: list[Chunk] = []
    i = j = 0
    for bi, bj, ln in matcher(ours, theirs):
        if i < bi or j < bj:
            pieces.append(Chunk("conflict", [], ours[i:bi], theirs[j:bj]))
        if ln:
            common = ours[bi : bi + ln]
            pieces.append(Chunk("same", [], common, common))
        i, j = bi + ln, bj + ln
    # Re-absorb shared runs that carry no real content and sit between conflicts.
    merged: list[Chunk] = []
    for k, piece in enumerate(pieces):
        between = 0 < k < len(pieces) - 1 and pieces[k - 1].kind == "conflict" and pieces[k + 1].kind == "conflict"
        if piece.kind == "same" and between and not _has_alnum(piece.ours):
            piece = Chunk("conflict", [], piece.ours, piece.theirs)
        if merged and piece.kind == merged[-1].kind == "conflict":
            prev = merged[-1]
            merged[-1] = Chunk("conflict", [], prev.ours + piece.ours, prev.theirs + piece.theirs)
        else:
            merged.append(piece)
    if len(merged) == 1:
        return [chunk]  # nothing shared: keep the base for diff3 consumers
    return merged


def _trim(chunk: Chunk) -> list[Chunk]:
    """Hoist the common prefix/suffix of ours and theirs out of a conflict."""
    ours, theirs = chunk.ours, chunk.theirs
    p = 0
    while p < len(ours) and p < len(theirs) and ours[p] == theirs[p]:
        p += 1
    s = 0
    while s < len(ours) - p and s < len(theirs) - p and ours[-1 - s] == theirs[-1 - s]:
        s += 1
    out: list[Chunk] = []
    if p:
        out.append(Chunk("same", [], ours[:p], ours[:p]))
    out.append(Chunk("conflict", chunk.base, ours[p : len(ours) - s], theirs[p : len(theirs) - s]))
    if s:
        out.append(Chunk("same", [], ours[len(ours) - s :], ours[len(ours) - s :]))
    return out


@dataclass
class MergeResult:
    lines: list[str]
    conflicts: int
    chunks: list[Chunk]

    @property
    def clean(self) -> bool:
        return self.conflicts == 0

    @property
    def text(self) -> str:
        return "".join(self.lines)


def _terminated(lines: list[str]) -> list[str]:
    """Conflict markers must start on a fresh line."""
    if lines and not lines[-1].endswith("\n"):
        return lines[:-1] + [lines[-1] + "\n"]
    return lines


def merge3(
    base: list[str],
    ours: list[str],
    theirs: list[str],
    style: str = "merge",
    labels: tuple[str, str, str] = ("ours", "base", "theirs"),
    matcher: Matcher = myers_blocks,
) -> MergeResult:
    if style not in STYLES:
        raise ValueError(f"unknown conflict style {style!r}; expected one of {STYLES}")
    chunks: list[Chunk] = []
    for chunk in merge_chunks(base, ours, theirs, matcher):
        if chunk.kind != "conflict":
            chunks.append(chunk)
        elif style == "merge":
            chunks.extend(_refine(chunk, matcher))
        elif style == "zdiff3":
            chunks.extend(_trim(chunk))
        else:
            chunks.append(chunk)

    l_ours, l_base, l_theirs = labels
    out: list[str] = []
    conflicts = 0
    for chunk in chunks:
        if chunk.kind != "conflict":
            out.extend(chunk.resolved())
            continue
        conflicts += 1
        if out:
            out = _terminated(out)
        out.append(f"<<<<<<< {l_ours}\n")
        out.extend(_terminated(chunk.ours))
        if style != "merge":
            out.append(f"||||||| {l_base}\n")
            out.extend(_terminated(chunk.base))
        out.append("=======\n")
        out.extend(_terminated(chunk.theirs))
        out.append(f">>>>>>> {l_theirs}\n")
    return MergeResult(out, conflicts, chunks)
