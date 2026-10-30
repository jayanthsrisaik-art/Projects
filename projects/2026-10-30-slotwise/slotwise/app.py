"""FastAPI app: slot search, booking, cancel/reschedule, ICS, and the web UI."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from .availability import ConfigError, iso_z, parse_instant
from .ics import booking_ics
from .store import BadToken, Conflict, NotFound, SlotUnavailable, Store

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
MAX_RANGE = timedelta(days=31)
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class BookingIn(BaseModel):
    event_type: str
    start: str
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=200)

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        if not EMAIL_RE.match(v):
            raise ValueError("invalid email address")
        return v

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("name must not be blank")
        return v


class TokenIn(BaseModel):
    token: str


class RescheduleIn(BaseModel):
    token: str
    start: str


def _instant(text: str, field: str) -> datetime:
    try:
        return parse_instant(text)
    except ValueError as exc:
        raise HTTPException(422, detail={"error": "bad_instant", "field": field, "message": str(exc)})


def _err(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": code, "message": message})


def create_app(
    db_path: str = "slotwise.db",
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    seed_file: str | None = None,
) -> FastAPI:
    store = Store(db_path)
    if seed_file:
        for cfg in json.loads(Path(seed_file).read_text()):
            store.upsert_event_type(cfg)

    app = FastAPI(title="slotwise", version="1.0.0")
    app.state.store = store

    @app.exception_handler(NotFound)
    async def _nf(_, exc):
        return _err(404, "not_found", str(exc))

    @app.exception_handler(SlotUnavailable)
    async def _su(_, exc):
        return _err(409, "slot_unavailable", str(exc))

    @app.exception_handler(Conflict)
    async def _cf(_, exc):
        return _err(409, "conflict", str(exc))

    @app.exception_handler(BadToken)
    async def _bt(_, exc):
        return _err(403, "bad_token", str(exc))

    @app.exception_handler(ConfigError)
    async def _ce(_, exc):
        return _err(422, "bad_config", str(exc))

    @app.get("/api/health")
    def health():
        return {"ok": True, "now": iso_z(now())}

    @app.get("/api/event-types")
    def event_types():
        return store.list_event_types()

    @app.put("/api/event-types/{slug}")
    def put_event_type(slug: str, config: dict):
        if config.get("slug", slug) != slug:
            raise HTTPException(422, detail={"error": "slug_mismatch"})
        store.upsert_event_type({**config, "slug": slug})
        return store.get_event_type(slug).__dict__

    @app.get("/api/event-types/{slug}/slots")
    def slots(slug: str, start: str = Query(...), end: str = Query(...)):
        s, e = _instant(start, "start"), _instant(end, "end")
        if e <= s:
            raise HTTPException(422, detail={"error": "bad_range", "message": "end must be after start"})
        if e - s > MAX_RANGE:
            raise HTTPException(422, detail={"error": "bad_range", "message": "range is limited to 31 days"})
        et = store.get_event_type(slug)
        found = store.available_slots(slug, s, e, now())
        dur = timedelta(minutes=et.duration_min)
        return {
            "event_type": slug,
            "host_tz": et.host_tz,
            "duration_min": et.duration_min,
            "slots": [{"start": iso_z(x), "end": iso_z(x + dur)} for x in found],
        }

    @app.post("/api/bookings", status_code=201)
    def book(body: BookingIn, idempotency_key: str | None = Header(default=None)):
        start = _instant(body.start, "start")
        booking, token, replayed = store.book(
            body.event_type, start, body.name, body.email, now(), idempotency_key
        )
        payload = {"booking": booking.to_dict(), "token": token, "replayed": replayed}
        return JSONResponse(status_code=200 if replayed else 201, content=payload)

    @app.get("/api/bookings/{booking_id}")
    def get_booking(booking_id: str):
        return store.get_booking(booking_id).to_dict()

    @app.get("/api/bookings/{booking_id}/ics")
    def get_ics(booking_id: str):
        b = store.get_booking(booking_id)
        et = store.get_event_type(b.event_type)
        return Response(
            booking_ics(b, et),
            media_type="text/calendar",
            headers={"Content-Disposition": f'attachment; filename="{b.id}.ics"'},
        )

    @app.post("/api/bookings/{booking_id}/cancel")
    def cancel(booking_id: str, body: TokenIn):
        return store.cancel(booking_id, body.token).to_dict()

    @app.post("/api/bookings/{booking_id}/reschedule")
    def reschedule(booking_id: str, body: RescheduleIn):
        new, token = store.reschedule(booking_id, body.token, _instant(body.start, "start"), now())
        return {"booking": new.to_dict(), "token": token, "previous_id": booking_id}

    if (WEB_DIR / "dist").is_dir():
        app.mount("/dist", StaticFiles(directory=WEB_DIR / "dist"), name="dist")

    @app.get("/")
    def index():
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/styles.css")
    def styles():
        return FileResponse(WEB_DIR / "styles.css")

    return app
