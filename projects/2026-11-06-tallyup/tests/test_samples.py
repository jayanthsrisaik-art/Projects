"""The committed sample outputs are regenerated and compared exactly."""
import contextlib
import io
import json
import sys
from pathlib import Path

from tallyup import bench
from tallyup.demo import run

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


def test_scenario_output_matches_expected(tmp_path):
    lines = run(json.loads((SAMPLES / "scenario.json").read_text()), str(tmp_path / "demo.db"))
    assert "\n".join(lines) + "\n" == (SAMPLES / "scenario_expected.txt").read_text()


def test_bench_output_matches_expected(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["bench", "--no-timing"])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        bench.main()
    assert buf.getvalue() == (SAMPLES / "bench_expected.md").read_text()
