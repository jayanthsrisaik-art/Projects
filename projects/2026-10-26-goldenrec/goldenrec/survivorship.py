"""Build one golden record per cluster, keeping per-field lineage."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from goldenrec.normalize import NormRecord, canonical_first, norm_name
from goldenrec.similarity import jaro_winkler

DEFAULT_SOURCE_PRIORITY = ("crm", "billing", "support")

FIELD_RULES = {
    "first_name": "consensus",   # typos and nicknames vary per record; take the value closest to all others
    "last_name": "consensus",
    "email": "most_recent",      # people change email; latest wins
    "phone": "most_recent",
    "dob": "most_frequent",      # constrained clustering allows at most one, but be explicit
    "street": "most_recent",     # address moves are time-ordered
    "city": "most_recent",
    "zip": "most_recent",
}


def _display_email(raw: str) -> str:
    """Lowercased, +tag removed, but dots kept: the address as the customer wrote it."""
    local, domain = raw.strip().lower().split("@")
    return f"{local.split('+', 1)[0]}@{domain}"


def _value(rec: NormRecord, fld: str) -> str | None:
    if fld in ("first_name", "last_name", "city"):
        return rec.raw[fld].strip().title() or None
    if fld == "street":
        return rec.raw["street"].strip() if rec.street else None
    if fld == "email":
        return _display_email(rec.raw["email"]) if rec.email else None
    return getattr(rec, fld)  # phone/dob/zip use normalised forms


def _compare_form(fld: str, value: str) -> str:
    """What consensus compares: nickname-folded first names, normalised surnames."""
    if fld == "first_name":
        return canonical_first(value) or ""
    return norm_name(value) or ""


@dataclass
class GoldenRecord:
    members: list[tuple[str, str]]
    values: dict[str, str | None]
    lineage: dict[str, str | None]  # field -> "source:source_id" that supplied it


def build_golden(cluster: list[NormRecord], source_priority=DEFAULT_SOURCE_PRIORITY) -> GoldenRecord:
    rank = {s: i for i, s in enumerate(source_priority)}

    def tiebreak(rv):  # source priority, then newest, then id: fully deterministic
        rec = rv[0]
        return (rank.get(rec.source, len(rank)), _desc(rec.updated_at), rec.source_id)

    values: dict[str, str | None] = {}
    lineage: dict[str, str | None] = {}
    for fld, strategy in FIELD_RULES.items():
        cands = [(rec, v) for rec in cluster if (v := _value(rec, fld))]
        if not cands:
            values[fld] = lineage[fld] = None
            continue
        if strategy == "most_recent":
            best = min(cands, key=lambda rv: (_desc(rv[0].updated_at), *tiebreak(rv)))
        elif strategy == "most_frequent":
            counts = Counter(v for _, v in cands)
            best = min(cands, key=lambda rv: (-counts[rv[1]], *tiebreak(rv)))
        elif strategy == "consensus":
            forms = [_compare_form(fld, v) for _, v in cands]

            def centrality(i: int) -> float:
                return round(sum(jaro_winkler(forms[i], f) for f in forms), 9)

            def formal(i: int) -> bool:  # "Nicholas" beats "Nick" when both fold to the same name
                return fld == "first_name" and norm_name(cands[i][1]) == forms[i]

            idx = min(range(len(cands)), key=lambda i: (-centrality(i), not formal(i), *tiebreak(cands[i])))
            best = cands[idx]
        else:  # pragma: no cover - guarded by FIELD_RULES
            raise ValueError(strategy)
        values[fld] = best[1]
        lineage[fld] = f"{best[0].source}:{best[0].source_id}"
    return GoldenRecord(sorted(r.key for r in cluster), values, lineage)


def _desc(iso: str) -> tuple:
    """Sort key that orders ISO timestamps newest first (empty sorts last)."""
    return tuple(-ord(c) for c in iso) + (1,)
