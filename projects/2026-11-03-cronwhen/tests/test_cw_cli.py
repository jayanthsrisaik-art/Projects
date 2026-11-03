import io
import json
from pathlib import Path

import pytest

from cronwhen.cli import main
from cronwhen.crontab import JobFileError, parse_jobs

EX = Path(__file__).resolve().parents[1] / "examples"


def run(*argv):
    buf = io.StringIO()
    code = main(list(argv), out=buf)
    return code, buf.getvalue()


def test_dst_report_matches_golden_file():
    code, out = run("dst-report", str(EX / "jobs.cron"), "--start", "2026-01-01", "--end", "2027-01-01")
    assert code == 0
    assert out == (EX / "expected" / "dst_report_2026.txt").read_text()


@pytest.mark.parametrize("golden,argv", [
    ("next_backup_spring.txt", ["next", "30 2 * * *", "--tz", "America/New_York",
                                "--after", "2026-03-07T00:00", "-n", "4", "--all"]),
    ("next_interval_fall.txt", ["next", "*/30 * * * *", "--tz", "America/New_York",
                                "--after", "2026-11-01T00:00", "--until", "2026-11-01T03:00", "--all"]),
])
def test_next_matches_golden_files(golden, argv):
    code, out = run(*argv)
    assert code == 0
    assert out == (EX / "expected" / golden).read_text()


def test_lint_matches_golden_file_and_exit_code():
    exprs = (EX / "expressions.txt").read_text().splitlines()
    code, out = run("lint", *exprs, "--tz", "Europe/London", "--year", "2026")
    assert code == 2  # contains errors
    assert out == (EX / "expected" / "lint_london_2026.txt").read_text()
    assert run("lint", "0 12 * * MON-FRI")[0] == 0
    assert run("lint", "0 0 31 * *")[0] == 1


def test_next_json_and_policy_flags():
    code, out = run("next", "30 2 * * *", "--tz", "America/New_York", "--after", "2026-03-07T12:00",
                    "-n", "1", "--gap", "skip", "--json")
    data = json.loads(out)
    assert code == 0 and data["gap"] == "skip"
    assert data["occurrences"][0]["fires_at_local"] == "2026-03-09T02:30:00-04:00"


def test_errors_exit_2(capsys):
    assert run("next", "61 * * * *", "--after", "2026-01-01")[0] == 2
    assert run("next", "* * * * *", "--tz", "Mars/Olympus", "--after", "2026-01-01")[0] == 2
    assert "error" in capsys.readouterr().err


def test_job_file_validation():
    jobs = parse_jobs("# c\nweekly  UTC  0 9 * * FRI#3\n\n")
    assert jobs[0].expr == "0 9 * * FRI#3"  # '#' inside an expression is not a comment
    for bad, msg in [("x UTC", "expected"), ("a UTC * * * * *\na UTC @daily", "duplicate"),
                     ("a Nowhere/Zone @daily", "time zone"), ("a UTC 0 0 32 * *", "out of range")]:
        with pytest.raises(JobFileError, match=msg):
            parse_jobs(bad)
