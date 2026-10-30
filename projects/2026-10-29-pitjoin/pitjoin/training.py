"""Build training sets from an entity spine and audit them for leakage."""

from __future__ import annotations

import csv
from pathlib import Path

from .engines import BASELINES, ENGINES, MODES
from .repo import EntityFrame, FeatureView, format_value

ALL_JOINS = {**ENGINES, **BASELINES}


def resolve_indices(views: dict[str, FeatureView], entities: EntityFrame,
                    engine: str = "sweep", mode: str = "knowledge") -> dict[str, list[int | None]]:
    if engine not in ALL_JOINS:
        raise ValueError(f"unknown engine {engine!r}; choose from {sorted(ALL_JOINS)}")
    fn = ALL_JOINS[engine]
    return {name: fn(v, entities.keys(v.entity), entities.ts, mode) for name, v in views.items()}


def build_training_set(views: dict[str, FeatureView], entities: EntityFrame,
                       engine: str = "sweep", mode: str = "knowledge") -> tuple[list[str], list[list]]:
    """Return (header, rows): the entity columns followed by ``view__feature`` columns.

    Row order and duplicates of the entity file are preserved; a feature view
    with no eligible row yields nulls for all its features.
    """
    chosen = resolve_indices(views, entities, engine, mode)
    header = list(entities.columns)
    for v in views.values():
        header += v.output_columns()
    rows = []
    for i, raw in enumerate(entities.rows):
        out = [raw[c] for c in entities.columns]
        for name, v in views.items():
            idx = chosen[name][i]
            out += [None] * len(v.features) if idx is None else list(v.rows[idx].values)
        rows.append(out)
    return header, rows


def write_csv(path: str | Path, header: list[str], rows: list[list]) -> None:
    with Path(path).open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(header)
        for r in rows:
            w.writerow([format_value(v) for v in r])


def leakage_report(views: dict[str, FeatureView], entities: EntityFrame,
                   strategies: tuple[str, ...] = ("naive_latest", "asof_event_only")) -> dict:
    """Compare join strategies against the bitemporal point-in-time join.

    For every (entity row, view) pair a strategy picks a row that is either
    - ``future_event``: the fact happened after the label time,
    - ``not_yet_known``: the fact happened before but landed after the label time,
    - ``stale``: older than the view's TTL,
    - or legitimate.
    ``value_mismatch`` counts pairs whose joined values differ from the
    point-in-time result (including null vs non-null).
    """
    truth = resolve_indices(views, entities, "sweep", "knowledge")
    n = len(entities.rows)
    report: dict = {"entity_rows": n, "views": list(views), "strategies": {}}
    for spec in strategies:
        engine, _, mode = spec.partition(":")
        picked = resolve_indices(views, entities, engine, mode or "knowledge")
        per_view = {}
        tot = {"pairs": 0, "future_event": 0, "not_yet_known": 0, "stale": 0, "value_mismatch": 0, "rows_affected": 0}
        affected = [False] * n
        for name, v in views.items():
            c = {"future_event": 0, "not_yet_known": 0, "stale": 0, "value_mismatch": 0}
            for i, (idx, true_idx) in enumerate(zip(picked[name], truth[name])):
                t = entities.ts[i]
                bad = False
                if idx is not None:
                    r = v.rows[idx]
                    if r.ts > t:
                        c["future_event"] += 1
                        bad = True
                    elif r.created > t:
                        c["not_yet_known"] += 1
                        bad = True
                    elif v.ttl_us is not None and r.ts < t - v.ttl_us:
                        c["stale"] += 1
                        bad = True
                got = None if idx is None else v.rows[idx].values
                want = None if true_idx is None else v.rows[true_idx].values
                if got != want:
                    c["value_mismatch"] += 1
                    bad = True
                affected[i] = affected[i] or bad
            per_view[name] = c
            for k in c:
                tot[k] += c[k]
            tot["pairs"] += n
        tot["rows_affected"] = sum(affected)
        tot["rows_affected_pct"] = round(100.0 * tot["rows_affected"] / n, 2) if n else 0.0
        report["strategies"][spec] = {"total": tot, "per_view": per_view}
    return report


__all__ = ["MODES", "build_training_set", "leakage_report", "resolve_indices", "write_csv"]
