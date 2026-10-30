"""Flag evaluation: targeting rules, deterministic percentage rollouts, prerequisites.

This module is deliberately dependency-free and mirrors ``web/src/evaluator.ts``
line for line; ``samples/parity_expected.json`` pins both implementations to the
same answers.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Mapping

BUCKET_SCALE = 100_000  # weights are expressed in 1/1000ths of a percent

FlagLookup = Callable[[str], "Mapping[str, Any] | None"]


# --------------------------------------------------------------------------- hashing
def murmur3_32(data: bytes, seed: int = 0) -> int:
    """MurmurHash3 x86 32-bit. Implemented by hand so the TS client can match it exactly."""
    c1, c2 = 0xCC9E2D51, 0x1B873593
    h = seed & 0xFFFFFFFF
    n = len(data)
    nblocks = n // 4
    for i in range(nblocks):
        k = int.from_bytes(data[4 * i : 4 * i + 4], "little")
        k = (k * c1) & 0xFFFFFFFF
        k = ((k << 15) | (k >> 17)) & 0xFFFFFFFF
        k = (k * c2) & 0xFFFFFFFF
        h ^= k
        h = ((h << 13) | (h >> 19)) & 0xFFFFFFFF
        h = (h * 5 + 0xE6546B64) & 0xFFFFFFFF
    tail = data[4 * nblocks :]
    k = 0
    if len(tail) >= 3:
        k ^= tail[2] << 16
    if len(tail) >= 2:
        k ^= tail[1] << 8
    if len(tail) >= 1:
        k ^= tail[0]
        k = (k * c1) & 0xFFFFFFFF
        k = ((k << 15) | (k >> 17)) & 0xFFFFFFFF
        k = (k * c2) & 0xFFFFFFFF
        h ^= k
    h ^= n
    h ^= h >> 16
    h = (h * 0x85EBCA6B) & 0xFFFFFFFF
    h ^= h >> 13
    h = (h * 0xC2B2AE35) & 0xFFFFFFFF
    h ^= h >> 16
    return h


MAX_SAFE_INT = 2**53 - 1


def bucket_key(value: Any) -> str | None:
    """Strings and safe integers are bucketable; anything else would stringify differently in JS.

    ``7.0`` counts as the integer 7 because JSON.parse cannot tell them apart.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, int) and abs(value) <= MAX_SAFE_INT:
        return str(value)
    return None


def bucket(flag_key: str, salt: str, value: str) -> int:
    """Map a (flag, salt, value) triple to [0, BUCKET_SCALE)."""
    return murmur3_32(f"{flag_key}.{salt}.{value}".encode("utf-8")) % BUCKET_SCALE


# --------------------------------------------------------------------------- clauses
def _kind(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, (int, float)):
        return "number"
    if isinstance(v, str):
        return "string"
    if isinstance(v, list):
        return "array"
    return "object"


def json_equal(a: Any, b: Any) -> bool:
    """Equality with JSON semantics: ``true`` is not ``1``, ``1`` is ``1.0``."""
    ka, kb = _kind(a), _kind(b)
    if ka != kb:
        return False
    if ka == "array":
        return len(a) == len(b) and all(json_equal(x, y) for x, y in zip(a, b))
    if ka == "object":
        return a.keys() == b.keys() and all(json_equal(a[k], b[k]) for k in a)
    return a == b


def _match_one(op: str, actual: Any, expected: Any) -> bool:
    if op == "in":
        return json_equal(actual, expected)
    if op in ("startsWith", "endsWith", "contains", "matches"):
        if not (isinstance(actual, str) and isinstance(expected, str)):
            return False
        if op == "startsWith":
            return actual.startswith(expected)
        if op == "endsWith":
            return actual.endswith(expected)
        if op == "contains":
            return expected in actual
        try:
            return re.search(expected, actual) is not None
        except re.error:
            return False
    if op in ("lt", "lte", "gt", "gte"):
        if _kind(actual) != "number" or _kind(expected) != "number":
            return False
        if op == "lt":
            return actual < expected
        if op == "lte":
            return actual <= expected
        if op == "gt":
            return actual > expected
        return actual >= expected
    return False


def clause_matches(clause: Mapping[str, Any], context: Mapping[str, Any]) -> bool:
    actual = context.get(clause["attribute"])
    if actual is None:
        # A missing attribute never matches, even for negated clauses: "country not in [US]"
        # should not silently include users whose country is unknown.
        return False
    candidates = actual if isinstance(actual, list) else [actual]
    hit = any(_match_one(clause["op"], c, e) for c in candidates for e in clause["values"])
    return not hit if clause.get("negate") else hit


# --------------------------------------------------------------------------- evaluation
def _result(flag: Mapping[str, Any], index: int, reason: dict) -> dict:
    return {"value": flag["variations"][index], "variation": index, "reason": reason}


def _serve(flag: Mapping[str, Any], serve: Mapping[str, Any], context: Mapping[str, Any]) -> int:
    if serve.get("variation") is not None:
        return serve["variation"]
    rollout = serve["rollout"]
    value = bucket_key(context.get(rollout.get("bucket_by") or "key"))
    # Contexts lacking the bucketing attribute all land in bucket 0, i.e. the first
    # variation with non-zero weight. That is stable and explainable, unlike a random pick.
    b = 0 if value is None else bucket(flag["key"], flag.get("salt") or flag["key"], value)
    cumulative = 0
    last_nonzero = 0
    for i, w in enumerate(rollout["weights"]):
        if w > 0:
            last_nonzero = i
        cumulative += w
        if b < cumulative:
            return i
    return last_nonzero


def evaluate(
    flag: Mapping[str, Any],
    env: str,
    context: Mapping[str, Any],
    lookup: FlagLookup,
    _stack: tuple[str, ...] = (),
) -> dict:
    """Evaluate ``flag`` in ``env`` for ``context``.

    Returns ``{"value", "variation", "reason"}``. ``lookup`` resolves prerequisite flags.
    """
    key = context.get("key")
    if not isinstance(key, str) or not key:
        return {"value": None, "variation": None, "reason": {"kind": "ERROR", "error": "CONTEXT_KEY_MISSING"}}
    cfg = flag["environments"].get(env)
    if cfg is None:
        return {"value": None, "variation": None, "reason": {"kind": "ERROR", "error": "UNKNOWN_ENVIRONMENT"}}
    if flag["key"] in _stack:
        return {"value": None, "variation": None, "reason": {"kind": "ERROR", "error": "PREREQUISITE_CYCLE"}}

    if not cfg["on"]:
        return _result(flag, cfg["off_variation"], {"kind": "OFF"})

    for pre in flag.get("prerequisites") or []:
        pre_flag = lookup(pre["key"])
        ok = False
        if pre_flag is not None:
            pre_cfg = pre_flag["environments"].get(env)
            res = evaluate(pre_flag, env, context, lookup, _stack + (flag["key"],))
            if res["reason"].get("error") == "PREREQUISITE_CYCLE":
                return res
            ok = bool(pre_cfg and pre_cfg["on"]) and res["variation"] == pre["variation"]
        if not ok:
            return _result(flag, cfg["off_variation"], {"kind": "PREREQUISITE_FAILED", "prerequisite": pre["key"]})

    for target in cfg.get("targets") or []:
        if key in target["values"]:
            return _result(flag, target["variation"], {"kind": "TARGET_MATCH"})

    for i, rule in enumerate(cfg.get("rules") or []):
        if all(clause_matches(c, context) for c in rule["clauses"]):
            return _result(flag, _serve(flag, rule["serve"], context), {"kind": "RULE_MATCH", "rule_index": i, "rule_id": rule["id"]})

    return _result(flag, _serve(flag, cfg["fallthrough"], context), {"kind": "FALLTHROUGH"})


def evaluate_all(flags: Mapping[str, Mapping[str, Any]], env: str, context: Mapping[str, Any]) -> dict[str, dict]:
    return {k: evaluate(f, env, context, flags.get) for k, f in sorted(flags.items())}
