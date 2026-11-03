"""Static checks for cron expressions: things that parse but probably are not
what the author meant, plus DST exposure for a given time zone."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import List, Optional

from .matcher import LocalSearcher, day_matches
from .parser import BOUNDS, FIELD_NAMES, CronSpec, CronSyntaxError, parse
from .schedule import Schedule, get_tz, resolve_wall_time

MONTH_ABBR = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()


@dataclass(frozen=True)
class Finding:
    level: str   # error | warning | info
    code: str
    message: str

    def as_dict(self) -> dict:
        return {"level": self.level, "code": self.code, "message": self.message}


def _months_that_never_fire(spec: CronSpec) -> List[int]:
    """Allowed months in which no day matches across a full 400-year cycle sample.

    Checking 28 consecutive years covers every (leap-year, weekday-of-the-1st)
    combination for each month within 1901-2099.
    """
    dead = []
    for m in sorted(spec.months):
        found = False
        for y in range(2001, 2029):
            d = date(y, m, 1)
            while d.month == m:
                if day_matches(spec, d):
                    found = True
                    break
                d += timedelta(days=1)
            if found:
                break
        if not found:
            dead.append(m)
    return dead


def lint_spec(spec: CronSpec, tz: Optional[str] = None, year: Optional[int] = None) -> List[Finding]:
    out: List[Finding] = []
    first = LocalSearcher(spec).next_at_or_after(datetime(2001, 1, 1))
    if first is None:
        out.append(Finding("error", "never-fires",
                           "no date in a full 400-year Gregorian cycle matches"))
        return out

    dead = _months_that_never_fire(spec)
    if dead:
        out.append(Finding("warning", "skips-months",
                           "never fires in " + ", ".join(MONTH_ABBR[m - 1] for m in dead)
                           + " (day-of-month does not exist there)"))
    if not spec.dom_star and not spec.dow_star:
        how = ("either matches (OR)" if spec.dom_dow == "or" else "both must match (AND)")
        out.append(Finding("info", "dom-dow-combined",
                           f"day-of-month and day-of-week are both restricted: {how}"))
    for name, raw in zip(FIELD_NAMES, spec.raw_fields):
        lo, hi = BOUNDS[name]
        span = (6 if name == "day-of-week" else hi) - lo + 1
        for part in raw.split(","):
            if "/" in part:
                base, _, step = part.partition("/")
                if step.isdigit() and int(step) >= span:
                    out.append(Finding("warning", "step-noop",
                                       f"{name} step {step} >= field span {span}: "
                                       f"'{part}' matches a single value"))
                elif step.isdigit() and base in ("*", "?") and span % int(step):
                    out.append(Finding("info", "uneven-step",
                                       f"{name} '{part}' does not divide {span}: the gap "
                                       f"across the {name} boundary is shorter"))
    if tz:
        out.extend(dst_findings(spec, tz, year))
    return out


def dst_findings(spec: CronSpec, tz_name: str, year: Optional[int] = None) -> List[Finding]:
    """Report wall times of this spec that hit a DST gap/overlap in ``year``."""
    tz = get_tz(tz_name)
    year = year or datetime.now().year
    sched = Schedule(spec, tz)
    out: List[Finding] = []
    gaps, overlaps = [], []
    for occ in sched.occurrences(datetime(year, 1, 1), datetime(year + 1, 1, 1)):
        if occ.status in ("shifted", "skipped"):
            gaps.append(occ)
        elif occ.status in ("repeat", "suppressed"):
            overlaps.append(occ)
    if gaps:
        verb = "shifted to the transition" if sched.gap == "shift" else "skipped"
        days = sorted({o.local.date().isoformat() for o in gaps})
        out.append(Finding("warning", "dst-gap",
                           f"{len(gaps)} wall time(s) on {', '.join(days)} do not exist in "
                           f"{tz_name}; policy gap={sched.gap}: {verb}"))
    if overlaps:
        verb = "run twice" if sched.overlap == "twice" else "run once (first occurrence)"
        days = sorted({o.local.date().isoformat() for o in overlaps})
        out.append(Finding("info" if sched.overlap == "once" else "warning", "dst-overlap",
                           f"{len(overlaps)} wall time(s) on {', '.join(days)} occur twice in "
                           f"{tz_name}; policy overlap={sched.overlap}: {verb}"))
    return out


def lint(expr: str, tz: Optional[str] = None, year: Optional[int] = None,
         dom_dow: str = "or") -> List[Finding]:
    try:
        spec = parse(expr, dom_dow=dom_dow)
    except CronSyntaxError as e:
        return [Finding("error", "syntax", str(e))]
    return lint_spec(spec, tz, year)


__all__ = ["Finding", "lint", "lint_spec", "dst_findings", "resolve_wall_time"]
