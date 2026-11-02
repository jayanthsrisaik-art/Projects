import itertools

import pytest

from refill.partitions import ALL, Calendar, Mapping

CAL = Calendar("2026-01-25", "2026-03-04")  # crosses month boundaries, partial months

CASES = [
    (Mapping("same"), "day", "day"),
    (Mapping("same"), "month", "month"),
    (Mapping("same"), "none", "none"),
    (Mapping("window", 1), "day", "day"),
    (Mapping("window", 7), "day", "day"),
    (Mapping("window", 30), "day", "day"),
    (Mapping("lag", 1), "day", "day"),
    (Mapping("lag", 10), "day", "day"),
    (Mapping("rollup"), "month", "day"),
    (Mapping("month_of"), "day", "month"),
    (Mapping("all"), "day", "none"),
    (Mapping("all"), "month", "day"),
    (Mapping("all"), "none", "day"),
]


@pytest.mark.parametrize("m,og,ig", CASES, ids=lambda x: str(x))
def test_forward_and_inverse_are_exact_converses(m, og, ig):
    """q in inverse(p)  <=>  p in forward(q), for every pair of partitions."""
    m.validate(og, ig)
    outs, ins = CAL.partitions(og), CAL.partitions(ig)
    for o, i in itertools.product(outs, ins):
        assert (i in m.forward(CAL, og, ig, o)) == (o in m.inverse(CAL, og, ig, i)), (o, i)
    for o in outs:
        assert all(CAL.contains(ig, k) for k in m.forward(CAL, og, ig, o))
    for i in ins:
        assert all(CAL.contains(og, k) for k in m.inverse(CAL, og, ig, i))


def test_window_clips_at_range_start():
    m = Mapping("window", 7)
    assert m.forward(CAL, "day", "day", "2026-01-27") == ["2026-01-25", "2026-01-26", "2026-01-27"]
    assert m.inverse(CAL, "day", "day", "2026-03-02") == ["2026-03-02", "2026-03-03", "2026-03-04"]


def test_month_bounds_are_clipped_and_leap_aware():
    assert CAL.partitions("month") == ["2026-01", "2026-02", "2026-03"]
    assert CAL.bounds("month", "2026-01") == ("2026-01-25", "2026-01-31")
    assert CAL.bounds("month", "2026-02") == ("2026-02-01", "2026-02-28")
    assert CAL.bounds("none", ALL) == ("2026-01-25", "2026-03-04")
    assert CAL.previous("month", "2026-01") == "2025-12"
    assert CAL.previous("day", "2026-03-01") == "2026-02-28"


@pytest.mark.parametrize("m,og,ig", [
    (Mapping("same"), "day", "month"),
    (Mapping("rollup"), "day", "day"),
    (Mapping("month_of"), "month", "day"),
    (Mapping("window", 0), "day", "day"),
    (Mapping("window", 3), "month", "month"),
    (Mapping("bogus"), "day", "day"),
])
def test_invalid_mapping_grains_rejected(m, og, ig):
    with pytest.raises(ValueError):
        m.validate(og, ig)


def test_calendar_rejects_inverted_range():
    with pytest.raises(ValueError):
        Calendar("2026-02-01", "2026-01-01")
