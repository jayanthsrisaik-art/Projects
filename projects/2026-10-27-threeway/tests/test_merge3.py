import random
import shutil
import subprocess
from pathlib import Path

import pytest

from threeway.merge3 import merge3
from threeway.myers import difflib_blocks
from threeway.unified import split_lines

SAMPLES = Path(__file__).resolve().parent.parent / "samples"
L = ("ours", "base", "theirs")


def lines(s):
    return [f"{x}\n" for x in s.split()] if s else []


def read(name):
    return split_lines((SAMPLES / name).read_text())


def rand_lines(rng, n):
    return [rng.choice(["a\n", "b\n", "c\n", "\n"]) for _ in range(n)]


def mutate(rng, base):
    out = list(base)
    for _ in range(rng.randint(0, 3)):
        k = rng.randint(0, len(out))
        if rng.random() < 0.5 or not out or k == len(out):
            out.insert(k, f"n{rng.randint(0, 99)}\n")
        else:
            del out[k]
    return out


def test_properties_on_random_files():
    rng = random.Random(3)
    for _ in range(500):
        base = rand_lines(rng, rng.randint(0, 20))
        x = mutate(rng, base)
        for matcher in (None, difflib_blocks):
            kw = {"matcher": matcher} if matcher else {}
            assert merge3(base, x, base, **kw).lines == x  # only ours changed
            assert merge3(base, base, x, **kw).lines == x  # only theirs changed
            assert merge3(base, x, x, **kw).lines == x  # identical changes
            assert merge3(base, base, base, **kw).lines == base


def test_disjoint_edits_merge_cleanly():
    base = lines("a b c d e f g")
    ours = lines("A b c d e f g")
    theirs = lines("a b c d e f G")
    r = merge3(base, ours, theirs)
    assert r.clean and r.lines == lines("A b c d e f G")


def test_adjacent_edits_conflict_like_diff3():
    base = lines("a b c d")
    r = merge3(base, lines("a B c d"), lines("a b C d"))
    assert r.conflicts == 1


def test_delete_vs_modify_conflicts():
    base = lines("a b c")
    r = merge3(base, lines("a c"), lines("a B c"), style="diff3", labels=L)
    assert r.text == "a\n<<<<<<< ours\n||||||| base\nb\n=======\nB\n>>>>>>> theirs\nc\n"


def test_both_insert_same_point_conflicts_unless_equal():
    base = lines("a b")
    assert merge3(base, lines("a x b"), lines("a y b")).conflicts == 1
    assert merge3(base, lines("a x b"), lines("a x b")).clean


def test_merge_style_refines_shared_lines_out_of_conflict():
    base = lines("a q b")
    ours = lines("a x1 shared y1 b")
    theirs = lines("a x2 shared y2 b")
    r = merge3(base, ours, theirs, labels=L)
    assert r.conflicts == 2
    assert "shared\n" not in r.text.split("<<<<<<<")[1].split(">>>>>>>")[0]
    unrefined = merge3(base, ours, theirs, style="diff3", labels=L)
    assert unrefined.conflicts == 1


def test_refinement_keeps_punctuation_only_runs_inside_conflict():
    base = lines("a q b")
    ours = lines("a x1 } y1 b")
    theirs = lines("a x2 } y2 b")
    assert merge3(base, ours, theirs).conflicts == 1


def test_zdiff3_hoists_common_prefix_and_suffix():
    base = lines("a q b")
    ours = lines("a head mine tail b")
    theirs = lines("a head yours tail b")
    r = merge3(base, ours, theirs, style="zdiff3", labels=L)
    assert r.text == (
        "a\nhead\n<<<<<<< ours\nmine\n||||||| base\nq\n=======\nyours\n>>>>>>> theirs\ntail\nb\n"
    )


def test_markers_start_on_their_own_line_without_trailing_newline():
    base = ["a\n", "b"]
    r = merge3(base, ["a\n", "x"], ["a\n", "y"], labels=L)
    assert r.text == "a\n<<<<<<< ours\nx\n=======\ny\n>>>>>>> theirs\n"


def test_unknown_style_rejected():
    with pytest.raises(ValueError):
        merge3([], [], [], style="nope")


def test_sample_clean_merge_golden():
    r = merge3(read("base.py"), read("ours.py"), read("theirs.py"), labels=L)
    assert r.clean
    assert r.text == (SAMPLES / "expected" / "merged.py").read_text()


@pytest.mark.parametrize("style", ["merge", "diff3", "zdiff3"])
def test_sample_conflict_golden(style):
    r = merge3(read("base.py"), read("ours.py"), read("theirs_conflict.py"), style=style, labels=L)
    assert r.conflicts == 1
    assert r.text == (SAMPLES / "expected" / f"conflict.{style}.txt").read_text()


GIT = shutil.which("git")


@pytest.mark.skipif(GIT is None, reason="git not installed")
@pytest.mark.parametrize("style,flag", [("merge", None), ("diff3", "--diff3"), ("zdiff3", "--zdiff3")])
def test_samples_byte_identical_to_git_merge_file(tmp_path, style, flag):
    for theirs in ("theirs.py", "theirs_conflict.py"):
        cmd = [GIT, "merge-file", "-p", "-L", "ours", "-L", "base", "-L", "theirs"]
        cmd += [flag] if flag else []
        cmd += [str(SAMPLES / n) for n in ("ours.py", "base.py", theirs)]
        git = subprocess.run(cmd, capture_output=True, text=True)
        r = merge3(read("base.py"), read("ours.py"), read(theirs), style=style, labels=L)
        assert git.returncode == r.conflicts
        assert git.stdout == r.text
