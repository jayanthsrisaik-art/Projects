"""FastAPI layer: thin request models over ``Store``, plus the static UI."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .store import BookingError, BookingRequest, Store

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"


class ResourceIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    timezone: str
    open_time: str = "08:00"
    close_time: str = "18:00"
    open_days: list[str] = ["MO", "TU", "WE", "TH", "FR"]
    buffer_minutes: int = 0


class BookingIn(BaseModel):
    resource_id: int
    title: str = Field(min_length=1, max_length=120)
    organizer: str = Field(min_length=1, max_length=80)
    date: str
    start_time: str
    duration_minutes: int
    rrule: str | None = None
    on_conflict: Literal["reject", "skip"] = "reject"


class MoveIn(BaseModel):
    date: str
    start_time: str
    duration_minutes: int | None = None


class CancelIn(BaseModel):
    scope: Literal["this", "following", "series"] = "this"


def create_app(db_path: str | None = None) -> FastAPI:
    store = Store(db_path or os.environ.get("BOOKGRID_DB", "bookgrid.db"))
    app = FastAPI(title="bookgrid", version="0.1.0")
    app.state.store = store

    @app.exception_handler(BookingError)
    async def booking_error(_: Request, exc: BookingError) -> JSONResponse:
        body = {"error": exc.message}
        if exc.details is not None:
            body["details"] = exc.details
        return JSONResponse(status_code=exc.status, content=body)

    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True}

    @app.get("/api/resources")
    def list_resources() -> list[dict]:
        return [r.as_dict() for r in store.list_resources()]

    @app.post("/api/resources", status_code=201)
    def create_resource(body: ResourceIn) -> dict:
        return store.create_resource(
            body.name, body.timezone, body.open_time, body.close_time,
            body.open_days, body.buffer_minutes,
        ).as_dict()

    @app.get("/api/resources/{rid}/occurrences")
    def occurrences(rid: int, start: str, end: str, include_cancelled: bool = False) -> list[dict]:
        return store.list_occurrences(rid, start, end, include_cancelled)

    @app.get("/api/resources/{rid}/free")
    def free(rid: int, date: str, duration: int = Query(30), step: int = Query(15)) -> dict:
        return store.free_slots(rid, date, duration, step)

    @app.post("/api/bookings/preview")
    def preview(body: BookingIn) -> dict:
        return store.preview(BookingRequest.from_dict(body.model_dump()))

    @app.post("/api/bookings", status_code=201)
    def book(body: BookingIn, idempotency_key: str | None = Header(default=None)) -> JSONResponse:
        if idempotency_key is not None and not 8 <= len(idempotency_key) <= 128:
            raise HTTPException(422, "Idempotency-Key must be 8-128 characters")
        data = body.model_dump()
        status, out, replayed = store.book(BookingRequest.from_dict(data), idempotency_key, data)
        headers = {"Idempotent-Replayed": "true"} if replayed else {}
        return JSONResponse(status_code=status, content=out, headers=headers)

    @app.get("/api/series/{sid}")
    def series(sid: int) -> dict:
        return store.get_series(sid)

    @app.get("/api/occurrences/{oid}")
    def get_occurrence(oid: int) -> dict:
        return store.get_occurrence(oid)

    @app.patch("/api/occurrences/{oid}")
    def move(oid: int, body: MoveIn) -> dict:
        return store.move(oid, body.date, body.start_time, body.duration_minutes)

    @app.post("/api/occurrences/{oid}/cancel")
    def cancel(oid: int, body: CancelIn) -> dict:
        return store.cancel(oid, body.scope)

    dist = FRONTEND / "dist"
    if (dist / "src").is_dir():
        app.mount("/static", StaticFiles(directory=dist / "src"), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        if not (dist / "src").is_dir():
            return JSONResponse({"message": "UI not built: run `npm install && npm run build` in frontend/"})
        return FileResponse(FRONTEND / "index.html")

    @app.get("/styles.css", include_in_schema=False)
    def styles():
        return FileResponse(FRONTEND / "styles.css")

    return app
