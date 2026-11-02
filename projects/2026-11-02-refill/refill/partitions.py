"""Partition calendar and dependency mappings.

A *node* is ``(table, key)`` where ``key`` is an ISO day (``2026-10-05``), a
month (``2026-10``) or ``"*"`` for unpartitioned tables.

Every mapping knows two directions:

* ``forward(out_key)``  -> upstream keys the output partition *reads*
* ``inverse(in_key)``   -> output keys that *read* the upstream partition

The planner walks ``inverse`` (cheap, change-driven); the brute-force oracle
walks ``forward``.  ``tests/test_partitions.py`` checks the two are exact
converses for every mapping, which is what makes the planner trustworthy.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

GRAINS = ("day", "month", "none")
ALL = "*"


class Calendar:
    """The materialised date range shared by every partitioned table."""

    def __init__(self, start: str, end: str):
        self.start = date.fromisoformat(start)
        self.end = date.fromisoformat(end)
        if self.end < self.start:
            raise ValueError(f"range end {end} is before start {start}")
        n = (self.end - self.start).days + 1
        self._days = [(self.start + timedelta(days=i)).isoformat() for i in range(n)]
        self._day_set = set(self._days)
        self._months = sorted({d[:7] for d in self._days})
        self._month_set = set(self._months)

    # -- enumeration -----------------------------------------------------
    def partitions(self, grain: str) -> list[str]:
        if grain == "day":
            return list(self._days)
        if grain == "month":
            return list(self._months)
        if grain == "none":
            return [ALL]
        raise ValueError(f"unknown grain {grain!r}")

    def contains(self, grain: str, key: str) -> bool:
        if grain == "day":
            return key in self._day_set
        if grain == "month":
            return key in self._month_set
        return key == ALL

    # -- helpers ---------------------------------------------------------
    def shift_day(self, key: str, n: int) -> str:
        """Shift a day key by ``n`` days (result may lie outside the range)."""
        return (date.fromisoformat(key) + timedelta(days=n)).isoformat()

    def days_in_month(self, month: str) -> list[str]:
        return [d for d in self._days if d[:7] == month]

    def bounds(self, grain: str, key: str) -> tuple[str, str]:
        """Inclusive [start, end] day bounds of a partition, clipped to range."""
        if grain == "day":
            return key, key
        if grain == "month":
            days = self.days_in_month(key)
            return days[0], days[-1]
        return self._days[0], self._days[-1]

    def previous(self, grain: str, key: str) -> str:
        """Previous partition key (unclipped) - used for the ``:prev`` param."""
        if grain == "day":
            return self.shift_day(key, -1)
        if grain == "month":
            y, m = int(key[:4]), int(key[5:7])
            y, m = (y - 1, 12) if m == 1 else (y, m - 1)
            return f"{y:04d}-{m:02d}"
        return ALL

    def start_day(self, grain: str, key: str) -> str:
        return self.bounds(grain, key)[0]


# ---------------------------------------------------------------------------
# Mappings
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Mapping:
    """How an output partition depends on an upstream table's partitions."""

    kind: str
    n: int = 1

    KINDS = ("same", "window", "lag", "rollup", "month_of", "all")

    def validate(self, out_grain: str, in_grain: str) -> None:
        k = self.kind
        if k not in self.KINDS:
            raise ValueError(f"unknown mapping {k!r}; expected one of {self.KINDS}")
        if k in ("window", "lag") and self.n < 1:
            raise ValueError(f"{k} mapping needs n >= 1, got {self.n}")
        need = {
            "same": None,
            "window": ("day", "day"),
            "lag": ("day", "day"),
            "rollup": ("month", "day"),
            "month_of": ("day", "month"),
        }
        if k == "same" and out_grain != in_grain:
            raise ValueError(f"'same' needs equal grains, got out={out_grain} in={in_grain}")
        if k in need and need[k] is not None and (out_grain, in_grain) != need[k]:
            raise ValueError(
                f"{k!r} needs out/in grains {need[k]}, got ({out_grain}, {in_grain})"
            )

    def forward(self, cal: Calendar, out_grain: str, in_grain: str, out_key: str) -> list[str]:
        k = self.kind
        if k == "same":
            return [out_key]
        if k == "window":
            keys = [cal.shift_day(out_key, -i) for i in range(self.n - 1, -1, -1)]
            return [x for x in keys if cal.contains("day", x)]
        if k == "lag":
            x = cal.shift_day(out_key, -self.n)
            return [x] if cal.contains("day", x) else []
        if k == "rollup":
            return cal.days_in_month(out_key)
        if k == "month_of":
            return [out_key[:7]]
        if k == "all":
            return cal.partitions(in_grain)
        raise AssertionError(k)

    def inverse(self, cal: Calendar, out_grain: str, in_grain: str, in_key: str) -> list[str]:
        k = self.kind
        if k == "same":
            return [in_key]
        if k == "window":
            keys = [cal.shift_day(in_key, i) for i in range(self.n)]
            return [x for x in keys if cal.contains("day", x)]
        if k == "lag":
            x = cal.shift_day(in_key, self.n)
            return [x] if cal.contains("day", x) else []
        if k == "rollup":
            return [in_key[:7]]
        if k == "month_of":
            return cal.days_in_month(in_key)
        if k == "all":
            return cal.partitions(out_grain)
        raise AssertionError(k)

    def describe(self) -> str:
        return f"{self.kind}({self.n})" if self.kind in ("window", "lag") else self.kind
