"""Landing-directory ingestion: checksum validation, parse to staging, redirect bad rows."""
import hashlib
import sqlite3
from pathlib import Path

from .money import to_cents

STG = """
CREATE TABLE IF NOT EXISTS stg_journal (batch_id, txn_id PRIMARY KEY, account_id, member_id, entry_type, amount_cents INT, txn_date);
CREATE TABLE IF NOT EXISTS stg_controls (batch_id PRIMARY KEY, control_debit_cents INT, control_credit_cents INT);
CREATE TABLE IF NOT EXISTS stg_members (snapshot_date, member_id, branch_id, region, credit_score INT, address, share_accounts INT);
CREATE TABLE IF NOT EXISTS stg_loans (snapshot_date, loan_id, member_id, product, rate_tier, balance_cents INT, days_past_due INT);
CREATE TABLE IF NOT EXISTS stg_rejects (source_file, line_no INT, raw_line, reason);
CREATE TABLE IF NOT EXISTS file_log (file_name PRIMARY KEY, sha256, status);
"""


def verify_manifest(folder: Path) -> dict[str, bool]:
    """Return {file_name: checksum_ok}. A file missing from disk counts as failed."""
    result = {}
    for line in (folder / "MANIFEST.sha256").read_text().splitlines():
        digest, name = line.split("  ", 1)
        p = folder / name
        result[name] = p.exists() and hashlib.sha256(p.read_bytes()).hexdigest() == digest
    return result


def _rows(path: Path):
    lines = path.read_text().splitlines()
    for no, raw in enumerate(lines[1:], start=2):
        if raw.strip():
            yield no, raw, raw.split("|")


def _reject(conn, name, no, raw, reason):
    conn.execute("INSERT INTO stg_rejects VALUES (?,?,?,?)", (name, no, raw, reason))


def _load_file(conn, path: Path) -> None:
    kind, stamp = path.stem.split("_")
    snap = f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:]}"
    for no, raw, f in _rows(path):
        try:
            if kind == "journal":
                if len(f) != 7 or f[4] not in ("D", "C"):
                    raise ValueError("bad field count or entry_type")
                conn.execute("INSERT INTO stg_journal VALUES (?,?,?,?,?,?,?)",
                             (f[0], f[1], f[2], f[3] or None, f[4], to_cents(f[5]), f[6]))
            elif kind == "controls":
                conn.execute("INSERT INTO stg_controls VALUES (?,?,?)", (f[0], to_cents(f[1]), to_cents(f[2])))
            elif kind == "members":
                conn.execute("INSERT INTO stg_members VALUES (?,?,?,?,?,?,?)",
                             (snap, f[0], f[1], f[2], int(f[3]), f[4], int(f[5])))
            elif kind == "loans":
                conn.execute("INSERT INTO stg_loans VALUES (?,?,?,?,?,?,?)",
                             (snap, f[0], f[1], f[2], f[3], to_cents(f[4]), int(f[5])))
        except (ValueError, IndexError, sqlite3.IntegrityError) as exc:
            _reject(conn, path.name, no, raw, str(exc))


def ingest(conn: sqlite3.Connection, folder: Path) -> dict:
    """Load every checksum-valid file; quarantine files whose checksum fails."""
    conn.executescript(STG)
    summary = {"loaded": [], "quarantined": []}
    for name, ok in sorted(verify_manifest(folder).items()):
        digest = hashlib.sha256((folder / name).read_bytes()).hexdigest() if (folder / name).exists() else None
        if not ok:
            conn.execute("INSERT OR REPLACE INTO file_log VALUES (?,?,?)", (name, digest, "QUARANTINED"))
            summary["quarantined"].append(name)
            continue
        if conn.execute("SELECT 1 FROM file_log WHERE file_name=? AND status='LOADED'", (name,)).fetchone():
            continue  # idempotent: already loaded
        with conn:
            _load_file(conn, folder / name)
            conn.execute("INSERT OR REPLACE INTO file_log VALUES (?,?,?)", (name, digest, "LOADED"))
        summary["loaded"].append(name)
    return summary
