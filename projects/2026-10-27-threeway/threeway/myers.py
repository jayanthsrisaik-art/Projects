"""Myers' O(ND) shortest-edit-script diff.

Everything downstream (unified diffs, three-way merge) consumes *matching
blocks*: ``(a_start, b_start, length)`` triples in increasing order, ending with
the sentinel ``(len(a), len(b), 0)`` -- the same shape that
``difflib.SequenceMatcher.get_matching_blocks`` returns, so the two matchers are
interchangeable.
"""

from __future__ import annotations

import difflib
from typing import Callable, Hashable, Sequence

Block = tuple[int, int, int]
Matcher = Callable[[Sequence[Hashable], Sequence[Hashable]], list[Block]]


def _intern(a: Sequence[Hashable], b: Sequence[Hashable]) -> tuple[list[int], list[int]]:
    """Map items to small ints so the inner loop compares ints, not strings."""
    table: dict[Hashable, int] = {}
    ia = [table.setdefault(x, len(table)) for x in a]
    ib = [table.setdefault(x, len(table)) for x in b]
    return ia, ib


def _myers_pairs(a: Sequence[int], b: Sequence[int]) -> list[tuple[int, int]]:
    """Return matched index pairs of a shortest edit script, in order.

    Greedy forward search over diagonals ``k = x - y`` keeping a snapshot of
    the furthest-reaching frontier for every edit distance ``d``; the path is
    recovered by walking the snapshots backwards. Memory is O(D^2).
    """
    n, m = len(a), len(b)
    if n == 0 or m == 0:
        return []
    max_d = n + m
    offset = max_d + 1
    v = [0] * (2 * max_d + 3)
    trace: list[list[int]] = []
    final_d = -1
    for d in range(max_d + 1):
        # Snapshot covers diagonals -(d+1)..(d+1): what step d reads.
        trace.append(v[offset - d - 1 : offset + d + 2])
        for k in range(-d, d + 1, 2):
            if k == -d or (k != d and v[offset + k - 1] < v[offset + k + 1]):
                x = v[offset + k + 1]  # move down: insertion from b
            else:
                x = v[offset + k - 1] + 1  # move right: deletion from a
            y = x - k
            while x < n and y < m and a[x] == b[y]:
                x += 1
                y += 1
            v[offset + k] = x
            if x >= n and y >= m:
                final_d = d
                break
        if final_d >= 0:
            break

    pairs: list[tuple[int, int]] = []
    x, y = n, m
    for d in range(final_d, -1, -1):
        snap = trace[d]
        base = d + 1
        k = x - y
        if k == -d or (k != d and snap[k - 1 + base] < snap[k + 1 + base]):
            prev_k = k + 1
        else:
            prev_k = k - 1
        prev_x = snap[prev_k + base]
        prev_y = prev_x - prev_k
        while x > prev_x and y > prev_y:
            x -= 1
            y -= 1
            pairs.append((x, y))
        if d > 0:
            x, y = prev_x, prev_y
    pairs.reverse()
    return pairs


def _pairs_to_blocks(pairs: list[tuple[int, int]]) -> list[Block]:
    blocks: list[Block] = []
    for i, j in pairs:
        if blocks:
            bi, bj, ln = blocks[-1]
            if bi + ln == i and bj + ln == j:
                blocks[-1] = (bi, bj, ln + 1)
                continue
        blocks.append((i, j, 1))
    return blocks


def _compact(seq: Sequence[int], changed: list[bool]) -> None:
    """Slide each run of changed lines to a canonical position (in place).

    A changed run ``[s, e)`` can rotate by one line whenever the line just
    outside one end equals the line at the other end: the unchanged line
    keeps its rank among unchanged lines and has the same content, so the
    alignment stays valid and the edit stays minimal. We slide every run up
    as far as possible (absorbing neighbouring runs), then down as far as
    possible, until it stops growing -- the same normalisation git's xdiff
    applies. Without it, two diffs of the same base can mark *different*
    copies of a repeated line (blank lines, ``}``) as changed, and diff3 then
    sees an overlap that is not there.
    """
    n = len(seq)
    i = 0
    while i < n:
        if not changed[i]:
            i += 1
            continue
        s = e = i
        while e < n and changed[e]:
            e += 1
        while True:
            size = e - s
            while s > 0 and seq[s - 1] == seq[e - 1]:
                s -= 1
                e -= 1
                changed[s], changed[e] = True, False
                while s > 0 and changed[s - 1]:
                    s -= 1
            while e < n and seq[s] == seq[e]:
                changed[s], changed[e] = False, True
                s += 1
                e += 1
                while e < n and changed[e]:
                    e += 1
            if e - s == size:
                break
        i = e


def myers_blocks(a: Sequence[Hashable], b: Sequence[Hashable], compact: bool = True) -> list[Block]:
    """Matching blocks of a minimal (shortest edit script) diff of ``a`` -> ``b``.

    With ``compact`` (the default) change runs are slid to canonical
    positions; ``compact=False`` returns Myers' raw path.
    """
    n, m = len(a), len(b)
    ia, ib = _intern(a, b)
    pre = 0
    while pre < n and pre < m and ia[pre] == ib[pre]:
        pre += 1
    suf = 0
    while suf < n - pre and suf < m - pre and ia[n - 1 - suf] == ib[m - 1 - suf]:
        suf += 1
    pairs = [(i + pre, j + pre) for i, j in _myers_pairs(ia[pre : n - suf], ib[pre : m - suf])]
    pairs = [(i, i) for i in range(pre)] + pairs
    pairs += [(n - suf + t, m - suf + t) for t in range(suf)]
    if compact:
        ca, cb = [True] * n, [True] * m
        for i, j in pairs:
            ca[i] = cb[j] = False
        _compact(ia, ca)
        _compact(ib, cb)
        pairs = list(zip((i for i in range(n) if not ca[i]), (j for j in range(m) if not cb[j])))
    blocks = _pairs_to_blocks(pairs)
    blocks.append((n, m, 0))
    return blocks


def myers_raw_blocks(a: Sequence[Hashable], b: Sequence[Hashable]) -> list[Block]:
    """Ablation: Myers' path without slider compaction."""
    return myers_blocks(a, b, compact=False)


def difflib_blocks(a: Sequence[Hashable], b: Sequence[Hashable]) -> list[Block]:
    """Baseline matcher: Python's Ratcliff/Obershelp ``SequenceMatcher``.

    Not minimal, and with ``autojunk`` it ignores very frequent lines in long
    inputs. Used to show how the matcher choice changes merge outcomes.
    """
    sm = difflib.SequenceMatcher(None, list(a), list(b))
    return [tuple(blk) for blk in sm.get_matching_blocks()]  # type: ignore[misc]


def edit_distance(blocks: list[Block], n: int, m: int) -> int:
    """Insertions + deletions implied by a set of matching blocks."""
    matched = sum(ln for _, _, ln in blocks)
    return (n - matched) + (m - matched)


MATCHERS: dict[str, Matcher] = {"myers": myers_blocks, "myers-raw": myers_raw_blocks, "difflib": difflib_blocks}
