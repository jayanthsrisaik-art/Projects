"""SQLite-backed pipeline run: load -> resolve -> stable golden IDs -> golden records.

Golden IDs are what downstream systems join on, so they must not churn when the
clustering is recomputed. Each run re-clusters *all* records (simple and
order-independent), then maps new clusters onto previous IDs:

* a cluster keeps the previous ID held by most of its members (largest clusters claim first);
* a previous ID that no cluster keeps is **retired** with ``merged_into`` set to the
  cluster that absorbed most of its members, so old references can still be followed;
* clusters with no claimable previous ID get a fresh ID (``created``, with ``split_from``
  when its members used to belong to an ID that a bigger piece kept).
"""

from __future__ import annotations

import csv
import json
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from goldenrec.matcher import MatchConfig, resolve
from goldenrec.normalize import FIELDS, normalize_record
from goldenrec.survivorship import FIELD_RULES, build_golden

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS source_records (
    source TEXT NOT NULL, source_id TEXT NOT NULL,
    {", ".join(f"{f} TEXT" for f in FIELDS)},
    loaded_run INTEGER NOT NULL,
    PRIMARY KEY (source, source_id)
);
CREATE TABLE IF NOT EXISTS runs (
    run_id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, input_rows INTEGER,
    records INTEGER, clusters INTEGER, candidate_pairs INTEGER, matches INTEGER,
    review_pairs INTEGER, blocked_merges INTEGER, oversized_blocks INTEGER
);
CREATE TABLE IF NOT EXISTS golden_ids (
    golden_id TEXT PRIMARY KEY, status TEXT NOT NULL CHECK (status IN ('active', 'retired')),
    merged_into TEXT, created_run INTEGER NOT NULL, retired_run INTEGER
);
CREATE TABLE IF NOT EXISTS crosswalk (
    source TEXT NOT NULL, source_id TEXT NOT NULL, golden_id TEXT NOT NULL,
    PRIMARY KEY (source, source_id)
);
CREATE TABLE IF NOT EXISTS golden_records (
    golden_id TEXT PRIMARY KEY,
    {", ".join(f"{f} TEXT" for f in FIELD_RULES)},
    member_count INTEGER NOT NULL, lineage TEXT NOT NULL, run_id INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS id_events (
    run_id INTEGER NOT NULL, golden_id TEXT NOT NULL, event TEXT NOT NULL, detail TEXT
);
CREATE TABLE IF NOT EXISTS review_queue (
    run_id INTEGER NOT NULL, a_source TEXT, a_id TEXT, b_source TEXT, b_id TEXT,
    score REAL, reasons TEXT
);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.executescript(SCHEMA)
    return conn


def read_csv(path: str | Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


@dataclass
class RunSummary:
    run_id: int
    input_rows: int
    records: int
    clusters: int
    candidate_pairs: int
    matches: int
    review_pairs: int
    blocked_merges: int
    oversized_blocks: int
    events: Counter = field(default_factory=Counter)

    def as_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k != "events"}
        d["events"] = dict(sorted(self.events.items()))
        return d


def _next_id(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT v FROM meta WHERE k = 'next_golden'").fetchone()
    n = int(row[0]) if row else 1
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('next_golden', ?)", (str(n + 1),))
    return f"G{n:06d}"


def upsert_sources(conn: sqlite3.Connection, rows: list[dict], run_id: int) -> None:
    """Insert or replace source rows; an older ``updated_at`` never overwrites a newer one."""
    cols = ["source", "source_id", *FIELDS, "loaded_run"]
    updates = ", ".join(f"{c} = excluded.{c}" for c in cols[2:])
    sql = (
        f"INSERT INTO source_records ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))}) "
        f"ON CONFLICT (source, source_id) DO UPDATE SET {updates} "
        "WHERE excluded.updated_at >= source_records.updated_at"
    )
    for row in rows:
        rec = normalize_record(row)  # validates required keys before anything is written
        conn.execute(sql, [rec.source, rec.source_id, *(rec.raw[f] for f in FIELDS), run_id])


def run_pipeline(db_path: str | Path, input_rows: list[dict], cfg: MatchConfig | None = None) -> RunSummary:
    conn = connect(db_path)
    try:
        with conn:  # one transaction: a failed run leaves the previous state intact
            return _run(conn, input_rows, cfg or MatchConfig())
    finally:
        conn.close()


def _run(conn: sqlite3.Connection, input_rows: list[dict], cfg: MatchConfig) -> RunSummary:
    run_id = conn.execute("SELECT COALESCE(MAX(run_id), 0) + 1 FROM runs").fetchone()[0]
    upsert_sources(conn, input_rows, run_id)

    cols = ["source", "source_id", *FIELDS]
    rows = [dict(zip(cols, r)) for r in conn.execute(
        f"SELECT {', '.join(cols)} FROM source_records ORDER BY source, source_id")]
    records = [normalize_record(r) for r in rows]
    by_key = {r.key: r for r in records}
    result = resolve(records, cfg)

    prior: dict[tuple[str, str], str] = {
        (s, i): g for s, i, g in conn.execute("SELECT source, source_id, golden_id FROM crosswalk")}
    prior_members: dict[str, set] = {}
    for k, g in prior.items():
        prior_members.setdefault(g, set()).add(k)

    events: list[tuple[str, str, str | None]] = []
    claimed: set[str] = set()
    assignment: dict[int, str] = {}
    order = sorted(range(len(result.clusters)), key=lambda i: (-len(result.clusters[i]), result.clusters[i][0]))
    for idx in order:
        members = result.clusters[idx]
        votes = Counter(prior[k] for k in members if k in prior)
        keep = next((g for g, _ in sorted(votes.items(), key=lambda gv: (-gv[1], gv[0])) if g not in claimed), None)
        if keep is None:
            keep = _next_id(conn)
            conn.execute("INSERT INTO golden_ids VALUES (?, 'active', NULL, ?, NULL)", (keep, run_id))
            split_from = sorted(votes)
            events.append((keep, "created", json.dumps({"split_from": split_from}) if split_from else None))
        elif set(members) != prior_members[keep]:
            events.append((keep, "updated", json.dumps({"members": len(members), "was": len(prior_members[keep])})))
        claimed.add(keep)
        assignment[idx] = keep

    key_to_new = {k: assignment[i] for i, members in enumerate(result.clusters) for k in members}
    for old_id in sorted(set(prior_members) - claimed):
        heirs = Counter(key_to_new[k] for k in prior_members[old_id] if k in key_to_new)
        into = min(heirs, key=lambda g: (-heirs[g], g)) if heirs else None
        conn.execute("UPDATE golden_ids SET status = 'retired', merged_into = ?, retired_run = ? WHERE golden_id = ?",
                     (into, run_id, old_id))
        events.append((old_id, "merged", json.dumps({"into": into})))

    conn.execute("DELETE FROM crosswalk")
    conn.executemany("INSERT INTO crosswalk VALUES (?, ?, ?)", [(*k, g) for k, g in sorted(key_to_new.items())])
    conn.execute("DELETE FROM golden_records")
    for idx, members in enumerate(result.clusters):
        golden = build_golden([by_key[k] for k in members])
        conn.execute(
            f"INSERT INTO golden_records VALUES ({', '.join('?' * (len(FIELD_RULES) + 4))})",
            [assignment[idx], *(golden.values[f] for f in FIELD_RULES), len(members),
             json.dumps(golden.lineage, sort_keys=True), run_id],
        )
    conn.executemany("INSERT INTO id_events VALUES (?, ?, ?, ?)", [(run_id, *e) for e in events])
    conn.executemany(
        "INSERT INTO review_queue VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(run_id, *p.a, *p.b, p.score, ",".join(p.reasons)) for p in result.review],
    )
    summary = RunSummary(
        run_id=run_id, input_rows=len(input_rows), records=len(records), clusters=len(result.clusters),
        candidate_pairs=result.blocking.candidate_pairs, matches=len(result.matches),
        review_pairs=len(result.review), blocked_merges=result.blocked_merges,
        oversized_blocks=result.blocking.oversized_blocks, events=Counter(e[1] for e in events),
    )
    conn.execute(
        "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (run_id, datetime.now(timezone.utc).isoformat(timespec="seconds"), summary.input_rows, summary.records,
         summary.clusters, summary.candidate_pairs, summary.matches, summary.review_pairs,
         summary.blocked_merges, summary.oversized_blocks),
    )
    return summary


def lookup(conn: sqlite3.Connection, golden_id: str) -> str | None:
    """Follow ``merged_into`` links from any (possibly retired) ID to the current active ID."""
    seen = set()
    current = golden_id
    while current not in seen:
        seen.add(current)
        row = conn.execute("SELECT status, merged_into FROM golden_ids WHERE golden_id = ?", (current,)).fetchone()
        if row is None:
            return None
        if row[0] == "active":
            return current
        current = row[1]
    raise RuntimeError(f"merge cycle at {golden_id}")


def export_golden(conn: sqlite3.Connection, path: str | Path) -> int:
    cols = ["golden_id", *FIELD_RULES, "member_count"]
    rows = conn.execute(f"SELECT {', '.join(cols)} FROM golden_records ORDER BY golden_id").fetchall()
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(cols)
        writer.writerows(rows)
    return len(rows)
