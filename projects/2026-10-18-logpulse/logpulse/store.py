"""SQLite storage and SQL-side per-minute feature aggregation."""
import sqlite3
from collections.abc import Iterable

from .parsing import parse_line

SCHEMA = """
CREATE TABLE IF NOT EXISTS logs(
  ts INTEGER NOT NULL, service TEXT NOT NULL, level TEXT NOT NULL,
  template_id TEXT NOT NULL, latency_ms REAL, msg TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_logs_ts ON logs(ts);
CREATE TABLE IF NOT EXISTS templates(
  template_id TEXT PRIMARY KEY, template TEXT NOT NULL, first_seen INTEGER NOT NULL);
"""


def connect(path: str = ":memory:") -> sqlite3.Connection:
    # check_same_thread off: FastAPI runs sync endpoints in a worker thread pool.
    con = sqlite3.connect(path, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def ingest(con: sqlite3.Connection, lines: Iterable[str]) -> dict:
    from .templates import mine_template

    ok = bad = 0
    for line in lines:
        rec = parse_line(line)
        if rec is None:
            bad += 1
            continue
        con.execute("INSERT INTO logs VALUES(?,?,?,?,?,?)",
                    (rec.ts, rec.service, rec.level, rec.template_id, rec.latency_ms, rec.msg))
        con.execute("INSERT INTO templates VALUES(?,?,?) ON CONFLICT(template_id) DO UPDATE "
                    "SET first_seen=MIN(first_seen, excluded.first_seen)",
                    (rec.template_id, mine_template(rec.msg), rec.ts))
        ok += 1
    con.commit()
    return {"ingested": ok, "rejected": bad}


def window_rows(con: sqlite3.Connection, known_before: int) -> list[sqlite3.Row]:
    """One row per minute that has logs. A template counts as *new* when it was first
    seen at or after `known_before` (the end of the training period), so the novelty
    feature never uses vocabulary learned from the evaluation period."""
    return con.execute(
        """
        SELECT l.ts / 60 AS win,
               COUNT(*) AS n,
               SUM(l.level = 'ERROR') AS n_err,
               SUM(t.first_seen >= :kb) AS n_new,
               COUNT(DISTINCT l.template_id) AS n_tpl,
               AVG(l.latency_ms) AS lat_mean,
               MAX(l.latency_ms) AS lat_max
        FROM logs l JOIN templates t USING(template_id)
        GROUP BY win ORDER BY win
        """, {"kb": known_before}).fetchall()
