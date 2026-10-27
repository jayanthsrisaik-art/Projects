import pytest

from depsolve.ranges import RangeError, parse_range
from depsolve.semver import parse_version

CASES = [
    # caret
    ("^1.2.3", "1.2.3", True), ("^1.2.3", "1.9.0", True), ("^1.2.3", "2.0.0", False),
    ("^1.2.3", "1.2.2", False), ("^0.2.3", "0.2.9", True), ("^0.2.3", "0.3.0", False),
    ("^0.0.3", "0.0.3", True), ("^0.0.3", "0.0.4", False), ("^0.0", "0.0.9", True),
    ("^0.0", "0.1.0", False), ("^1", "1.99.0", True), ("^1.x", "2.0.0", False),
    # tilde
    ("~1.2.3", "1.2.9", True), ("~1.2.3", "1.3.0", False), ("~1.2", "1.2.0", True),
    ("~1", "1.9.0", True), ("~1", "2.0.0", False), ("~>1.2", "1.2.5", True),
    # x-ranges and partials
    ("1.x", "1.0.0", True), ("1.x", "2.0.0", False), ("1.2.*", "1.2.7", True),
    ("1.2", "1.3.0", False), ("*", "9.9.9", True), ("", "0.0.1", True), ("1.x.9", "1.4.0", True),
    # primitives with partial versions
    (">1.2", "1.2.9", False), (">1.2", "1.3.0", True), (">1", "2.0.0", True),
    ("<=1.2", "1.2.9", True), ("<=1.2", "1.3.0", False), ("<1.2", "1.1.9", True),
    ("<1.2", "1.2.0", False), (">=1.2", "1.2.0", True), ("=1.2.3", "1.2.3", True),
    (">= 1.2.0 < 2", "1.5.0", True), (">*", "1.0.0", False), ("<*", "0.0.0", False),
    # hyphen ranges
    ("1.2.3 - 2.3.4", "2.3.4", True), ("1.2.3 - 2.3.4", "2.3.5", False),
    ("1.2 - 2.3", "2.3.9", True), ("1.2 - 2.3", "2.4.0", False), ("1.2 - 2", "1.1.9", False),
    # OR
    ("^1.0.0 || ^3.0.0", "3.1.0", True), ("^1.0.0 || ^3.0.0", "2.0.0", False),
    # prerelease rule
    ("^1.0.0", "1.5.0-beta", False),
    (">=1.5.0-beta <2", "1.5.0-beta.2", True),
    (">=1.5.0-beta <2", "1.6.0-beta", False),
    ("^2.0.0-rc.1", "2.0.0-rc.2", True),
    ("^2.0.0-rc.1", "2.0.0-rc.0", False),
    ("^2.0.0-rc.1", "2.0.0", True),
    ("<2.0.0", "2.0.0-rc.1", False),
    ("*", "1.0.0-alpha", False),
]


@pytest.mark.parametrize("rng,version,expected", CASES)
def test_range_semantics(rng, version, expected):
    assert parse_range(rng).satisfies(parse_version(version)) is expected


@pytest.mark.parametrize("bad", ["^a.b", ">=1.2.3.4", "~v", "1.2.3 - ", "<=>1"])
def test_invalid_ranges_rejected(bad):
    with pytest.raises(RangeError):
        parse_range(bad)


def test_filter_preserves_order():
    vs = [parse_version(v) for v in ["2.0.0", "1.5.0", "1.0.0", "0.9.0"]]
    assert [str(v) for v in parse_range("^1").filter(vs)] == ["1.5.0", "1.0.0"]
