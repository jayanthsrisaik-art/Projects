"""SQLite persistence: endpoints, and a delivery table that acts as a transactional outbox."""
import json
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS endpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL,
    secret TEXT NOT NULL,
    event_types TEXT NOT NULL            -- JSON list; ["*"] matches everything
);
CREATE TABLE IF NOT EXISTS deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    endpoint_id INTEGER NOT NULL REFERENCES endpoints(id),
    idempotency_key TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload TEXT NOT NULL,               -- canonical JSON body that is signed and sent
    status TEXT NOT NULL DEFAULT 'pending',  -- pending | delivered | dead
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at INTEGER NOT NULL,
    last_status_code INTEGER,
    last_error TEXT,
    created_at INTEGER NOT NULL,
    UNIQUE (endpoint_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_due ON deliveries(status, next_attempt_at);
"""


def connect(path: str = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def add_endpoint(conn, url: str, secret: str, event_types: list[str]) -> int:
    cur = conn.execute(
        "INSERT INTO endpoints (url, secret, event_types) VALUES (?, ?, ?)",
        (url, secret, json.dumps(event_types)),
    )
    conn.commit()
    return cur.lastrowid


def enqueue_event(conn, idempotency_key: str, event_type: str, data: dict, now: int) -> list[int]:
    """Fan an event out to matching endpoints. Re-sending the same key is a no-op per endpoint.

    Returns ids of newly created deliveries.
    """
    body = json.dumps(
        {"type": event_type, "idempotency_key": idempotency_key, "data": data},
        sort_keys=True, separators=(",", ":"),
    )
    created = []
    with conn:
        for ep in conn.execute("SELECT id, event_types FROM endpoints").fetchall():
            types = json.loads(ep["event_types"])
            if "*" not in types and event_type not in types:
                continue
            cur = conn.execute(
                "INSERT OR IGNORE INTO deliveries "
                "(endpoint_id, idempotency_key, event_type, payload, next_attempt_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (ep["id"], idempotency_key, event_type, body, now, now),
            )
            if cur.rowcount:
                created.append(cur.lastrowid)
    return created


def due_deliveries(conn, now: int, limit: int):
    return conn.execute(
        "SELECT d.*, e.url, e.secret FROM deliveries d JOIN endpoints e ON e.id = d.endpoint_id "
        "WHERE d.status = 'pending' AND d.next_attempt_at <= ? ORDER BY d.next_attempt_at, d.id LIMIT ?",
        (now, limit),
    ).fetchall()


def get_delivery(conn, delivery_id: int):
    return conn.execute("SELECT * FROM deliveries WHERE id = ?", (delivery_id,)).fetchone()


def list_deliveries(conn, status: str | None = None):
    if status:
        return conn.execute("SELECT * FROM deliveries WHERE status = ? ORDER BY id", (status,)).fetchall()
    return conn.execute("SELECT * FROM deliveries ORDER BY id").fetchall()
