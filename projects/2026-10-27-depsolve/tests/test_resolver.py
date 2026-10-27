import pytest

from depsolve.greedy import greedy_resolve
from depsolve.registry import Registry
from depsolve.resolver import ResolutionError, Resolver, resolve, verify
from depsolve.semver import parse_version

from helpers import backjump_problem, deps, make_registry, versions
from helpers import SAMPLES



def as_text(solution):
    return {k: str(v) for k, v in solution.items()}


def test_sample_needs_backtracking_and_greedy_fails():
    reg = Registry.load(SAMPLES / "registry.json")
    root = deps({"web": "^2.0.0", "orm": "^3.0.0", "log": "^1.2.0"})
    res = resolve(reg, root)
    assert res.ok
    assert as_text(res.solution) == {
        "web": "2.2.1", "orm": "3.1.0", "log": "1.3.2", "http": "1.8.4",
        "tls": "2.4.0", "crypto": "3.2.0", "json": "1.6.0",
    }
    assert verify(reg, root, res.solution) == []
    assert not greedy_resolve(reg, root).ok  # picks web 2.3.0 -> http 2 -> tls 3, clashes with orm


def test_prefers_newest_and_skips_yanked_and_prereleases():
    reg = make_registry({"a": {"1.0.0": {}, "1.1.0": {}, "1.2.0!": {}, "1.3.0-beta": {}}})
    res = resolve(reg, deps({"a": "^1.0.0"}))
    assert as_text(res.solution) == {"a": "1.1.0"}


def test_locked_version_preferred_even_if_yanked():
    reg = make_registry({"a": {"1.0.0": {}, "1.1.0!": {}, "1.2.0": {}}})
    res = resolve(reg, deps({"a": "^1.0.0"}), locked=versions({"a": "1.1.0"}))
    assert as_text(res.solution) == {"a": "1.1.0"}
    # --upgrade drops the lock preference and the yanked exemption
    res = resolve(reg, deps({"a": "^1.0.0"}), locked=versions({"a": "1.1.0"}), upgrade={"a"})
    assert as_text(res.solution) == {"a": "1.2.0"}


def test_lock_ignored_when_it_no_longer_satisfies_manifest():
    reg = make_registry({"a": {"1.0.0": {}, "2.0.0": {}, "2.1.0": {}}})
    res = resolve(reg, deps({"a": "^2.0.0"}), locked=versions({"a": "1.0.0"}))
    assert as_text(res.solution) == {"a": "2.1.0"}


def test_dependency_cycles_and_self_dependency():
    reg = make_registry({
        "a": {"1.0.0": {"b": "^1", "a": "^1"}},
        "b": {"1.0.0": {"a": "^1"}},
    })
    res = resolve(reg, deps({"a": "*"}))
    assert as_text(res.solution) == {"a": "1.0.0", "b": "1.0.0"}


def test_unsatisfiable_self_dependency():
    reg = make_registry({"a": {"1.0.0": {"a": "^2"}}})
    assert not resolve(reg, deps({"a": "*"})).ok


def test_missing_package_fails_cleanly():
    reg = make_registry({"a": {"1.0.0": {"ghost": "^1"}}})
    assert not resolve(reg, deps({"a": "^1"})).ok
    assert not resolve(reg, deps({"nope": "*"})).ok


def test_only_reachable_packages_installed():
    reg = make_registry({
        "a": {"2.0.0": {"b": "^1"}, "1.0.0": {}},
        "b": {"1.0.0": {"c": "^9"}},  # unsatisfiable -> a falls back to 1.0.0
        "c": {"1.0.0": {}},
    })
    res = resolve(reg, deps({"a": "*"}))
    assert as_text(res.solution) == {"a": "1.0.0"}


def test_backjumping_explores_far_fewer_candidates():
    reg, root = backjump_problem(5)
    cbj = Resolver(reg, root).resolve()
    chrono = Resolver(reg, root, backjump=False).resolve()
    assert cbj.ok and chrono.ok
    assert cbj.solution == chrono.solution
    assert as_text(cbj.solution)["a"] == "1.0.0"
    assert cbj.stats.backjumps >= 1
    assert cbj.stats.steps * 50 < chrono.stats.steps


def test_step_budget():
    reg, root = backjump_problem(6)
    with pytest.raises(ResolutionError):
        Resolver(reg, root, backjump=False, max_steps=500).resolve()


def test_verify_reports_violations():
    reg = make_registry({"a": {"1.0.0": {"b": "^2"}, "1.1.0!": {}}, "b": {"1.0.0": {}}, "c": {"1.0.0": {}}})
    root = deps({"a": "^1"})
    problems = verify(reg, root, versions({"a": "1.0.0", "b": "1.0.0", "c": "1.0.0"}))
    assert any("requires b ^2" in p for p in problems)
    assert any("c@1.0.0 is not reachable" in p for p in problems)
    assert any("yanked" in p for p in verify(reg, root, versions({"a": "1.1.0"})))
    assert "a@9.0.0 is not published" in verify(reg, root, {"a": parse_version("9.0.0")})
