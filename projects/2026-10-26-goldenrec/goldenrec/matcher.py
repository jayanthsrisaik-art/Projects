"""Blocking, pairwise scoring and constrained clustering."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from itertools import combinations

from goldenrec.normalize import NormRecord
from goldenrec.similarity import jaro_winkler, soundex, token_jaccard

Key = tuple[str, str]


@dataclass(frozen=True)
class MatchConfig:
    match_threshold: float = 6.0
    review_threshold: float = 4.0
    max_block_size: int = 60
    w_email: float = 4.0
    w_email_diff: float = -1.0
    w_phone: float = 3.0
    w_phone_diff: float = -1.0
    w_dob: float = 3.0
    w_name_strong: float = 3.0
    w_name_weak: float = 1.5
    w_name_mismatch: float = -3.0
    w_address: float = 2.0
    w_zip_only: float = 0.5
    enforce_dob_constraint: bool = True


@dataclass
class BlockingStats:
    blocks: int = 0
    oversized_blocks: int = 0
    candidate_pairs: int = 0


def blocking_keys(rec: NormRecord) -> list[str]:
    """Several cheap keys; a pair is a candidate if it shares any one of them."""
    keys = []
    if rec.email:
        keys.append(f"e:{rec.email}")
    if rec.phone:
        keys.append(f"p:{rec.phone}")
    sx_first, sx_last = soundex(rec.first_canon), soundex(rec.last)
    if rec.dob and (sx_first or sx_last):
        # order-independent so swapped first/last names still collide
        names = "|".join(sorted(x for x in (sx_first, sx_last) if x))
        keys.append(f"nd:{names}:{rec.dob}")
    if sx_last and rec.zip:
        keys.append(f"lz:{sx_last}:{rec.zip}")
    if sx_first and sx_last and rec.zip:
        keys.append(f"fz:{sx_first}:{rec.zip}")  # catches surname typos that change soundex
    return keys


def candidate_pairs(records: list[NormRecord], cfg: MatchConfig) -> tuple[set[tuple[Key, Key]], BlockingStats]:
    blocks: dict[str, list[Key]] = defaultdict(list)
    for rec in records:
        for bk in blocking_keys(rec):
            blocks[bk].append(rec.key)
    stats = BlockingStats(blocks=len(blocks))
    pairs: set[tuple[Key, Key]] = set()
    for members in blocks.values():
        if len(members) > cfg.max_block_size:
            stats.oversized_blocks += 1  # e.g. a shared support@ address; skip rather than go quadratic
            continue
        for a, b in combinations(sorted(set(members)), 2):
            pairs.add((a, b))
    stats.candidate_pairs = len(pairs)
    return pairs, stats


def name_similarity(a: NormRecord, b: NormRecord) -> float | None:
    if not (a.first_canon and a.last and b.first_canon and b.last):
        return None
    straight = (jaro_winkler(a.first_canon, b.first_canon) + jaro_winkler(a.last, b.last)) / 2
    swapped = (jaro_winkler(a.first_canon, b.last) + jaro_winkler(a.last, b.first_canon)) / 2
    return max(straight, swapped)


@dataclass
class PairScore:
    a: Key
    b: Key
    score: float
    conflict: str | None = None
    reasons: list[str] = field(default_factory=list)


def score_pair(a: NormRecord, b: NormRecord, cfg: MatchConfig) -> PairScore:
    """Additive agreement/disagreement weights. Missing fields contribute nothing."""
    result = PairScore(a.key, b.key, 0.0)

    def add(weight: float, reason: str) -> None:
        result.score += weight
        result.reasons.append(f"{reason}:{weight:+g}")

    if a.dob and b.dob and a.dob != b.dob:
        result.conflict = "dob"
        result.reasons.append("dob_conflict")
        result.score = float("-inf")
        return result
    if a.email and b.email:
        if a.email == b.email:
            add(cfg.w_email, "email")
        else:
            add(cfg.w_email_diff, "email_diff")
    if a.phone and b.phone:
        if a.phone == b.phone:
            add(cfg.w_phone, "phone")
        else:
            add(cfg.w_phone_diff, "phone_diff")
    if a.dob and b.dob:
        add(cfg.w_dob, "dob")
    name = name_similarity(a, b)
    if name is not None:
        if name >= 0.92:
            add(cfg.w_name_strong, "name")
        elif name >= 0.85:
            add(cfg.w_name_weak, "name_fuzzy")
        elif name < 0.75:
            add(cfg.w_name_mismatch, "name_mismatch")
    if a.zip and b.zip and a.zip == b.zip:
        if token_jaccard(a.street, b.street) >= 0.75:
            add(cfg.w_address, "address")
        else:
            add(cfg.w_zip_only, "zip")
    return result


class ConstrainedUnionFind:
    """Union-find that refuses merges which would put two different DOBs in one cluster.

    Plain union-find chains A~B~C into one entity even when A and C clearly
    conflict (e.g. parent and child sharing a phone and address). Tracking the
    set of known DOBs per root blocks that transitive over-merge.
    """

    def __init__(self, records: list[NormRecord], enforce: bool = True):
        self.enforce = enforce
        self.parent: dict[Key, Key] = {r.key: r.key for r in records}
        self.dobs: dict[Key, set[str]] = {r.key: ({r.dob} if r.dob else set()) for r in records}
        self.blocked = 0

    def find(self, k: Key) -> Key:
        root = k
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[k] != root:
            self.parent[k], k = root, self.parent[k]
        return root

    def union(self, a: Key, b: Key) -> bool:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return True
        if self.enforce and len(self.dobs[ra] | self.dobs[rb]) > 1:
            self.blocked += 1
            return False
        if rb < ra:
            ra, rb = rb, ra
        self.parent[rb] = ra
        self.dobs[ra] |= self.dobs.pop(rb)
        return True

    def clusters(self) -> list[list[Key]]:
        groups: dict[Key, list[Key]] = defaultdict(list)
        for k in self.parent:
            groups[self.find(k)].append(k)
        return sorted((sorted(g) for g in groups.values()), key=lambda g: g[0])


@dataclass
class MatchResult:
    clusters: list[list[Key]]
    matches: list[PairScore]
    review: list[PairScore]
    blocking: BlockingStats
    blocked_merges: int
    candidate_set: set[tuple[Key, Key]]


def resolve(records: list[NormRecord], cfg: MatchConfig | None = None) -> MatchResult:
    cfg = cfg or MatchConfig()
    by_key = {r.key: r for r in records}
    if len(by_key) != len(records):
        raise ValueError("duplicate (source, source_id) in input")
    pairs, stats = candidate_pairs(records, cfg)
    scored = [score_pair(by_key[a], by_key[b], cfg) for a, b in sorted(pairs)]
    matches = [p for p in scored if p.score >= cfg.match_threshold]
    review = [p for p in scored if cfg.review_threshold <= p.score < cfg.match_threshold]
    # strongest evidence first, so a weak link can't pre-empt a strong one under the DOB constraint
    matches.sort(key=lambda p: (-p.score, p.a, p.b))
    uf = ConstrainedUnionFind(records, cfg.enforce_dob_constraint)
    for p in matches:
        uf.union(p.a, p.b)
    return MatchResult(uf.clusters(), matches, review, stats, uf.blocked, pairs)


def baseline_clusters(records: list[NormRecord]) -> list[list[Key]]:
    """Naive deterministic rule: same lowercased email OR same digits-only phone."""
    parent = {r.key: r.key for r in records}

    def find(k: Key) -> Key:
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    seen: dict[str, Key] = {}
    for r in records:
        email = r.raw["email"].lower() or None
        phone = "".join(ch for ch in r.raw["phone"] if ch.isdigit()) or None
        for token in (f"e:{email}" if email else None, f"p:{phone}" if phone else None):
            if token is None:
                continue
            if token in seen:
                ra, rb = find(seen[token]), find(r.key)
                if ra != rb:
                    parent[max(ra, rb)] = min(ra, rb)
            else:
                seen[token] = r.key
    groups: dict[Key, list[Key]] = defaultdict(list)
    for k in parent:
        groups[find(k)].append(k)
    return sorted((sorted(g) for g in groups.values()), key=lambda g: g[0])
