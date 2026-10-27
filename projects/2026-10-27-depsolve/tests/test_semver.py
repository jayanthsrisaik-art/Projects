import pytest

from depsolve.semver import VersionError, parse_version


def test_spec_precedence_order():
    # Ordering example from the SemVer 2.0.0 specification, section 11.
    ordered = [
        "1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-alpha.beta", "1.0.0-beta",
        "1.0.0-beta.2", "1.0.0-beta.11", "1.0.0-rc.1", "1.0.0", "1.0.1", "1.1.0", "2.0.0",
    ]
    versions = [parse_version(v) for v in ordered]
    assert sorted(reversed(versions)) == versions


def test_build_metadata_ignored_for_precedence():
    a, b = parse_version("1.2.3+build.1"), parse_version("1.2.3+other")
    assert a == b and hash(a) == hash(b)
    assert str(a) == "1.2.3+build.1"


def test_numeric_identifiers_compare_numerically():
    assert parse_version("1.0.0-rc.2") < parse_version("1.0.0-rc.10")
    assert parse_version("10.0.0") > parse_version("9.99.99")


@pytest.mark.parametrize("bad", ["1.2", "01.2.3", "1.2.3-01", "1.2.3-", "a.b.c", "", "1.2.3.4"])
def test_invalid_versions_rejected(bad):
    with pytest.raises(VersionError):
        parse_version(bad)


def test_leading_v_accepted():
    assert parse_version("v1.2.3") == parse_version("1.2.3")
