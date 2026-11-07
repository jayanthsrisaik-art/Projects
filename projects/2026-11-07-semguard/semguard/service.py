"""FastAPI service: an LLM completion endpoint fronted by the guarded semantic cache."""
from __future__ import annotations

import os
import time
from typing import Optional

from fastapi import FastAPI, Response
from pydantic import BaseModel, Field

from .cache import CacheConfig, Lookup, SemanticCache, scope_key
from .llm import LLM, get_backend


class CompleteRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=20_000)
    system: str = Field("", max_length=20_000)
    tenant: str = Field("", max_length=200)
    cache: bool = True               # false => always call the model, but still store the answer


class CacheInfo(BaseModel):
    status: str
    score: float
    reason: str
    matched_prompt: Optional[str] = None
    rejected: list[dict] = []


class CompleteResponse(BaseModel):
    text: str
    cache: CacheInfo
    model: str
    latency_ms: float


def create_app(cache: Optional[SemanticCache] = None, llm: Optional[LLM] = None) -> FastAPI:
    cache = cache or SemanticCache(
        os.environ.get("SEMGUARD_DB", "semguard.db"),
        CacheConfig(threshold=float(os.environ.get("SEMGUARD_THRESHOLD", "0.68"))))
    llm = llm or get_backend()
    model_name = getattr(llm, "model", llm.name)
    app = FastAPI(title="semguard", version="1.0.0")

    @app.post("/v1/complete", response_model=CompleteResponse)
    def complete(req: CompleteRequest, response: Response) -> CompleteResponse:
        t0 = time.perf_counter()
        scope = scope_key(model_name, req.system, req.tenant)
        if req.cache:
            lk = cache.lookup(req.prompt, scope)
        else:
            lk = Lookup("bypass", reason="client requested cache bypass")
        if lk.hit:
            text = lk.entry["response"]
        else:
            text = llm.complete(req.prompt, req.system).text
            if lk.status == "miss" or not req.cache:
                cache.store(req.prompt, text, scope)
        response.headers["X-Cache"] = lk.status.upper()
        info = CacheInfo(status=lk.status, score=round(lk.score, 4), reason=lk.reason,
                         matched_prompt=lk.entry["prompt"] if lk.entry else None, rejected=lk.rejected)
        return CompleteResponse(text=text, cache=info, model=model_name,
                                latency_ms=round((time.perf_counter() - t0) * 1000, 2))

    @app.get("/v1/stats")
    def stats() -> dict:
        return cache.stats()

    @app.delete("/v1/cache")
    def purge(expired_only: bool = False) -> dict:
        return {"deleted": cache.purge(expired_only=expired_only)}

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True, "backend": llm.name}

    return app
