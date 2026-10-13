import pytest

from taskgraph.config import ConfigError, load, parse
from taskgraph.graph import find_cycle, levels, select, validate
from pathlib import Path


def proj(tasks):
    return parse({"task": tasks}, Path("."))


def test_string_cmd_is_split():
    p = proj({"a": {"cmd": "echo 'hi there'"}})
    assert p.tasks["a"].cmd == ("echo", "hi there")


@pytest.mark.parametrize("bad,msg", [
    ({}, "at least one"),
    ({"a": {}}, "'cmd'"),
    ({"a": {"cmd": ""}}, "empty"),
    ({"a": {"cmd": "x", "bogus": 1}}, "unknown keys"),
    ({"a": {"cmd": "x", "deps": ["nope"]}}, "unknown task"),
    ({"a": {"cmd": "x", "deps": "b"}}, "list of strings"),
])
def test_invalid_configs(bad, msg):
    with pytest.raises(ConfigError, match=msg):
        parse({"task": bad} if bad else {}, Path("."))


def test_load_errors(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load(tmp_path / "missing.toml")
    f = tmp_path / "bad.toml"
    f.write_text("[task.a\n")
    with pytest.raises(ConfigError, match="invalid TOML"):
        load(f)


def test_cycle_detected_with_path():
    p = proj({"a": {"cmd": "x", "deps": ["b"]}, "b": {"cmd": "x", "deps": ["c"]},
              "c": {"cmd": "x", "deps": ["a"]}})
    cycle = find_cycle(p)
    assert cycle[0] == cycle[-1] and set(cycle) == {"a", "b", "c"}
    with pytest.raises(ConfigError, match="cycle"):
        validate(p)


def test_self_cycle():
    assert find_cycle(proj({"a": {"cmd": "x", "deps": ["a"]}})) == ["a", "a"]


def test_select_and_levels_diamond():
    p = proj({
        "base": {"cmd": "x"},
        "left": {"cmd": "x", "deps": ["base"]},
        "right": {"cmd": "x", "deps": ["base"]},
        "top": {"cmd": "x", "deps": ["left", "right"]},
        "other": {"cmd": "x"},
    })
    assert select(p, ["left"]) == {"left", "base"}
    assert select(p, None) == set(p.tasks)
    assert levels(p, select(p, ["top"])) == [["base"], ["left", "right"], ["top"]]
    with pytest.raises(ConfigError, match="unknown task"):
        select(p, ["zzz"])
