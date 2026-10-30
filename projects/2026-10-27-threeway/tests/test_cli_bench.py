from pathlib import Path

from threeway.bench import KINDS, make_scenario, run_bench
from threeway.cli import main

import random

SAMPLES = Path(__file__).resolve().parent.parent / "samples"
S = str(SAMPLES)


def test_merge_exit_code_is_conflict_count(capsys):
    assert main(["merge", f"{S}/ours.py", f"{S}/base.py", f"{S}/theirs.py"]) == 0
    assert capsys.readouterr().out == (SAMPLES / "expected" / "merged.py").read_text()
    rc = main(["merge", "-L", "ours", "-L", "base", "-L", "theirs", "--style", "diff3",
               f"{S}/ours.py", f"{S}/base.py", f"{S}/theirs_conflict.py"])
    out = capsys.readouterr()
    assert rc == 1 and "1 conflict(s)" in out.err
    assert out.out == (SAMPLES / "expected" / "conflict.diff3.txt").read_text()


def test_diff_then_apply_via_cli(tmp_path, capsys):
    assert main(["diff", f"{S}/base.py", f"{S}/base.py"]) == 0
    assert capsys.readouterr().out == ""
    assert main(["diff", f"{S}/base.py", f"{S}/theirs.py"]) == 1
    patch = tmp_path / "t.patch"
    patch.write_text(capsys.readouterr().out)
    out = tmp_path / "out.py"
    assert main(["apply", f"{S}/base.py", str(patch), "-o", str(out)]) == 0
    assert out.read_text() == (SAMPLES / "theirs.py").read_text()


def test_apply_failure_exit_code(tmp_path, capsys):
    patch = tmp_path / "p.patch"
    patch.write_text("--- a\n+++ b\n@@ -1 +1 @@\n-nope\n+yes\n")
    assert main(["apply", f"{S}/base.py", str(patch)]) == 2
    assert "does not apply" in capsys.readouterr().err


def test_overlap_scenarios_really_overlap():
    rng = random.Random(11)
    for _ in range(200):
        sc = make_scenario(rng, "overlap")
        assert sc.expected is None and sc.ours != sc.theirs


def test_bench_is_deterministic_and_never_misses_a_true_conflict():
    a = run_bench(trials=60, seed=3, use_git=False)
    b = run_bench(trials=60, seed=3, use_git=False)
    for name in a:
        assert a[name].correct == b[name].correct
        assert a[name].total == {k: 20 for k in KINDS}
        # Both sides rewrite a shared base line: no valid diff3 can merge that.
        assert a[name].rate("overlap") == 1.0
