"""Cron expression parser.

Supports the classic five fields (minute hour day-of-month month day-of-week),
names (JAN..DEC, SUN..SAT), lists, ranges, steps (``*/n``, ``a-b/n``, ``a/n``),
macros (``@hourly`` ... ``@yearly``) and the widely used extensions:

* day-of-month: ``L`` (last day), ``L-n`` (n days before the last day),
  ``nW`` (weekday nearest to day n, never leaving the month), ``LW`` (last weekday)
* day-of-week:  ``nL`` / ``FRIL`` (last given weekday of the month),
  ``n#k`` / ``FRI#k`` (k-th given weekday of the month, k = 1..5)

Day-of-week 0 and 7 both mean Sunday.  When both day fields are restricted the
default is Vixie-cron OR semantics; ``dom_dow="and"`` requires both.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, Optional, Set, Tuple

MONTH_NAMES = {n: i for i, n in enumerate(
    "jan feb mar apr may jun jul aug sep oct nov dec".split(), start=1)}
DOW_NAMES = {n: i for i, n in enumerate("sun mon tue wed thu fri sat".split())}

MACROS = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}

FIELD_NAMES = ("minute", "hour", "day-of-month", "month", "day-of-week")
BOUNDS = {"minute": (0, 59), "hour": (0, 23), "day-of-month": (1, 31),
          "month": (1, 12), "day-of-week": (0, 7)}


class CronSyntaxError(ValueError):
    """Raised for an invalid expression; carries the offending field and token."""

    def __init__(self, message: str, expr: str, field_name: Optional[str] = None,
                 token: Optional[str] = None):
        self.expr = expr
        self.field_name = field_name
        self.token = token
        where = ""
        if field_name:
            where = f"{field_name}"
            if token is not None:
                where += f" token {token!r}"
            where += ": "
        super().__init__(f"{where}{message} (in {expr!r})")


@dataclass(frozen=True)
class CronSpec:
    """A parsed expression.  All sets use cron numbering (dow 0 = Sunday)."""

    expr: str
    minutes: FrozenSet[int]
    hours: FrozenSet[int]
    months: FrozenSet[int]
    dom: FrozenSet[int] = frozenset()
    dom_last: FrozenSet[int] = frozenset()        # offsets n of ``L-n`` (``L`` -> 0)
    dom_weekday: FrozenSet[int] = frozenset()     # n of ``nW``
    dom_last_weekday: bool = False                # ``LW``
    dow: FrozenSet[int] = frozenset()
    dow_last: FrozenSet[int] = frozenset()        # weekdays of ``nL``
    dow_nth: FrozenSet[Tuple[int, int]] = frozenset()  # (weekday, k) of ``n#k``
    dom_star: bool = True
    dow_star: bool = True
    minute_star: bool = False
    hour_star: bool = False
    dom_dow: str = "or"
    raw_fields: Tuple[str, ...] = field(default=(), compare=False)

    @property
    def is_interval(self) -> bool:
        """Vixie-cron's notion of a 'wildcard' job: minute or hour starts with '*'.

        Such jobs follow real elapsed time across DST changes; fixed-time jobs
        follow the wall clock.  Used by the ``auto`` DST policies.
        """
        return self.minute_star or self.hour_star


def _value(tok: str, name: str, expr: str, names: Optional[Dict[str, int]]) -> int:
    low = tok.lower()
    if names and low in names:
        return names[low]
    if not re.fullmatch(r"\d+", tok):
        raise CronSyntaxError("expected a number" + (" or name" if names else ""),
                              expr, name, tok)
    v = int(tok)
    lo, hi = BOUNDS[name]
    if not lo <= v <= hi:
        raise CronSyntaxError(f"value {v} out of range {lo}-{hi}", expr, name, tok)
    return v


def _names_for(name: str) -> Optional[Dict[str, int]]:
    return {"month": MONTH_NAMES, "day-of-week": DOW_NAMES}.get(name)


def _expand(part: str, name: str, expr: str) -> Set[int]:
    """Expand one comma-separated element without special tokens."""
    lo, hi = BOUNDS[name]
    if name == "day-of-week":
        star_hi = 6
    else:
        star_hi = hi
    names = _names_for(name)
    step = 1
    base = part
    if "/" in part:
        base, _, step_s = part.partition("/")
        if not re.fullmatch(r"\d+", step_s) or int(step_s) == 0:
            raise CronSyntaxError("step must be a positive integer", expr, name, part)
        step = int(step_s)
    if base in ("*", "?"):
        if base == "?" and name not in ("day-of-month", "day-of-week"):
            raise CronSyntaxError("'?' is only allowed in day fields", expr, name, part)
        start, end = lo, star_hi
    elif "-" in base:
        a, _, b = base.partition("-")
        start, end = _value(a, name, expr, names), _value(b, name, expr, names)
        if start > end:
            raise CronSyntaxError("range start is after range end (wrap-around ranges "
                                  "are not supported)", expr, name, part)
    else:
        start = _value(base, name, expr, names)
        end = (star_hi if name == "day-of-week" else hi) if "/" in part else start
    if not base:
        raise CronSyntaxError("empty value", expr, name, part)
    out = set(range(start, end + 1, step))
    if name == "day-of-week":
        out = {v % 7 for v in out}
    return out


def parse(expr: str, dom_dow: str = "or") -> CronSpec:
    """Parse a cron expression into a :class:`CronSpec`."""
    if dom_dow not in ("or", "and"):
        raise ValueError("dom_dow must be 'or' or 'and'")
    original = expr
    text = expr.strip()
    if not text:
        raise CronSyntaxError("empty expression", original)
    if text.startswith("@"):
        macro = text.lower()
        if macro == "@reboot":
            raise CronSyntaxError("@reboot has no calendar schedule", original)
        if macro not in MACROS:
            raise CronSyntaxError(f"unknown macro {text!r}", original)
        text = MACROS[macro]
    fields = text.split()
    if len(fields) != 5:
        raise CronSyntaxError(f"expected 5 fields, got {len(fields)}", original)

    sets = {}
    for name, raw in zip(FIELD_NAMES, fields):
        if name in ("day-of-month", "day-of-week"):
            continue
        acc: Set[int] = set()
        for part in raw.split(","):
            if not part:
                raise CronSyntaxError("empty list element", original, name, raw)
            acc |= _expand(part, name, original)
        sets[name] = frozenset(acc)

    # day-of-month
    dom_raw = fields[2]
    dom: Set[int] = set()
    dom_last: Set[int] = set()
    dom_weekday: Set[int] = set()
    dom_lw = False
    for part in dom_raw.split(","):
        up = part.upper()
        if not part:
            raise CronSyntaxError("empty list element", original, "day-of-month", dom_raw)
        if up == "L":
            dom_last.add(0)
        elif up == "LW":
            dom_lw = True
        elif re.fullmatch(r"L-\d+", up):
            n = int(up[2:])
            if n > 30:
                raise CronSyntaxError("offset must be 0-30", original, "day-of-month", part)
            dom_last.add(n)
        elif re.fullmatch(r"\d+W", up):
            dom_weekday.add(_value(up[:-1], "day-of-month", original, None))
        elif "L" in up or "W" in up or "#" in up:
            raise CronSyntaxError("unsupported special token", original, "day-of-month", part)
        else:
            dom |= _expand(part, "day-of-month", original)

    # day-of-week
    dow_raw = fields[4]
    dow: Set[int] = set()
    dow_last: Set[int] = set()
    dow_nth: Set[Tuple[int, int]] = set()
    for part in dow_raw.split(","):
        up = part.upper()
        if not part:
            raise CronSyntaxError("empty list element", original, "day-of-week", dow_raw)
        m_last = re.fullmatch(r"([0-7]|[A-Z]{3})L", up)
        m_nth = re.fullmatch(r"([0-7]|[A-Z]{3})#(\d+)", up)
        if m_last:
            dow_last.add(_value(m_last.group(1), "day-of-week", original, DOW_NAMES) % 7)
        elif m_nth:
            k = int(m_nth.group(2))
            if not 1 <= k <= 5:
                raise CronSyntaxError("occurrence after '#' must be 1-5", original,
                                      "day-of-week", part)
            wd = _value(m_nth.group(1), "day-of-week", original, DOW_NAMES) % 7
            dow_nth.add((wd, k))
        elif "#" in up or (up.endswith("L") and len(up) > 1):
            raise CronSyntaxError("unsupported special token", original, "day-of-week", part)
        else:
            dow |= _expand(part, "day-of-week", original)

    return CronSpec(
        expr=original.strip(),
        minutes=sets["minute"], hours=sets["hour"], months=sets["month"],
        dom=frozenset(dom), dom_last=frozenset(dom_last),
        dom_weekday=frozenset(dom_weekday), dom_last_weekday=dom_lw,
        dow=frozenset(dow), dow_last=frozenset(dow_last), dow_nth=frozenset(dow_nth),
        dom_star=dom_raw.startswith("*") or dom_raw == "?",
        dow_star=dow_raw.startswith("*") or dow_raw == "?",
        minute_star=fields[0].startswith("*"),
        hour_star=fields[1].startswith("*"),
        dom_dow=dom_dow,
        raw_fields=tuple(fields),
    )
