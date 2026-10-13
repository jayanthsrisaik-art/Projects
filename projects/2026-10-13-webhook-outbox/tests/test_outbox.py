import json

import pytest
from fastapi.testclient import TestClient

from app import store, worker
from app.main import create_app
from app.signing import sign, verify
from app.worker import RetryPolicy


def make_conn():
    conn = store.connect()
    store.add_endpoint(conn, "http://recv.test/hook", "s3cretkey", ["order.created"])
    return conn


class FakeTransport:
    def __init__(self, *codes):
        self.codes = list(codes)
        self.calls = []

    def __call__(self, url, body, headers):
        self.calls.append((url, body, headers))
        code = self.codes.pop(0)
        if isinstance(code, Exception):
            raise code
        return code


# --- signing ---
def test_signature_roundtrip_and_tamper():
    body = b'{"a":1}'
    h = sign("k" * 8, body, 1000)
    assert verify("k" * 8, body, h, now=1000)
    assert not verify("k" * 8, b'{"a":2}', h, now=1000)
    assert not verify("other-secret", body, h, now=1000)


def test_signature_replay_and_malformed():
    h = sign("k" * 8, b"x", 1000)
    assert not verify("k" * 8, b"x", h, now=1000 + 301)
    assert verify("k" * 8, b"x", h, now=1000 + 300)
    assert not verify("k" * 8, b"x", "garbage", now=1000)


# --- fan-out and idempotency ---
def test_fanout_filters_by_event_type():
    conn = make_conn()
    store.add_endpoint(conn, "http://all.test", "s3cretkey", ["*"])
    assert len(store.enqueue_event(conn, "k1", "order.created", {}, 0)) == 2
    assert len(store.enqueue_event(conn, "k2", "user.deleted", {}, 0)) == 1


def test_idempotent_enqueue():
    conn = make_conn()
    assert len(store.enqueue_event(conn, "k1", "order.created", {"id": 1}, 0)) == 1
    assert store.enqueue_event(conn, "k1", "order.created", {"id": 1}, 0) == []
    assert len(store.list_deliveries(conn)) == 1


# --- worker ---
def test_success_sends_verifiable_signature():
    conn = make_conn()
    store.enqueue_event(conn, "k1", "order.created", {"id": 7}, 100)
    t = FakeTransport(200)
    assert worker.run_once(conn, 100, t) == {"delivered": 1, "retried": 0, "dead": 0}
    _, body, headers = t.calls[0]
    assert verify("s3cretkey", body, headers["X-Signature"], now=100)
    assert json.loads(body)["data"] == {"id": 7}
    assert store.list_deliveries(conn)[0]["status"] == "delivered"


def test_backoff_schedule_then_dead_letter():
    conn = make_conn()
    store.enqueue_event(conn, "k1", "order.created", {}, 0)
    policy = RetryPolicy(max_attempts=4, base_delay=10, max_delay=25)
    t = FakeTransport(500, 503, 502, 500)
    now, nexts = 0, []
    for _ in range(4):
        worker.run_once(conn, now, t, policy)
        row = store.list_deliveries(conn)[0]
        nexts.append(row["next_attempt_at"] - now)
        now = row["next_attempt_at"]
    assert nexts[:2] == [10, 20]          # exponential
    assert nexts[2] == 25                  # capped at max_delay
    assert row["status"] == "dead" and row["attempts"] == 4


def test_not_due_is_skipped():
    conn = make_conn()
    store.enqueue_event(conn, "k1", "order.created", {}, 0)
    t = FakeTransport(500)
    worker.run_once(conn, 0, t, RetryPolicy(base_delay=60))
    assert worker.run_once(conn, 59, FakeTransport()) == {"delivered": 0, "retried": 0, "dead": 0}


def test_permanent_4xx_dead_immediately_but_429_retries():
    conn = make_conn()
    store.enqueue_event(conn, "a", "order.created", {}, 0)
    assert worker.run_once(conn, 0, FakeTransport(400))["dead"] == 1
    store.enqueue_event(conn, "b", "order.created", {}, 0)
    assert worker.run_once(conn, 0, FakeTransport(429))["retried"] == 1


def test_network_error_is_retried_and_recorded():
    conn = make_conn()
    store.enqueue_event(conn, "k", "order.created", {}, 0)
    stats = worker.run_once(conn, 0, FakeTransport(ConnectionError("refused")))
    row = store.list_deliveries(conn)[0]
    assert stats["retried"] == 1 and "refused" in row["last_error"] and row["last_status_code"] is None


# --- API ---
def test_api_end_to_end_with_requeue():
    clock = {"t": 1000}
    t = FakeTransport(404, 200)
    client = TestClient(create_app(clock=lambda: clock["t"], transport=t))
    assert client.post("/endpoints", json={"url": "http://r.test/h", "secret": "longsecret"}).status_code == 201
    ev = {"idempotency_key": "e1", "type": "x", "data": {"n": 1}}
    assert client.post("/events", json=ev).json() == {"created_deliveries": [1]}
    assert client.post("/events", json=ev).json() == {"created_deliveries": []}
    assert client.post("/worker/run-once").json()["dead"] == 1
    assert client.get("/deliveries", params={"status": "dead"}).json()[0]["last_status_code"] == 404
    assert client.post("/deliveries/1/retry").status_code == 200
    assert client.post("/worker/run-once").json()["delivered"] == 1
    assert client.post("/deliveries/1/retry").status_code == 409


def test_api_validation_and_404():
    client = TestClient(create_app())
    assert client.post("/endpoints", json={"url": "nope", "secret": "longsecret"}).status_code == 422
    assert client.post("/endpoints", json={"url": "http://a.test", "secret": "x"}).status_code == 422
    assert client.get("/deliveries/99").status_code == 404
    assert client.get("/deliveries", params={"status": "bogus"}).status_code == 422
