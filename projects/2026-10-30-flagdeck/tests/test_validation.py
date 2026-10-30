import pytest
from pydantic import ValidationError

from conftest import flag, load_sample_flags
from flagdeck.diff import json_diff
from flagdeck.schema import FlagIn, find_cycle, semantic_errors


def errors(doc, others=None):
    return semantic_errors(doc, others or {})


def test_sample_flags_are_valid():
    flags = load_sample_flags()
    for k, doc in flags.items():
        assert errors(doc, {o: d for o, d in flags.items() if o != k}) == []


def test_defaults_fill_every_environment_and_salt():
    doc = flag("x")
    assert set(doc["environments"]) == {"dev", "prod"}
    assert doc["salt"] == "x"
    assert doc["environments"]["prod"]["on"] is False


@pytest.mark.parametrize("bad", [
    {"key": "Bad Key", "variations": [1, 2]},
    {"key": "x", "variations": [True]},
    {"key": "x", "variations": [True, False], "environments": {"qa": {}}},
    {"key": "x", "variations": [True, False], "unexpected": 1},
    {"key": "x", "variations": [True, False], "environments": {"prod": {"fallthrough": {}}}},
    {"key": "x", "variations": [True, False],
     "environments": {"prod": {"fallthrough": {"variation": 1, "rollout": {"weights": [1, 99999]}}}}},
    {"key": "x", "variations": [True, False], "environments": {"prod": {"rules": [
        {"id": "r", "clauses": [{"attribute": "a", "op": "regex", "values": ["x"]}], "serve": {"variation": 0}}]}}},
])
def test_shape_errors_rejected(bad):
    with pytest.raises(ValidationError):
        FlagIn.model_validate(bad)


def test_semantic_errors():
    doc = flag("x", variations=[True, "yes", True], prod={
        "off_variation": 5,
        "targets": [{"variation": 1, "values": ["a"]}, {"variation": 0, "values": ["a"]}],
        "rules": [
            {"id": "r", "clauses": [{"attribute": "e", "op": "matches", "values": ["("]}], "serve": {"variation": 9}},
            {"id": "r", "clauses": [{"attribute": "v", "op": "lt", "values": ["4"]}],
             "serve": {"rollout": {"weights": [50_000, 40_000, 0]}}},
        ],
    })
    errs = "\n".join(errors(doc))
    for needle in ["share one JSON type", "duplicates an earlier variation", "off_variation 5 out of range",
                   "repeats context keys ['a']", "is not unique", "invalid regex", "variation 9 out of range",
                   "needs numeric values", "sum to 90000"]:
        assert needle in errs, needle


def test_rollout_weight_count_must_match_variations():
    doc = flag("x", prod={"fallthrough": {"rollout": {"weights": [100_000]}}})
    assert any("has 1 entries, expected 2" in e for e in errors(doc))


def test_prerequisite_must_exist_and_index_in_range():
    other = flag("other")
    assert errors(flag("x", prereqs=[{"key": "nope", "variation": 1}])) == ["prerequisite 'nope' does not exist"]
    assert "variation 5 out of range" in errors(flag("x", prereqs=[{"key": "other", "variation": 5}]), {"other": other})[0]


def test_prerequisite_cycles_detected():
    a = flag("a", prereqs=[{"key": "b", "variation": 1}])
    b = flag("b", prereqs=[{"key": "c", "variation": 1}])
    c_new = flag("c", prereqs=[{"key": "a", "variation": 1}])
    errs = errors(c_new, {"a": a, "b": b})
    assert errs == ["prerequisite cycle: c -> a -> b -> c"]
    assert errors(flag("s", prereqs=[{"key": "s", "variation": 1}])) == ["prerequisite cycle: s -> s"]


def test_find_cycle_ignores_diamonds():
    graph = {"top": ["l", "r"], "l": ["base"], "r": ["base"], "base": []}
    assert find_cycle(graph, "top") is None


def test_json_diff_paths():
    before = {"environments": {"prod": {"on": False, "rules": [{"id": "a"}]}}, "variations": [True, False]}
    after = {"environments": {"prod": {"on": True, "rules": [{"id": "a"}, {"id": "b"}]}}, "variations": [True, False]}
    assert json_diff(before, after) == [
        {"path": "environments.prod.on", "op": "changed", "before": False, "after": True},
        {"path": "environments.prod.rules[1]", "op": "added", "before": None, "after": {"id": "b"}},
    ]
    assert json_diff({"a": 1}, {"a": True}) == [{"path": "a", "op": "changed", "before": 1, "after": True}]
    assert json_diff({"a": [1, 2]}, {"a": [1]}) == [{"path": "a[1]", "op": "removed", "before": 2, "after": None}]
