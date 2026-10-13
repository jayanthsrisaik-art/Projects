import shutil
from pathlib import Path

from taskgraph.cli import main

DEMO = Path(__file__).resolve().parent.parent / "samples" / "demo"


def demo(tmp_path):
    d = tmp_path / "demo"
    shutil.copytree(DEMO, d)
    return str(d / "tasks.toml")


def test_graph_matches_sample(tmp_path, capsys):
    assert main(["-f", demo(tmp_path), "graph"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "wave 0: banner, sort", "wave 1: stats", "wave 2: all"]


def test_demo_run_twice(tmp_path, capsys):
    f = demo(tmp_path)
    assert main(["-f", f, "run"]) == 0
    first = capsys.readouterr().out
    assert first.count("[    ran]") == 4
    assert (tmp_path / "demo/build/sorted.txt").read_text() == "apple\napple\nfig\npear\n"
    assert main(["-f", f, "run"]) == 0
    assert capsys.readouterr().out.count("[ cached]") == 4


def test_editing_input_reruns_chain(tmp_path, capsys):
    f = demo(tmp_path)
    main(["-f", f, "run"])
    capsys.readouterr()
    (tmp_path / "demo/words.txt").write_text("zebra\n")
    main(["-f", f, "run"])
    out = capsys.readouterr().out
    assert "[ cached] banner" in out
    for name in ("sort", "stats", "all"):
        assert f"[    ran] {name}" in out


def test_errors_return_exit_codes(tmp_path, capsys):
    assert main(["-f", str(tmp_path / "nope.toml"), "list"]) == 2
    f = tmp_path / "t.toml"
    f.write_text('[task.a]\ncmd="x"\ndeps=["a"]\n')
    assert main(["-f", str(f), "run"]) == 2
    assert "cycle" in capsys.readouterr().err
    f.write_text('[task.a]\ncmd=["definitely-missing-binary"]\n')
    assert main(["-f", str(f), "run"]) == 1
