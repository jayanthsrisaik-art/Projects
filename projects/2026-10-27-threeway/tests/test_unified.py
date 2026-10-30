import random
import shutil
import subprocess
from pathlib import Path

import pytest

from threeway.myers import difflib_blocks
from threeway.unified import PatchError, apply_patch, parse_patch, split_lines, unified_diff

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


def rand_file(rng, n):
    return [rng.choice(["a\n", "b\n", "c\n", "\n", "}\n"]) for _ in range(n)]


def mutate(rng, lines):
    out = list(lines)
    for _ in range(rng.randint(0, 4)):
        op = rng.choice("idr")
        k = rng.randint(0, len(out))
        if op == "i":
            out.insert(k, f"new{rng.randint(0, 9)}\n")
        elif out and k < len(out):
            if op == "d":
                del out[k]
            else:
                out[k] = f"chg{rng.randint(0, 9)}\n"
    return out


@pytest.mark.parametrize("context", [0, 1, 3])
def test_roundtrip_random(context):
    rng = random.Random(context)
    for _ in range(300):
        a = rand_file(rng, rng.randint(0, 30))
        b = mutate(rng, a)
        patch = unified_diff(a, b, context=context)
        assert (patch == "") == (a == b)
        assert apply_patch(a, patch).lines == b


def test_roundtrip_with_difflib_matcher():
    rng = random.Random(9)
    for _ in range(100):
        a = rand_file(rng, 20)
        b = mutate(rng, a)
        assert apply_patch(a, unified_diff(a, b, matcher=difflib_blocks)).lines == b


def test_golden_sample_patch():
    base = split_lines((SAMPLES / "base.py").read_text())
    ours = split_lines((SAMPLES / "ours.py").read_text())
    expected = (SAMPLES / "expected" / "ours.patch").read_text()
    assert unified_diff(base, ours, "samples/base.py", "samples/ours.py") == expected


def test_missing_newline_at_eof_roundtrips():
    a = ["one\n", "two"]
    b = ["one\n", "two\n", "three"]
    patch = unified_diff(a, b)
    assert patch.count("\\ No newline at end of file") == 2
    assert apply_patch(a, patch).lines == b
    assert apply_patch(b, unified_diff(b, a)).lines == a


def test_hunk_header_ranges():
    patch = unified_diff(["a\n"], ["a\n", "b\n"], context=0)
    assert "@@ -1,0 +2 @@" in patch  # insertion after line 1
    patch = unified_diff(["a\n", "b\n"], ["a\n"], context=0)
    assert "@@ -2 +1,0 @@" in patch


def test_apply_at_offset():
    a = [f"{i}\n" for i in range(20)]
    b = list(a)
    b[10] = "ten\n"
    patch = unified_diff(a, b)
    shifted = ["header\n"] * 4 + a
    result = apply_patch(shifted, patch)
    assert result.offsets == [4]
    assert result.lines == ["header\n"] * 4 + b


def test_apply_rejects_mismatched_context():
    a = [f"{i}\n" for i in range(10)]
    b = list(a)
    b[5] = "five\n"
    other = [f"x{i}\n" for i in range(10)]
    with pytest.raises(PatchError):
        apply_patch(other, unified_diff(a, b))


def test_parse_rejects_truncated_hunk():
    with pytest.raises(PatchError):
        parse_patch("--- a\n+++ b\n@@ -1,3 +1,3 @@\n a\n-b\n")


@pytest.mark.skipif(shutil.which("patch") is None, reason="GNU patch not installed")
def test_gnu_patch_accepts_our_output(tmp_path):
    rng = random.Random(5)
    for k in range(20):
        a = rand_file(rng, 25)
        b = mutate(rng, a)
        src = tmp_path / f"f{k}.txt"
        src.write_text("".join(a))
        (tmp_path / "p.diff").write_text(unified_diff(a, b))
        subprocess.run(["patch", "-s", str(src), str(tmp_path / "p.diff")], check=True)
        assert src.read_text() == "".join(b)


def test_split_lines_only_on_newline():
    text = "a\r\nb\x0cc d\ne"
    assert split_lines(text) == ["a\r\n", "b\x0cc d\n", "e"]
    assert "".join(split_lines(text)) == text
    assert split_lines("") == [] and split_lines("x\n") == ["x\n"]


def test_crlf_and_form_feed_roundtrip():
    a = split_lines("one\r\ntwo\r\n\x0c\r\nthree\r\n")
    b = split_lines("one\r\n2\r\n\x0c\r\nthree\r\n")
    patch = unified_diff(a, b)
    assert apply_patch(a, patch).lines == b
