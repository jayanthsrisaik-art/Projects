"""Path-level JSON diff used for the audit log."""
from __future__ import annotations

from typing import Any

from .evaluator import _kind, json_equal

_MISSING = object()


def json_diff(before: Any, after: Any, path: str = "") -> list[dict]:
    """List of ``{"path", "before", "after"}`` changes; absent values are reported as ``None``
    with ``"op"`` set to ``added``/``removed``/``changed``."""
    if before is _MISSING:
        return [{"path": path, "op": "added", "before": None, "after": after}]
    if after is _MISSING:
        return [{"path": path, "op": "removed", "before": before, "after": None}]
    kb, ka = _kind(before), _kind(after)
    if kb == ka == "object":
        out: list[dict] = []
        for k in sorted(set(before) | set(after)):
            sub = f"{path}.{k}" if path else k
            out += json_diff(before.get(k, _MISSING), after.get(k, _MISSING), sub)
        return out
    if kb == ka == "array":
        out = []
        for i in range(max(len(before), len(after))):
            b = before[i] if i < len(before) else _MISSING
            a = after[i] if i < len(after) else _MISSING
            out += json_diff(b, a, f"{path}[{i}]")
        return out
    if json_equal(before, after):
        return []
    return [{"path": path, "op": "changed", "before": before, "after": after}]
