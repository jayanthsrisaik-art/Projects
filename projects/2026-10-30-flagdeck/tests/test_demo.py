import importlib.util

from conftest import ROOT


def test_demo_output_matches_checked_in_sample():
    spec = importlib.util.spec_from_file_location("demo", ROOT / "scripts/demo.py")
    demo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo)
    assert demo.run() == (ROOT / "samples/demo_output.txt").read_text(encoding="utf-8")
