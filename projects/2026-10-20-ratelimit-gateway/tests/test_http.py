import pytest
from fastapi.testclient import TestClient

from app import create_app
from ratelimit.middleware import client_key
from ratelimit.policy import PolicySet, Rule


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def build(rules, exempt=("/health",)):
    clock = Clock()
    app = create_app(PolicySet(rules, exempt), clock=clock)
    return TestClient(app), clock


def test_headers_and_429_with_retry_after():
    client, clock = build([Rule("/", "fixed_window", 2, 60)])
    r = client.get("/items")
    assert r.status_code == 200
    assert r.headers["ratelimit-limit"] == "2" and r.headers["ratelimit-remaining"] == "1"
    assert client.get("/items").headers["ratelimit-remaining"] == "0"
    r = client.get("/items")
    assert r.status_code == 429
    assert r.json()["error"] == "rate_limited"
    assert int(r.headers["retry-after"]) >= 1
    clock.t += int(r.headers["retry-after"]) + 0.01
    assert client.get("/items").status_code == 200


def test_exempt_path_is_never_limited():
    client, _ = build([Rule("/", "fixed_window", 1, 60)])
    assert all(client.get("/health").status_code == 200 for _ in range(10))
    assert "ratelimit-limit" not in client.get("/health").headers


def test_longest_prefix_wins_and_buckets_are_separate():
    client, _ = build([Rule("/", "fixed_window", 100, 60), Rule("/login", "fixed_window", 1, 60)])
    assert client.post("/login").status_code == 200
    assert client.post("/login").status_code == 429
    assert client.get("/items").status_code == 200  # unaffected by /login budget


def test_prefix_match_is_segment_aware():
    ps = PolicySet([Rule("/api", "fixed_window", 1, 60)])
    assert ps.match("/api") and ps.match("/api/x")
    assert ps.match("/apiary") is None


def test_api_key_isolates_clients():
    client, _ = build([Rule("/", "fixed_window", 1, 60)])
    assert client.get("/items", headers={"x-api-key": "a"}).status_code == 200
    assert client.get("/items", headers={"x-api-key": "a"}).status_code == 429
    assert client.get("/items", headers={"x-api-key": "b"}).status_code == 200


def test_client_key_forwarded_header_only_when_trusted():
    scope = {"headers": [(b"x-forwarded-for", b"9.9.9.9, 1.1.1.1")], "client": ("5.5.5.5", 1)}
    assert client_key(scope) == "ip:5.5.5.5"
    assert client_key(scope, "X-Forwarded-For") == "ip:9.9.9.9"
    assert client_key({"headers": []}) == "ip:unknown"


def test_rejected_requests_do_not_reach_handler():
    calls = []
    from fastapi import FastAPI
    from ratelimit.middleware import RateLimitMiddleware
    app = FastAPI()

    @app.get("/x")
    def x():
        calls.append(1)
        return {}
    app.add_middleware(RateLimitMiddleware, policies=PolicySet([Rule("/", "token_bucket", 1, 60)]),
                       clock=Clock())
    c = TestClient(app)
    c.get("/x"); c.get("/x"); c.get("/x")
    assert len(calls) == 1


def test_config_validation():
    with pytest.raises(ValueError):
        Rule("nope", "fixed_window", 1, 1)
    with pytest.raises(ValueError):
        PolicySet([Rule("/a", "fixed_window", 1, 1), Rule("/a", "token_bucket", 1, 1)])
    ps = PolicySet.from_config({"rules": [{"prefix": "/a", "algorithm": "token_bucket", "limit": 1, "window": 1}],
                                "exempt": ["/h"]})
    assert ps.match("/h") is None and ps.match("/a")
