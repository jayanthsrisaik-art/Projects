from cronwhen.lint import lint


def codes(expr, **kw):
    return {f.code for f in lint(expr, **kw)}


def test_never_fires_and_skipped_months():
    assert codes("0 0 30 2 *") == {"never-fires"}
    assert codes("0 0 31 4,6 *") == {"never-fires"}
    assert codes("0 0 29 2 *") == set()  # leap years exist
    assert "skips-months" in codes("0 0 30 * *")
    assert codes("0 0 L * *") == set()


def test_dom_dow_and_mode_can_make_rare_but_valid_schedules():
    assert "never-fires" not in codes("0 0 13 * FRI", dom_dow="and")
    # the 31st is never the first Monday of a month
    assert "never-fires" in codes("0 0 31 * MON#1", dom_dow="and")


def test_steps():
    assert "step-noop" in codes("*/60 * * * *")
    assert "uneven-step" in codes("0 */5 * * *")
    assert codes("*/15 * * * *") == set()


def test_dst_exposure_depends_on_zone_and_time():
    assert {"dst-gap", "dst-overlap"} <= codes("30 1 * * *", tz="Europe/London", year=2026)
    assert codes("30 1 * * *", tz="Asia/Tokyo", year=2026) == set()
    assert codes("30 3 * * *", tz="Europe/London", year=2026) == set()
    gap = [f for f in lint("30 2 * * *", tz="America/New_York", year=2026) if f.code == "dst-gap"]
    assert "2026-03-08" in gap[0].message and "shift" in gap[0].message


def test_syntax_error_is_a_finding():
    (f,) = lint("0 0 * * 9")
    assert f.level == "error" and f.code == "syntax"
