import random

import pytest

from threeway.merge3 import merge3
from threeway.myers import difflib_blocks, edit_distance, myers_blocks, myers_raw_blocks


def lcs_len(a, b):
    prev = [0] * (len(b) + 1)
    for x in a:
        cur = [0]
        for j, y in enumerate(b):
            cur.append(prev[j] + 1 if x == y else max(prev[j + 1], cur[j]))
        prev = cur
    return prev[-1]


def assert_valid(blocks, a, b):
    assert blocks[-1] == (len(a), len(b), 0)
    i = j = 0
    for bi, bj, ln in blocks[:-1]:
        assert ln > 0 and bi >= i and bj >= j
        assert list(a[bi : bi + ln]) == list(b[bj : bj + ln])
        i, j = bi + ln, bj + ln


@pytest.mark.parametrize("matcher", [myers_blocks, myers_raw_blocks])
def test_minimal_against_lcs_oracle(matcher):
    rng = random.Random(0)
    for _ in range(400):
        a = [rng.choice("abc") for _ in range(rng.randint(0, 12))]
        b = [rng.choice("abc") for _ in range(rng.randint(0, 12))]
        blocks = matcher(a, b)
        assert_valid(blocks, a, b)
        assert sum(ln for _, _, ln in blocks) == lcs_len(a, b)
        assert edit_distance(blocks, len(a), len(b)) == len(a) + len(b) - 2 * lcs_len(a, b)


def test_difflib_baseline_is_valid_but_not_always_minimal():
    rng = random.Random(1)
    worse = 0
    for _ in range(300):
        a = [rng.choice("ab") for _ in range(10)]
        b = [rng.choice("ab") for _ in range(10)]
        blocks = difflib_blocks(a, b)
        assert_valid(blocks, a, b)
        worse += sum(ln for _, _, ln in blocks) < lcs_len(a, b)
    assert worse > 0  # Ratcliff/Obershelp is greedy, not an LCS


@pytest.mark.parametrize(
    "a,b,expected",
    [
        ("", "", [(0, 0, 0)]),
        ("abc", "", [(3, 0, 0)]),
        ("", "abc", [(0, 3, 0)]),
        ("abc", "abc", [(0, 0, 3), (3, 3, 0)]),
        ("abcabba", "cbabac", None),  # Myers' paper example: D = 5
    ],
)
def test_edge_cases(a, b, expected):
    blocks = myers_blocks(list(a), list(b))
    assert_valid(blocks, a, b)
    if expected is not None:
        assert blocks == expected
    else:
        assert edit_distance(blocks, len(a), len(b)) == 5


def test_compaction_makes_both_diffs_agree_on_repeated_lines():
    # base "bbb"; both sides delete one b, ours also inserts O, theirs prepends T.
    # Raw Myers marks a different b as deleted in each diff, so diff3 sees an
    # overlap; after compaction both diffs delete the *last* b and agree.
    base, ours, theirs = list("bbb"), list("bOb"), list("Tbb")
    assert myers_raw_blocks(base, ours) != myers_blocks(base, ours)
    raw = merge3(base, ours, theirs, matcher=myers_raw_blocks)
    canon = merge3(base, ours, theirs)
    assert not raw.clean
    assert canon.clean and canon.lines == list("TbOb")


def test_compaction_keeps_minimality_on_random_inputs():
    rng = random.Random(2)
    for _ in range(300):
        a = [rng.choice(["\n", "}\n", "x\n"]) for _ in range(rng.randint(0, 15))]
        b = [rng.choice(["\n", "}\n", "x\n"]) for _ in range(rng.randint(0, 15))]
        blocks = myers_blocks(a, b)
        assert_valid(blocks, a, b)
        assert sum(ln for _, _, ln in blocks) == lcs_len(a, b)
