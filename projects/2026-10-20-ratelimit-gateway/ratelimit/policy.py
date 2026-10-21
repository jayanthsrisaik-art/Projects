"""Route-based policies: longest matching path prefix wins; each policy owns one limiter."""
from __future__ import annotations

from dataclasses import dataclass, field

from .algorithms import Limiter, make_limiter


@dataclass
class Rule:
    prefix: str
    algorithm: str
    limit: int
    window: float
    limiter: Limiter = field(init=False, repr=False)

    def __post_init__(self):
        if not self.prefix.startswith("/"):
            raise ValueError(f"prefix must start with '/': {self.prefix!r}")
        self.limiter = make_limiter(self.algorithm, self.limit, self.window)


class PolicySet:
    def __init__(self, rules: list[Rule], exempt: tuple[str, ...] = ()):
        prefixes = [r.prefix for r in rules]
        if len(prefixes) != len(set(prefixes)):
            raise ValueError("duplicate rule prefixes")
        # longest prefix first so the most specific rule wins
        self.rules = sorted(rules, key=lambda r: len(r.prefix), reverse=True)
        self.exempt = exempt

    @classmethod
    def from_config(cls, cfg: dict) -> "PolicySet":
        rules = [Rule(r["prefix"], r["algorithm"], r["limit"], r["window"]) for r in cfg.get("rules", [])]
        return cls(rules, tuple(cfg.get("exempt", ())))

    def match(self, path: str) -> Rule | None:
        if any(_under(path, e) for e in self.exempt):
            return None
        for rule in self.rules:
            if _under(path, rule.prefix):
                return rule
        return None

    def evict_idle(self, now: float) -> int:
        return sum(r.limiter.evict_idle(now) for r in self.rules)


def _under(path: str, prefix: str) -> bool:
    """Segment-aware prefix test: '/api' covers '/api' and '/api/x' but not '/apiary'."""
    if prefix == "/":
        return True
    return path == prefix or path.startswith(prefix.rstrip("/") + "/")
