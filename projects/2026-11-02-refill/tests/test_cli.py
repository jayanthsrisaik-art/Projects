import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EX = ROOT / "examples"


def run(*args):
    return subprocess.run([sys.executable, "-m", "refill", *args], cwd=ROOT,
                          capture_output=True, text=True)


def test_lint_ok():
    r = run("lint", "examples/pipeline.json")
    assert r.returncode == 0 and r.stdout.startswith("OK: 9 tables")


def test_plan_text_matches_golden():
    r = run("plan", "examples/pipeline.json", "-c", "raw_orders:2026-10-05..2026-10-06",
            "-e", "daily_share_of_month:2026-10-02", "-e", "region_sales_7d:2026-10-12")
    assert r.returncode == 0, r.stderr
    assert r.stdout == (EX / "expected" / "plan_late_orders.txt").read_text()


def test_plan_json_matches_golden():
    r = run("plan", "examples/pipeline.json", "-c", "monthly_region_sales:2026-09", "--json")
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == json.loads((EX / "expected" / "plan_logic_change.json").read_text())


def test_bad_change_exits_2():
    r = run("plan", "examples/pipeline.json", "-c", "raw_orders:2027-01-01")
    assert r.returncode == 2 and "not a day partition" in r.stderr


def test_demo_matches_golden_report():
    r = run("demo", "examples/pipeline.json", "--json")
    assert r.returncode == 0, r.stderr
    got = json.loads(r.stdout)
    assert got == json.loads((EX / "expected" / "demo_report.json").read_text())
    for sc in got["scenarios"]:
        assert sc["strategies"]["refill"]["wrong_partitions"] == 0
        assert sc["strategies"]["refill_cutoff"]["wrong_partitions"] == 0
        assert sc["strategies"]["full_rebuild"]["wrong_partitions"] == 0
