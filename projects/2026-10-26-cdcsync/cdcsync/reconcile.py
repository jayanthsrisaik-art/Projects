"""Source-vs-target reconciliation with bucketed checksums.

Rows are hashed into `buckets` by primary key. A digest per bucket is
compared first; only mismatched buckets are diffed row by row, so the report
points at the exact keys and columns that disagree.

Both databases are read locally here. In a production setting the bucket
digests would be computed inside each database so only digests cross the wire.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from .applier import q


def _rows(conn: sqlite3.Connection, relation: str, pk: list[str]) -> dict[tuple, dict]:
    cur = conn.execute(f"SELECT * FROM {q(relation)}")
    cols = [d[0] for d in cur.description]
    out = {}
    for r in cur:
        row = dict(zip(cols, r))
        out[tuple(row[c] for c in pk)] = row
    return out


def _canon(row: dict, cols: list[str]) -> str:
    return json.dumps([row.get(c) for c in cols], sort_keys=True, default=str)


def _bucket(key: tuple, buckets: int) -> int:
    h = hashlib.sha256(json.dumps(list(key), default=str).encode()).digest()
    return int.from_bytes(h[:4], "big") % buckets


def _digests(rows: dict[tuple, dict], cols: list[str], buckets: int) -> dict[int, str]:
    acc: dict[int, list[str]] = {}
    for key in sorted(rows, key=lambda k: json.dumps(list(k), default=str)):
        acc.setdefault(_bucket(key, buckets), []).append(_canon(rows[key], cols))
    return {b: hashlib.sha256("\n".join(v).encode()).hexdigest() for b, v in acc.items()}


def reconcile(source_db: str | Path, target_db: str | Path, buckets: int = 16,
              sample_limit: int = 20) -> dict:
    src = sqlite3.connect(f"file:{source_db}?mode=ro", uri=True)
    tgt = sqlite3.connect(f"file:{target_db}?mode=ro", uri=True)
    try:
        src_tables = [r[0] for r in src.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        known = dict(tgt.execute("SELECT name, pk_cols FROM _cdc_tables"))
        report: dict = {"ok": True, "tables": {}}
        for table in sorted(set(src_tables) | set(known)):
            entry: dict = {}
            report["tables"][table] = entry
            if table not in src_tables or table not in known:
                entry["error"] = "missing in source" if table not in src_tables else "missing in target"
                report["ok"] = False
                continue
            pk = json.loads(known[table])
            s_rows = _rows(src, table, pk)
            t_rows = _rows(tgt, "v_" + table, pk)
            src_cols = [r[1] for r in src.execute(f"PRAGMA table_info({q(table)})")]
            tgt_cols = [r[1] for r in tgt.execute(f"PRAGMA table_info({q('v_' + table)})")]
            cols = sorted(set(src_cols) | set(tgt_cols))
            s_dig, t_dig = _digests(s_rows, cols, buckets), _digests(t_rows, cols, buckets)
            bad = sorted(b for b in set(s_dig) | set(t_dig) if s_dig.get(b) != t_dig.get(b))
            missing, extra, diff = [], [], []
            for key in set(s_rows) | set(t_rows):
                if _bucket(key, buckets) not in bad:
                    continue
                if key not in t_rows:
                    missing.append(key)
                elif key not in s_rows:
                    extra.append(key)
                else:
                    cols_off = [c for c in cols if s_rows[key].get(c) != t_rows[key].get(c)]
                    if cols_off:
                        diff.append({"key": list(key), "columns": cols_off})
            entry.update({
                "source_rows": len(s_rows),
                "target_rows": len(t_rows),
                "buckets_checked": buckets,
                "buckets_mismatched": len(bad),
                "missing_in_target": [list(k) for k in sorted(missing)][:sample_limit],
                "extra_in_target": [list(k) for k in sorted(extra)][:sample_limit],
                "value_mismatches": sorted(diff, key=lambda d: d["key"])[:sample_limit],
                "missing_count": len(missing),
                "extra_count": len(extra),
                "mismatch_count": len(diff),
            })
            if bad:
                report["ok"] = False
        return report
    finally:
        src.close()
        tgt.close()
