"""FastAPI surface. Run: uvicorn app.main:app"""
import os
import time

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, HttpUrl

from . import store, worker


class EndpointIn(BaseModel):
    url: HttpUrl
    secret: str = Field(min_length=8)
    event_types: list[str] = Field(default_factory=lambda: ["*"], min_length=1)


class EventIn(BaseModel):
    idempotency_key: str = Field(min_length=1)
    type: str = Field(min_length=1)
    data: dict


def create_app(db_path: str | None = None, clock=lambda: int(time.time()), transport=None) -> FastAPI:
    app = FastAPI(title="Webhook Outbox")
    conn = store.connect(db_path or os.environ.get("OUTBOX_DB", ":memory:"))
    tx = transport or worker.httpx_transport

    def view(row):
        return dict(row)

    @app.post("/endpoints", status_code=201)
    def add_endpoint(body: EndpointIn):
        return {"id": store.add_endpoint(conn, str(body.url), body.secret, body.event_types)}

    @app.post("/events", status_code=202)
    def publish(body: EventIn):
        ids = store.enqueue_event(conn, body.idempotency_key, body.type, body.data, clock())
        return {"created_deliveries": ids}

    @app.get("/deliveries")
    def deliveries(status: str | None = None):
        if status not in (None, "pending", "delivered", "dead"):
            raise HTTPException(422, "status must be pending, delivered or dead")
        return [view(r) for r in store.list_deliveries(conn, status)]

    @app.get("/deliveries/{delivery_id}")
    def delivery(delivery_id: int):
        row = store.get_delivery(conn, delivery_id)
        if row is None:
            raise HTTPException(404, "delivery not found")
        return view(row)

    @app.post("/worker/run-once")
    def run_worker():
        return worker.run_once(conn, clock(), tx)

    @app.post("/deliveries/{delivery_id}/retry")
    def requeue_dead(delivery_id: int):
        """Manually requeue a dead-lettered delivery."""
        row = store.get_delivery(conn, delivery_id)
        if row is None:
            raise HTTPException(404, "delivery not found")
        if row["status"] != "dead":
            raise HTTPException(409, "only dead deliveries can be requeued")
        with conn:
            conn.execute("UPDATE deliveries SET status='pending', attempts=0, next_attempt_at=? WHERE id=?",
                         (clock(), delivery_id))
        return {"status": "pending"}

    return app


app = create_app()
