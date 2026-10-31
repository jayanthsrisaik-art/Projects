from fastapi.testclient import TestClient

from veilgate.app import create_app
from veilgate.llm import MockLLM


def make():
    llm = MockLLM()
    return TestClient(create_app(llm=llm, on_leak="block")), llm


def test_chat_roundtrip_and_session_reuse():
    client, llm = make()
    r = client.post("/v1/chat", json={"message": "Customer Ana Ruiz, ana@example.com"})
    assert r.status_code == 200
    body = r.json()
    assert "Ana Ruiz" in body["reply"] and "ana@example.com" in body["reply"]
    assert "Ana Ruiz" not in llm.last_prompt
    sid = body["session_id"]
    r2 = client.post("/v1/chat", json={"message": "Ana Ruiz again", "session_id": sid})
    assert "<PERSON_1>" in llm.last_prompt                 # same placeholder across turns
    info = client.get(f"/v1/sessions/{sid}").json()
    assert info["entities"] == {"PERSON": 1, "EMAIL": 1}
    assert "Ana" not in str(info)                           # summary exposes counts only
    assert client.delete(f"/v1/sessions/{sid}").status_code == 200
    assert client.get(f"/v1/sessions/{sid}").status_code == 404
    assert r2.status_code == 200


def test_streaming_endpoint():
    client, _ = make()
    r = client.post("/v1/chat", json={"message": "Customer Ana Ruiz from 10.0.0.7", "stream": True})
    assert r.status_code == 200
    assert "Ana Ruiz" in r.text and "10.0.0.7" in r.text
    assert r.headers["x-session-id"]


def test_blocked_request_returns_422_without_values():
    client, llm = make()
    r = client.post("/v1/chat", json={"message": "member code 123456789012"})
    assert r.status_code == 422
    assert "123456789012" not in r.text
    assert llm.last_prompt is None


def test_unknown_session_and_validation():
    client, _ = make()
    assert client.post("/v1/chat", json={"message": "hi", "session_id": "nope"}).status_code == 404
    assert client.post("/v1/chat", json={"message": ""}).status_code == 422
