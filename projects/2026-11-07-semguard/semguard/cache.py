"""SQLite-backed semantic cache with exact, semantic and guarded lookup."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np

from .embed import HashingEmbedder
from .facts import GUARDS, Facts, Lexicon, conflict, extract_facts
from .text import normalize

_SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL,
    prompt TEXT NOT NULL,
    norm TEXT NOT NULL,
    response TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}',
    vec BLOB NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    last_used_at REAL NOT NULL,
    hits INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_entries_scope_norm ON entries(scope, norm);
CREATE TABLE IF NOT EXISTS counters (scope TEXT NOT NULL, status TEXT NOT NULL, n INTEGER NOT NULL,
                                     PRIMARY KEY (scope, status));
"""


def scope_key(model: str, system: str = "", tenant: str = "", params: Optional[dict] = None) -> str:
    """Answers are only reusable under the same model, system prompt, tenant and sampling params."""
    blob = json.dumps({"m": model, "s": system, "t": tenant, "p": params or {}}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


@dataclass(frozen=True)
class CacheConfig:
    threshold: float = 0.68          # min cosine similarity; default = dev-calibrated value (README)
    top_k: int = 5                   # candidates checked against the guards, best first
    ttl_seconds: float = 7 * 86400
    max_entries_per_scope: int = 50_000
    semantic: bool = True            # False => normalised exact match only
    guards: tuple[str, ...] = GUARDS

    def with_(self, **kw) -> "CacheConfig":
        return CacheConfig(**{**self.__dict__, **kw})


@dataclass
class Lookup:
    status: str                      # "hit" | "miss" | "bypass"
    score: float = 0.0
    reason: str = ""
    entry: Optional[dict] = None
    rejected: list[dict] = field(default_factory=list)

    @property
    def hit(self) -> bool:
        return self.status == "hit"


class _ScopeIndex:
    def __init__(self, dim: int):
        self.ids: list[int] = []
        self.mat = np.zeros((0, dim), dtype=np.float32)
        self.facts: list[Facts] = []
        self.expires: list[float] = []


class SemanticCache:
    def __init__(self, path: str = ":memory:", config: Optional[CacheConfig] = None,
                 embedder: Optional[HashingEmbedder] = None, lexicon: Optional[Lexicon] = None,
                 clock: Callable[[], float] = time.time):
        self.config = config or CacheConfig()
        self.embedder = embedder or HashingEmbedder()
        self.lexicon = lexicon or Lexicon.default()
        self.clock = clock
        self._lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(_SCHEMA)
        self._index: dict[str, _ScopeIndex] = {}

    # ------------------------------------------------------------------ index
    def _scope_index(self, scope: str) -> _ScopeIndex:
        idx = self._index.get(scope)
        if idx is None:
            idx = _ScopeIndex(self.embedder.dim)
            rows = self.db.execute("SELECT id, prompt, vec, expires_at FROM entries WHERE scope=? ORDER BY id",
                                   (scope,)).fetchall()
            if rows:
                idx.ids = [r["id"] for r in rows]
                idx.mat = np.vstack([np.frombuffer(r["vec"], dtype=np.float32) for r in rows])
                idx.facts = [extract_facts(r["prompt"], self.lexicon) for r in rows]
                idx.expires = [r["expires_at"] for r in rows]
            self._index[scope] = idx
        return idx

    def _entry(self, entry_id: int) -> dict:
        r = self.db.execute("SELECT id, prompt, response, metadata, hits, created_at FROM entries WHERE id=?",
                            (entry_id,)).fetchone()
        return {"id": r["id"], "prompt": r["prompt"], "response": r["response"],
                "metadata": json.loads(r["metadata"]), "hits": r["hits"], "created_at": r["created_at"]}

    def _count(self, scope: str, status: str) -> None:
        self.db.execute("INSERT INTO counters(scope, status, n) VALUES (?, ?, 1) "
                        "ON CONFLICT(scope, status) DO UPDATE SET n = n + 1", (scope, status))

    def _touch(self, entry_id: int, now: float) -> None:
        self.db.execute("UPDATE entries SET hits = hits + 1, last_used_at = ? WHERE id = ?", (now, entry_id))

    # ----------------------------------------------------------------- lookup
    def lookup(self, prompt: str, scope: str = "default", now: Optional[float] = None) -> Lookup:
        now = self.clock() if now is None else now
        cfg = self.config
        with self._lock:
            result = self._lookup(prompt, scope, now, cfg)
            self._count(scope, result.status)
            self.db.commit()
            return result

    def _lookup(self, prompt: str, scope: str, now: float, cfg: CacheConfig) -> Lookup:
        facts = extract_facts(prompt, self.lexicon)
        if "volatile" in cfg.guards and facts.volatile:
            return Lookup("bypass", reason="time-sensitive wording; never served from cache")
        norm = normalize(prompt)
        row = self.db.execute("SELECT id FROM entries WHERE scope=? AND norm=? AND expires_at > ? "
                              "ORDER BY last_used_at DESC LIMIT 1", (scope, norm, now)).fetchone()
        if row is not None:
            self._touch(row["id"], now)
            return Lookup("hit", 1.0, "exact (normalised) match", self._entry(row["id"]))
        if not cfg.semantic:
            return Lookup("miss", reason="no exact match")
        idx = self._scope_index(scope)
        if not idx.ids:
            return Lookup("miss", reason="empty scope")
        q = self.embedder.embed(prompt)
        scores = idx.mat @ q
        k = min(cfg.top_k, len(idx.ids))
        top = np.argpartition(-scores, k - 1)[:k]
        top = top[np.argsort(-scores[top], kind="stable")]
        rejected: list[dict] = []
        best = float(scores[top[0]])
        for i in top:
            s = float(scores[i])
            if s < cfg.threshold:
                break
            if idx.expires[i] <= now:
                rejected.append({"id": idx.ids[i], "score": round(s, 4), "reason": "expired"})
                continue
            why = conflict(facts, idx.facts[i], cfg.guards)
            if why:
                rejected.append({"id": idx.ids[i], "score": round(s, 4), "reason": why})
                continue
            self._touch(idx.ids[i], now)
            return Lookup("hit", s, "semantic match", self._entry(idx.ids[i]), rejected)
        reason = (f"best similarity {best:.3f} < threshold {cfg.threshold:.2f}" if not rejected
                  else f"{len(rejected)} candidate(s) above threshold rejected by guards")
        return Lookup("miss", best, reason, None, rejected)

    # ------------------------------------------------------------------ store
    def store(self, prompt: str, response: str, scope: str = "default", metadata: Optional[dict] = None,
              now: Optional[float] = None) -> Optional[int]:
        """Insert an answer; returns the entry id, or None if the prompt is not cacheable."""
        now = self.clock() if now is None else now
        facts = extract_facts(prompt, self.lexicon)
        if "volatile" in self.config.guards and facts.volatile:
            return None
        vec = self.embedder.embed(prompt)
        with self._lock:
            idx = self._scope_index(scope)
            expires = now + self.config.ttl_seconds
            cur = self.db.execute(
                "INSERT INTO entries(scope, prompt, norm, response, metadata, vec, created_at, expires_at, "
                "last_used_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (scope, prompt, normalize(prompt), response, json.dumps(metadata or {}), vec.tobytes(),
                 now, expires, now))
            entry_id = cur.lastrowid
            idx.ids.append(entry_id)
            idx.mat = np.vstack([idx.mat, vec[None, :]])
            idx.facts.append(facts)
            idx.expires.append(expires)
            self._evict(scope)
            self.db.commit()
            return entry_id

    def _evict(self, scope: str) -> None:
        n = self.db.execute("SELECT COUNT(*) FROM entries WHERE scope=?", (scope,)).fetchone()[0]
        excess = n - self.config.max_entries_per_scope
        if excess > 0:
            self.db.execute("DELETE FROM entries WHERE id IN (SELECT id FROM entries WHERE scope=? "
                            "ORDER BY last_used_at ASC, id ASC LIMIT ?)", (scope, excess))
            self._index.pop(scope, None)

    def purge(self, scope: Optional[str] = None, expired_only: bool = False) -> int:
        with self._lock:
            sql, args = "DELETE FROM entries WHERE 1=1", []
            if scope is not None:
                sql += " AND scope=?"
                args.append(scope)
            if expired_only:
                sql += " AND expires_at <= ?"
                args.append(self.clock())
            n = self.db.execute(sql, args).rowcount
            self.db.commit()
            self._index.clear()
            return n

    def stats(self, scope: Optional[str] = None) -> dict:
        with self._lock:
            where, args = ("WHERE scope=?", (scope,)) if scope else ("", ())
            entries = self.db.execute(f"SELECT COUNT(*) FROM entries {where}", args).fetchone()[0]
            counts = {r["status"]: r["n"] for r in self.db.execute(
                f"SELECT status, SUM(n) AS n FROM counters {where} GROUP BY status", args)}
            lookups = sum(counts.values())
            return {"entries": entries, "lookups": lookups, **{k: counts.get(k, 0) for k in ("hit", "miss", "bypass")},
                    "hit_rate": round(counts.get("hit", 0) / lookups, 4) if lookups else 0.0}

    def close(self) -> None:
        self.db.close()
