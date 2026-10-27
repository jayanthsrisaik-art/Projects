from depsolve.explain import explain
from depsolve.registry import Registry
from depsolve.resolver import Resolver

from helpers import SAMPLES, deps, make_registry


def run(reg, root):
    r = Resolver(reg, root)
    res = r.resolve()
    assert not res.ok
    return explain(r, res)


def test_sample_conflict_core_and_reason():
    reg = Registry.load(SAMPLES / "registry.json")
    ex = run(reg, deps({"web": "^2.3.0", "orm": "^3.0.0", "log": "^1.0.0"}))
    assert ex.core == ["orm ^3.0.0", "web ^2.3.0"]  # log is innocent and dropped
    assert len(ex.reasons) == 1
    assert "http@2.0.0/2.1.0 requires tls ^3.0.0" in ex.reasons[0]
    assert "orm@3.0.0/3.1.0 requires tls ^2.0.0/^2.1.0" in ex.reasons[0]


def test_core_is_minimal():
    reg = make_registry({
        "a": {"1.0.0": {"x": "^1"}}, "b": {"1.0.0": {"x": "^2"}},
        "c": {"1.0.0": {}}, "x": {"1.0.0": {}, "2.0.0": {}},
    })
    root = deps({"a": "*", "b": "*", "c": "*"})
    ex = run(reg, root)
    assert ex.core == ["a *", "b *"]
    for drop in ("a", "b"):
        rest = {k: v for k, v in root.items() if k not in (drop, "c")}
        assert Resolver(reg, rest).resolve().ok


def test_missing_and_no_matching_version_reasons():
    reg = make_registry({"a": {"1.0.0": {"ghost": "^1"}}, "b": {"1.0.0": {}, "1.1.0!": {}}})
    assert "ghost is not in the registry (required by a@1.0.0)" in run(reg, deps({"a": "*"})).reasons
    reasons = run(reg, deps({"b": "^1.1.0"})).reasons
    assert reasons == ["root requires b ^1.1.0, but no installable version matches (available: 1.0.0)"]


def test_render_mentions_core_and_reasons():
    reg = make_registry({"a": {"1.0.0": {}}})
    text = run(reg, deps({"a": "^2"})).render()
    assert text.startswith("Resolution failed.")
    assert "  - a ^2" in text and "Because:" in text


def test_clash_with_already_chosen_version_is_explained():
    # Search order is orm -> tls (pinned to 1.0.0 by orm) -> web, so web's clash
    # is with an already-chosen version, not an empty domain; it must still be explained.
    reg = make_registry({
        "web": {"1.0.0": {"tls": "^2"}}, "orm": {"1.0.0": {"tls": "^1"}},
        "tls": {"1.0.0": {}, "2.0.0": {}},
    })
    ex = run(reg, deps({"web": "*", "orm": "*"}))
    assert ex.reasons == [
        "orm@1.0.0 requires tls ^1, but web@1.0.0 requires tls ^2; no version of tls satisfies both"
    ]
