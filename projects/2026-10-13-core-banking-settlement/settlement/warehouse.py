"""Warehouse build: SCD Type 2 dimensions, trial-balance audit, gated fact load."""
import hashlib
import sqlite3

OPEN_END = "9999-12-31"

DDL = """
CREATE TABLE IF NOT EXISTS dim_member (
  member_key INTEGER PRIMARY KEY AUTOINCREMENT, member_id TEXT, branch_id TEXT, region TEXT,
  credit_band TEXT, address TEXT, share_accounts INT, row_hash TEXT,
  valid_from TEXT, valid_to TEXT, is_current INT);
CREATE TABLE IF NOT EXISTS dim_loan_account (
  loan_key INTEGER PRIMARY KEY AUTOINCREMENT, loan_id TEXT, member_id TEXT, product TEXT, rate_tier TEXT,
  delinquency_bucket TEXT, balance_cents INT, row_hash TEXT, valid_from TEXT, valid_to TEXT, is_current INT);
CREATE TABLE IF NOT EXISTS audit_batch (
  batch_id TEXT PRIMARY KEY, txn_date TEXT, debit_cents INT, credit_cents INT, diff_cents INT,
  control_debit_cents INT, control_credit_cents INT, status TEXT);
CREATE TABLE IF NOT EXISTS exception_log (id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id TEXT, reason TEXT);
CREATE TABLE IF NOT EXISTS notification_log (id INTEGER PRIMARY KEY AUTOINCREMENT, message TEXT);
CREATE TABLE IF NOT EXISTS fact_member_transactions (
  txn_id TEXT PRIMARY KEY, batch_id TEXT, account_id TEXT, member_key INT, entry_type TEXT,
  amount_cents INT, txn_date TEXT);
CREATE INDEX IF NOT EXISTS ix_fact_date ON fact_member_transactions(txn_date, account_id);
"""


def credit_band(score: int) -> str:
    return "poor" if score < 620 else "fair" if score < 680 else "good" if score < 740 else "excellent"


def delinquency_bucket(dpd: int) -> str:
    return "current" if dpd < 30 else "30" if dpd < 60 else "60" if dpd < 90 else "90+"


def _hash(*vals) -> str:
    return hashlib.sha256("|".join(map(str, vals)).encode()).hexdigest()


def _scd2(conn, table, key_col, bk, attrs: dict, tracked_hash: str, snap: str):
    """Insert a new current row, or close the old one and insert a new version when the hash changed.

    Validity is half-open: [valid_from, valid_to).
    """
    cur = conn.execute(f"SELECT rowid AS rid, row_hash, valid_from FROM {table} WHERE {key_col}=? AND is_current=1", (bk,)).fetchone()
    if cur and (cur["row_hash"] == tracked_hash or snap <= cur["valid_from"]):
        return  # unchanged, or an already-processed snapshot being replayed (keeps reruns idempotent)
    if cur:
        conn.execute(f"UPDATE {table} SET valid_to=?, is_current=0 WHERE rowid=?", (snap, cur["rid"]))
    cols = {key_col: bk, **attrs, "row_hash": tracked_hash, "valid_from": snap, "valid_to": OPEN_END, "is_current": 1}
    conn.execute(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", list(cols.values()))


def build_dimensions(conn) -> None:
    conn.executescript(DDL)
    with conn:
        for r in conn.execute("SELECT * FROM stg_members ORDER BY snapshot_date, member_id").fetchall():
            band = credit_band(r["credit_score"])
            # Tracked attributes: branch, region, credit band, address. Score changes inside a band are ignored.
            h = _hash(r["branch_id"], r["region"], band, r["address"], r["share_accounts"])
            _scd2(conn, "dim_member", "member_id", r["member_id"],
                  {"branch_id": r["branch_id"], "region": r["region"], "credit_band": band,
                   "address": r["address"], "share_accounts": r["share_accounts"]}, h, r["snapshot_date"])
        for r in conn.execute("SELECT * FROM stg_loans ORDER BY snapshot_date, loan_id").fetchall():
            b = delinquency_bucket(r["days_past_due"])
            h = _hash(r["product"], r["rate_tier"], b, r["balance_cents"])
            _scd2(conn, "dim_loan_account", "loan_id", r["loan_id"],
                  {"member_id": r["member_id"], "product": r["product"], "rate_tier": r["rate_tier"],
                   "delinquency_bucket": b, "balance_cents": r["balance_cents"]}, h, r["snapshot_date"])


def audit_batches(conn) -> list[dict]:
    """Trial balance per batch: sum(D) must equal sum(C) to the cent AND match the control file."""
    conn.executescript(DDL)
    rows = conn.execute("""
        SELECT j.batch_id, MIN(j.txn_date) AS txn_date,
               SUM(CASE WHEN entry_type='D' THEN amount_cents ELSE 0 END) AS d,
               SUM(CASE WHEN entry_type='C' THEN amount_cents ELSE 0 END) AS c,
               k.control_debit_cents AS kd, k.control_credit_cents AS kc
        FROM stg_journal j LEFT JOIN stg_controls k USING (batch_id) GROUP BY j.batch_id ORDER BY j.batch_id
    """).fetchall()
    out = []
    with conn:
        for r in rows:
            diff = r["d"] - r["c"]
            if diff != 0:
                status, why = "UNBALANCED", f"debits-credits = {diff} cents"
            elif r["kd"] is None:
                status, why = "NO_CONTROL", "no control record"
            elif (r["d"], r["c"]) != (r["kd"], r["kc"]):
                status, why = "CONTROL_MISMATCH", "ledger totals differ from control file"
            else:
                status, why = "BALANCED", None
            conn.execute("INSERT OR REPLACE INTO audit_batch VALUES (?,?,?,?,?,?,?,?)",
                         (r["batch_id"], r["txn_date"], r["d"], r["c"], diff, r["kd"], r["kc"], status))
            conn.execute("DELETE FROM exception_log WHERE batch_id=?", (r["batch_id"],))
            if why:
                conn.execute("INSERT INTO exception_log (batch_id, reason) VALUES (?,?)", (r["batch_id"], f"{status}: {why}"))
            out.append({"batch_id": r["batch_id"], "status": status})
        bad = [o["batch_id"] for o in out if o["status"] != "BALANCED"]
        if bad:
            conn.execute("INSERT INTO notification_log (message) VALUES (?)",
                         (f"{len(bad)} settlement batch(es) failed reconciliation and are held back: {', '.join(bad)}",))
    return out


def load_facts(conn) -> int:
    """Only BALANCED batches reach the fact table. Member key is the version valid on txn_date."""
    with conn:
        cur = conn.execute("""
            INSERT OR IGNORE INTO fact_member_transactions
            SELECT j.txn_id, j.batch_id, j.account_id,
                   (SELECT m.member_key FROM dim_member m WHERE m.member_id = j.member_id
                      AND m.valid_from <= j.txn_date AND j.txn_date < m.valid_to),
                   j.entry_type, j.amount_cents, j.txn_date
            FROM stg_journal j JOIN audit_batch a USING (batch_id) WHERE a.status='BALANCED'
        """)
    return cur.rowcount


def run_pipeline(conn, folder) -> dict:
    from .ingest import ingest
    summary = ingest(conn, folder)
    build_dimensions(conn)
    audit = audit_batches(conn)
    summary["facts_loaded"] = load_facts(conn)
    summary["audit"] = {a["batch_id"]: a["status"] for a in audit}
    return summary
