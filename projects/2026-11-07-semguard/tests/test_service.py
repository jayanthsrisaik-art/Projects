from fastapi.testclient import TestClient

from semguard.cache import SemanticCache
from semguard.llm import MockLLM
from semguard.service import create_app


def make():
    llm = MockLLM()
    return TestClient(create_app(SemanticCache(), llm)), llm


def test_miss_then_paraphrase_hit():
    client, llm = make()
    r1 = client.post("/v1/complete", json={"prompt": "How do I reset my password?"})
    assert r1.status_code == 200 and r1.headers["X-Cache"] == "MISS"
    r2 = client.post("/v1/complete", json={"prompt": "help me reset my password please"})
    body = r2.json()
    assert r2.headers["X-Cache"] == "HIT" and body["text"] == r1.json()["text"]
    assert body["cache"]["matched_prompt"] == "How do I reset my password?"
    assert llm.calls == 1


def test_guard_rejection_is_explained():
    client, llm = make()
    client.post("/v1/complete", json={"prompt": "can I get a refund for order 4521"})
    r = client.post("/v1/complete", json={"prompt": "can I get a refund for order 4512"}).json()
    assert r["cache"]["status"] == "miss" and "literals differ" in r["cache"]["rejected"][0]["reason"]
    assert llm.calls == 2


def test_system_prompt_scopes_and_volatile_bypass():
    client, llm = make()
    client.post("/v1/complete", json={"prompt": "how do I reset my password", "system": "be terse"})
    r = client.post("/v1/complete", json={"prompt": "how do I reset my password", "system": "be verbose"})
    assert r.headers["X-Cache"] == "MISS"
    r = client.post("/v1/complete", json={"prompt": "weather in paris today"})
    assert r.headers["X-Cache"] == "BYPASS"
    stats = client.get("/v1/stats").json()
    assert stats["bypass"] == 1 and stats["entries"] == 2


def test_client_bypass_and_validation_and_purge():
    client, llm = make()
    client.post("/v1/complete", json={"prompt": "how do I reset my password"})
    r = client.post("/v1/complete", json={"prompt": "how do I reset my password", "cache": False})
    assert r.headers["X-Cache"] == "BYPASS" and llm.calls == 2
    assert client.post("/v1/complete", json={"prompt": ""}).status_code == 422
    assert client.delete("/v1/cache").json()["deleted"] == 2
    assert client.get("/healthz").json() == {"ok": True, "backend": "mock"}
