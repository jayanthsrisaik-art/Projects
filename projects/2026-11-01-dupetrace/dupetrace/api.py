"""FastAPI service.

POST /reports            file a report; returns duplicate suggestions, then stores + indexes it
POST /reports/preview    suggestions only, nothing stored
GET  /reports/{id}       stored report with parsed fields
POST /reports/{id}/duplicate-of   triager confirms a link (feedback for future training)
GET  /health
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from . import store
from .engine import DuplicateFinder
from .model import Reranker


class ReportIn(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=300)
    body: str = Field(min_length=1, max_length=100_000)
    component: str = "unknown"
    version: Optional[str] = None
    created_at: Optional[str] = None  # ISO-8601 with timezone; defaults to now


class Suggestion(BaseModel):
    id: str
    title: str
    created_at: str
    score: float
    reasons: list[str]


class SuggestOut(BaseModel):
    id: str
    likely_duplicate: bool
    threshold: Optional[float]
    parsed: dict
    suggestions: list[Suggestion]


class LinkIn(BaseModel):
    target_id: str


def create_app(db_path: str | Path, model_path: str | Path | None = None) -> FastAPI:
    conn = store.connect(db_path)
    model = Reranker.load(model_path) if model_path and Path(model_path).exists() else None
    finder = DuplicateFinder(model)
    for r in store.load_reports(conn):
        finder.add(r)
    lock = threading.Lock()  # one writer: SQLite connection + in-memory index
    app = FastAPI(title="dupetrace", version="0.1.0")

    def _record(body: ReportIn) -> dict:
        rec = body.model_dump()
        rec["created_at"] = rec["created_at"] or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        rec["component"] = rec["component"] or "unknown"
        reason = store.validate(rec)
        if reason:
            raise HTTPException(422, reason)
        return rec

    def _suggest(rep: store.Report) -> SuggestOut:
        ranked, likely = finder.suggest(rep, top=5)
        return SuggestOut(
            id=rep.id, likely_duplicate=likely, threshold=model.threshold if model else None,
            parsed={"exc_type": rep.exc_type, "message": rep.norm_message, "frames": list(rep.frames),
                    "component": rep.component},
            suggestions=[Suggestion(id=c.report.id, title=c.report.title, created_at=c.report.created_at,
                                    score=round(c.score, 4), reasons=c.reasons) for c in ranked],
        )

    @app.get("/health")
    def health():
        return {"status": "ok", "reports_indexed": len(finder.index), "model_loaded": model is not None}

    @app.post("/reports/preview", response_model=SuggestOut)
    def preview(body: ReportIn):
        rep = store.build_report(_record(body))
        with lock:
            return _suggest(rep)

    @app.post("/reports", response_model=SuggestOut, status_code=201)
    def create(body: ReportIn):
        rec = _record(body)
        with lock:
            existing = store.get_report(conn, rec["id"])
            if existing is not None:
                raise HTTPException(409, f"report {rec['id']} already exists")
            rep = store.build_report(rec)
            out = _suggest(rep)  # suggestions computed BEFORE the report joins the index
            with conn:
                store.insert_report(conn, rec)
            finder.add(rep)
            return out

    @app.get("/reports/{report_id}")
    def get(report_id: str):
        rep = store.get_report(conn, report_id)
        if rep is None:
            raise HTTPException(404, "not found")
        return {"id": rep.id, "title": rep.title, "created_at": rep.created_at, "component": rep.component,
                "version": rep.version, "exc_type": rep.exc_type, "message": rep.norm_message,
                "frames": list(rep.frames)}

    @app.post("/reports/{report_id}/duplicate-of", status_code=201)
    def link(report_id: str, body: LinkIn):
        with lock:
            if store.get_report(conn, report_id) is None or store.get_report(conn, body.target_id) is None:
                raise HTTPException(404, "unknown report id")
            try:
                store.link_duplicate(conn, report_id, body.target_id)
            except ValueError as e:
                raise HTTPException(422, str(e)) from e
        return {"report_id": report_id, "duplicate_of": body.target_id}

    return app
