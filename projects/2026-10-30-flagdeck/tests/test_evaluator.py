import random

import pytest

from conftest import flag
from flagdeck.evaluator import (bucket, bucket_key, clause_matches, evaluate, evaluate_all, json_equal,
                                murmur3_32)

NONE = {}.get


def ev(f, ctx, env="prod", flags=None):
    flags = flags or {f["key"]: f}
    return evaluate(f, env, ctx, flags.get)


# ---------------------------------------------------------------- hashing
@pytest.mark.parametrize("data,seed,expected", [
    (b"", 0, 0),
    (b"", 1, 0x514E28B7),
    (b"hello", 0, 0x248BFA47),
    (b"Hello, world!", 1234, 0xFAF6CDB3),
    (b"The quick brown fox jumps over the lazy dog", 0, 0x2E4FF723),
])
def test_murmur_reference_vectors(data, seed, expected):
    assert murmur3_32(data, seed) == expected


def test_murmur_matches_mmh3_when_available():
    mmh3 = pytest.importorskip("mmh3")
    rng = random.Random(3)
    for _ in range(2000):
        data = bytes(rng.randrange(256) for _ in range(rng.randrange(0, 33)))
        seed = rng.randrange(2**32)
        assert murmur3_32(data, seed) == mmh3.hash(data, seed, signed=False)


def test_bucket_key_types():
    assert bucket_key("abc") == "abc"
    assert bucket_key(42) == "42"
    assert bucket_key(42.0) == "42"  # JS cannot distinguish 42.0 from 42
    assert bucket_key(4.5) is None
    assert bucket_key(True) is None
    assert bucket_key(2**60) is None  # not exactly representable in JS
    assert bucket_key(None) is None


# ---------------------------------------------------------------- evaluation order
def test_off_serves_off_variation_and_ignores_targets():
    f = flag(prod={"on": False, "off_variation": 0, "targets": [{"variation": 1, "values": ["u"]}]})
    assert ev(f, {"key": "u"}) == {"value": False, "variation": 0, "reason": {"kind": "OFF"}}


def test_targets_beat_rules_and_rules_are_ordered():
    f = flag(variations=["a", "b", "c"], prod={
        "on": True,
        "targets": [{"variation": 2, "values": ["vip"]}],
        "rules": [
            {"id": "first", "clauses": [{"attribute": "plan", "op": "in", "values": ["pro"]}], "serve": {"variation": 1}},
            {"id": "second", "clauses": [{"attribute": "plan", "op": "in", "values": ["pro", "free"]}], "serve": {"variation": 0}},
        ],
        "fallthrough": {"variation": 2},
    })
    assert ev(f, {"key": "vip", "plan": "pro"})["reason"] == {"kind": "TARGET_MATCH"}
    r = ev(f, {"key": "x", "plan": "pro"})
    assert (r["value"], r["reason"]) == ("b", {"kind": "RULE_MATCH", "rule_index": 0, "rule_id": "first"})
    assert ev(f, {"key": "x", "plan": "free"})["reason"]["rule_id"] == "second"
    assert ev(f, {"key": "x", "plan": "team"})["reason"] == {"kind": "FALLTHROUGH"}


def test_all_clauses_in_a_rule_must_match():
    f = flag(prod={"on": True, "rules": [{"id": "r", "clauses": [
        {"attribute": "plan", "op": "in", "values": ["pro"]},
        {"attribute": "country", "op": "in", "values": ["US"]}], "serve": {"variation": 1}}]})
    assert ev(f, {"key": "a", "plan": "pro", "country": "US"})["value"] is True
    assert ev(f, {"key": "a", "plan": "pro", "country": "DE"})["value"] is False


def test_missing_context_key_is_an_error():
    f = flag(prod={"on": True})
    assert ev(f, {"email": "x"})["reason"] == {"kind": "ERROR", "error": "CONTEXT_KEY_MISSING"}
    assert ev(f, {"key": ""})["reason"]["kind"] == "ERROR"


# ---------------------------------------------------------------- prerequisites
def test_prerequisite_pass_fail_and_off():
    parent = flag("parent", prod={"on": True, "rules": [
        {"id": "beta", "clauses": [{"attribute": "beta", "op": "in", "values": [True]}], "serve": {"variation": 1}}]})
    child = flag("child", prereqs=[{"key": "parent", "variation": 1}], prod={"on": True, "fallthrough": {"variation": 1}})
    flags = {"parent": parent, "child": child}
    assert ev(child, {"key": "u", "beta": True}, flags=flags)["value"] is True
    assert ev(child, {"key": "u", "beta": False}, flags=flags)["reason"] == {"kind": "PREREQUISITE_FAILED", "prerequisite": "parent"}
    # A prerequisite that is OFF fails even if its off variation happens to equal the required one.
    parent["environments"]["prod"].update(on=False, off_variation=1)
    assert ev(child, {"key": "u", "beta": True}, flags=flags)["reason"]["kind"] == "PREREQUISITE_FAILED"


def test_missing_prerequisite_fails_closed():
    child = flag("child", prereqs=[{"key": "gone", "variation": 1}], prod={"on": True, "fallthrough": {"variation": 1}})
    assert ev(child, {"key": "u"})["reason"] == {"kind": "PREREQUISITE_FAILED", "prerequisite": "gone"}


def test_runtime_cycle_guard():
    # Validation forbids cycles; the evaluator still must not recurse forever on bad data.
    a = flag("a", prereqs=[{"key": "b", "variation": 1}], prod={"on": True})
    b = flag("b", prereqs=[{"key": "a", "variation": 1}], prod={"on": True})
    r = ev(a, {"key": "u"}, flags={"a": a, "b": b})
    assert r["reason"] == {"kind": "ERROR", "error": "PREREQUISITE_CYCLE"}


# ---------------------------------------------------------------- clauses
@pytest.mark.parametrize("op,values,actual,expected", [
    ("in", ["US", "CA"], "CA", True),
    ("in", [1], True, False),            # bool is not a number
    ("in", [True], 1, False),
    ("in", [4], 4.0, True),
    ("in", [{"a": 1}], {"a": 1}, True),
    ("startsWith", ["adm"], "admin", True),
    ("endsWith", ["@acme.com"], "x@acme.com", True),
    ("endsWith", ["@acme.com"], "x@acme.com.evil", False),
    ("contains", ["beta"], "closed-beta-2", True),
    ("contains", ["1"], 123, False),     # string ops never coerce
    ("matches", [r"^v\d+\.\d+$"], "v4.10", True),
    ("matches", [r"^v\d+$"], "v4.1", False),
    ("lt", [4.1], 4.0, True),
    ("lt", [4.1], "4.0", False),         # numeric ops never coerce
    ("gte", [18], 18, True),
    ("gt", [0], True, False),
])
def test_operators(op, values, actual, expected):
    assert clause_matches({"attribute": "a", "op": op, "values": values}, {"key": "k", "a": actual}) is expected


def test_list_attribute_matches_any_element():
    c = {"attribute": "groups", "op": "in", "values": ["beta"]}
    assert clause_matches(c, {"key": "k", "groups": ["mobile", "beta"]})
    assert not clause_matches(c, {"key": "k", "groups": []})


def test_negated_clause_never_matches_missing_attribute():
    c = {"attribute": "country", "op": "in", "values": ["US"], "negate": True}
    assert clause_matches(c, {"key": "k", "country": "DE"})
    assert not clause_matches(c, {"key": "k", "country": "US"})
    assert not clause_matches(c, {"key": "k"})
    assert not clause_matches(c, {"key": "k", "country": None})


def test_json_equal_is_type_strict():
    assert json_equal([1, "a", None], [1.0, "a", None])
    assert not json_equal([True], [1])
    assert not json_equal({"a": 1}, {"a": 1, "b": 2})


# ---------------------------------------------------------------- rollouts
def rollout_flag(weights, key="exp", bucket_by="key", salt=""):
    doc = flag(key, variations=[f"v{i}" for i in range(len(weights))],
               prod={"on": True, "fallthrough": {"rollout": {"bucket_by": bucket_by, "weights": weights}}})
    if salt:
        doc["salt"] = salt
    return doc


def test_rollout_distribution_close_to_weights():
    f = rollout_flag([50_000, 30_000, 20_000])
    n = 20_000
    counts = [0, 0, 0]
    for i in range(n):
        counts[ev(f, {"key": f"user-{i}"})["variation"]] += 1
    for got, want in zip(counts, (0.5, 0.3, 0.2)):
        assert abs(got / n - want) < 0.015, counts


def test_rollout_is_sticky_and_monotonic_when_increased():
    users = [{"key": f"u{i}"} for i in range(5000)]
    on_at = {}
    for pct in (5_000, 20_000, 50_000, 100_000):
        f = rollout_flag([100_000 - pct, pct])
        on_at[pct] = {u["key"] for u in users if ev(f, u)["value"] == "v1"}
        assert {u["key"] for u in users if ev(f, u)["value"] == "v1"} == on_at[pct]  # deterministic
    assert on_at[5_000] <= on_at[20_000] <= on_at[50_000] <= on_at[100_000]
    assert len(on_at[100_000]) == 5000


def test_salt_and_flag_key_reshuffle_buckets():
    users = [{"key": f"u{i}"} for i in range(4000)]
    on = lambda f: {u["key"] for u in users if ev(f, u)["value"] == "v1"}  # noqa: E731
    base = on(rollout_flag([50_000, 50_000], key="exp"))
    other_key = on(rollout_flag([50_000, 50_000], key="exp2"))
    resalted = on(rollout_flag([50_000, 50_000], key="exp", salt="v2"))
    # Independent 50/50 splits overlap ~50%; identical ones would overlap 100%.
    for s in (other_key, resalted):
        overlap = len(base & s) / len(base)
        assert 0.4 < overlap < 0.6, overlap


def test_bucket_by_custom_attribute_keeps_org_together():
    f = rollout_flag([50_000, 50_000], bucket_by="org")
    results = {ev(f, {"key": f"u{i}", "org": "org-42"})["value"] for i in range(200)}
    assert len(results) == 1
    assert bucket("exp", "exp", "org-42") == bucket("exp", "exp", "org-42")


def test_missing_bucket_attribute_lands_in_first_nonzero_variation():
    f = rollout_flag([0, 70_000, 30_000], bucket_by="device_id")
    assert ev(f, {"key": "u1"})["value"] == "v1"
    assert ev(f, {"key": "u1", "device_id": 3.5})["value"] == "v1"


def test_evaluate_all_returns_sorted_keys():
    a, b = flag("b-flag", prod={"on": True}), flag("a-flag", prod={"on": True})
    assert list(evaluate_all({"b-flag": a, "a-flag": b}, "prod", {"key": "u"})) == ["a-flag", "b-flag"]
