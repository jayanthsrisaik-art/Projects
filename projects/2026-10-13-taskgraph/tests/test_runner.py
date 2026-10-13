import time

from taskgraph.config import load
from taskgraph.runner import CACHED, FAILED, RAN, SKIPPED, run


def test_chain_runs_then_caches(make_project, tmp_path):
    f = make_project("""
        [task.a]
        cmd = ["{py}", "-c", "open('a.out','a').write('x')"]
        outputs = ["a.out"]
        [task.b]
        cmd = ["{py}", "-c", "open('b.out','a').write('x')"]
        deps = ["a"]
        outputs = ["b.out"]
    """)
    p = load(f)
    r1 = run(p, ["b"])
    assert {k: v.status for k, v in r1.items()} == {"a": RAN, "b": RAN}
    r2 = run(load(f), ["b"])
    assert {v.status for v in r2.values()} == {CACHED}
    assert (tmp_path / "a.out").read_text() == "x"  # not re-executed


def test_input_change_invalidates_task_and_downstream_only(make_project, tmp_path):
    f = make_project("""
        [task.a]
        cmd = ["{py}", "-c", "open('a.out','w').write(open('in.txt').read())"]
        inputs = ["in.txt"]
        outputs = ["a.out"]
        [task.b]
        cmd = ["{py}", "-c", "open('b.out','w').write(open('a.out').read())"]
        deps = ["a"]
        outputs = ["b.out"]
        [task.c]
        cmd = ["{py}", "-c", "pass"]
    """)
    (tmp_path / "in.txt").write_text("one")
    run(load(f))
    (tmp_path / "in.txt").write_text("two")
    r = run(load(f))
    assert r["a"].status == RAN and r["b"].status == RAN and r["c"].status == CACHED
    assert (tmp_path / "b.out").read_text() == "two"


def test_touch_without_content_change_stays_cached(make_project, tmp_path):
    f = make_project("""
        [task.a]
        cmd = ["{py}", "-c", "pass"]
        inputs = ["in.txt"]
    """)
    (tmp_path / "in.txt").write_text("same")
    run(load(f))
    (tmp_path / "in.txt").write_text("same")  # rewritten, new mtime
    assert run(load(f))["a"].status == CACHED


def test_missing_output_forces_rerun(make_project, tmp_path):
    f = make_project("""
        [task.a]
        cmd = ["{py}", "-c", "open('o.txt','w').write('1')"]
        outputs = ["o.txt"]
    """)
    run(load(f))
    (tmp_path / "o.txt").unlink()
    assert run(load(f))["a"].status == RAN


def test_failure_skips_dependents_but_not_independents(make_project):
    f = make_project("""
        [task.bad]
        cmd = ["{py}", "-c", "import sys; print('boom'); sys.exit(3)"]
        [task.child]
        cmd = ["{py}", "-c", "pass"]
        deps = ["bad"]
        [task.grandchild]
        cmd = ["{py}", "-c", "pass"]
        deps = ["child"]
        [task.free]
        cmd = ["{py}", "-c", "pass"]
    """)
    r = run(load(f))
    assert r["bad"].status == FAILED and "boom" in r["bad"].output and "3" in r["bad"].reason
    assert r["child"].status == SKIPPED and r["grandchild"].status == SKIPPED
    assert r["free"].status == RAN


def test_failed_task_is_not_cached(make_project):
    f = make_project("""
        [task.bad]
        cmd = ["{py}", "-c", "import sys; sys.exit(1)"]
    """)
    run(load(f))
    assert run(load(f))["bad"].status == FAILED


def test_missing_executable_is_a_failure(make_project):
    f = make_project("""
        [task.a]
        cmd = ["definitely-not-a-real-binary-xyz"]
    """)
    r = run(load(f))["a"]
    assert r.status == FAILED and "cannot execute" in r.output


def test_force_and_dry_run(make_project, tmp_path):
    f = make_project("""
        [task.a]
        cmd = ["{py}", "-c", "open('n','a').write('x')"]
    """)
    r = run(load(f), dry_run=True)
    assert r["a"].status == RAN and not (tmp_path / "n").exists()
    assert not (tmp_path / ".taskgraph").exists()
    run(load(f))
    run(load(f), force=True)
    assert (tmp_path / "n").read_text() == "xx"


def test_independent_tasks_run_in_parallel(make_project):
    f = make_project("""
        [task.a]
        cmd = ["{py}", "-c", "import time; time.sleep(1)"]
        [task.b]
        cmd = ["{py}", "-c", "import time; time.sleep(1)"]
        [task.c]
        cmd = ["{py}", "-c", "import time; time.sleep(1)"]
    """)
    t = time.monotonic()
    run(load(f), jobs=3)
    assert time.monotonic() - t < 2.5  # serial would take >= 3s


def test_dependency_order_respected(make_project, tmp_path):
    f = make_project("""
        [task.a]
        cmd = ["{py}", "-c", "import time; time.sleep(0.3); open('log','a').write('a')"]
        [task.b]
        cmd = ["{py}", "-c", "open('log','a').write('b')"]
        deps = ["a"]
    """)
    run(load(f), jobs=4)
    assert (tmp_path / "log").read_text() == "ab"
