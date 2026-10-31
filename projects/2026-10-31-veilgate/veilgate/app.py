"""FastAPI service: a PII-safe chat endpoint in front of an LLM.

Sessions keep their vault in process memory only, so the same person keeps
the same placeholder across turns.  Responses never include vault values
other than the rehydrated reply itself.

Run:  uvicorn veilgate.app:app --port 8000
Env:  VEILGATE_BACKEND=mock|mock:echo|anthropic   VEILGATE_ON_LEAK=block|warn
"""
from __future__ import annotations

import os
import uuid
from collections import OrderedDict

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .egress import LeakBlocked
from .gateway import Gateway
from .llm import LLM, make_llm
from .vault import Session

MAX_SESSIONS = 1000


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=50_000)
    session_id: str | None = None
    stream: bool = False


class ChatResponse(BaseModel):
    session_id: str
    reply: str
    entities_redacted: dict[str, int]
    egress_findings: int
    unknown_placeholders: list[str]


def create_app(llm: LLM | None = None, on_leak: str | None = None) -> FastAPI:
    app = FastAPI(title="veilgate", version="1.0.0")
    gateway = Gateway(llm or make_llm(os.environ.get("VEILGATE_BACKEND", "mock")),
                      on_leak=on_leak or os.environ.get("VEILGATE_ON_LEAK", "block"))
    sessions: OrderedDict[str, Session] = OrderedDict()

    def get_session(sid: str | None) -> tuple[str, Session]:
        if sid is None:
            sid = uuid.uuid4().hex
        elif sid not in sessions:
            raise HTTPException(404, "unknown session")
        if sid not in sessions:
            sessions[sid] = Session()
            if len(sessions) > MAX_SESSIONS:
                sessions.popitem(last=False)  # evict oldest vault
        sessions.move_to_end(sid)
        return sid, sessions[sid]

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "backend": gateway.llm.name, "on_leak": gateway.on_leak}

    @app.post("/v1/chat", response_model=ChatResponse)
    def chat(req: ChatRequest):
        sid, session = get_session(req.session_id)
        try:
            if req.stream:
                audit, chunks = gateway.chat_stream(session, req.message)
                return StreamingResponse(chunks, media_type="text/plain",
                                         headers={"X-Session-Id": sid})
            result = gateway.chat(session, req.message)
        except LeakBlocked as e:
            raise HTTPException(422, {"error": "egress_blocked",
                                      "findings": [{"kind": f.kind, "start": f.start} for f in e.findings]})
        a = result.audit
        return ChatResponse(session_id=sid, reply=result.reply, entities_redacted=a.entities_redacted,
                            egress_findings=len(a.egress_findings), unknown_placeholders=a.unknown_placeholders)

    @app.get("/v1/sessions/{sid}")
    def session_info(sid: str) -> dict:
        if sid not in sessions:
            raise HTTPException(404, "unknown session")
        return {"session_id": sid, "entities": sessions[sid].summary()}

    @app.delete("/v1/sessions/{sid}")
    def forget(sid: str) -> dict:
        if sessions.pop(sid, None) is None:
            raise HTTPException(404, "unknown session")
        return {"deleted": sid}

    return app


app = create_app()
