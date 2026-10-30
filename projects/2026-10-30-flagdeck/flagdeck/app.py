"""FastAPI service: flag CRUD with If-Match concurrency, audit log, evaluation, SDK payload, impact preview."""
from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

from .evaluator import evaluate, evaluate_all
from .population import synthetic_contexts
from .schema import ENVIRONMENTS, FlagIn, semantic_errors
from .store import Conflict, InUse, Invalid, NotFound, Store

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


class Context(BaseModel):
    model_config = {"extra": "allow"}
    key: str = Field(min_length=1)


class EvaluateIn(BaseModel):
    env: str
    context: Context


class PreviewIn(BaseModel):
    env: str
    draft: dict[str, Any]
    contexts: Optional[list[Context]] = None
    sample_size: int = Field(default=2000, ge=1, le=50_000)


class ToggleIn(BaseModel):
    env: str
    on: bool
    comment: str = ""


def _etag(version: int) -> str:
    return f'"{version}"'


def _parse_if_match(value: Optional[str]) -> int:
    if value is None:
        raise HTTPException(428, "If-Match header with the flag version is required")
    try:
        return int(value.strip().strip('"').removeprefix("W/").strip('"'))
    except ValueError:
        raise HTTPException(400, f"malformed If-Match: {value!r}")


def _check_env(env: str) -> None:
    if env not in ENVIRONMENTS:
        raise HTTPException(404, f"unknown environment '{env}'; expected one of {list(ENVIRONMENTS)}")


def _parse_flag(body: dict) -> dict:
    try:
        return FlagIn.model_validate(body).doc()
    except ValidationError as exc:
        raise HTTPException(422, [f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()])


def create_app(db_path: Optional[str] = None) -> FastAPI:
    store = Store(db_path or os.environ.get("FLAGDECK_DB", "flagdeck.db"))
    app = FastAPI(title="flagdeck", version="1.0.0")
    app.state.store = store

    @app.exception_handler(NotFound)
    async def _nf(_: Request, exc: NotFound):
        return JSONResponse({"detail": f"flag '{exc}' not found"}, status_code=404)

    @app.exception_handler(Conflict)
    async def _cf(_: Request, exc: Conflict):
        if exc.current_version is None:
            return JSONResponse({"detail": "flag already exists"}, status_code=409)
        return JSONResponse({"detail": "flag was modified by someone else", "current_version": exc.current_version},
                            status_code=412, headers={"ETag": _etag(exc.current_version)})

    @app.exception_handler(Invalid)
    async def _inv(_: Request, exc: Invalid):
        return JSONResponse({"detail": exc.errors}, status_code=422)

    @app.exception_handler(InUse)
    async def _iu(_: Request, exc: InUse):
        return JSONResponse({"detail": "flag is a prerequisite of other flags", "dependents": exc.dependents},
                            status_code=409)

    def with_etag(row: dict, status: int = 200) -> JSONResponse:
        return JSONResponse(row, status_code=status, headers={"ETag": _etag(row["version"])})

    @app.get("/api/meta")
    def meta():
        return {"environments": list(ENVIRONMENTS)}

    @app.get("/api/flags")
    def list_flags():
        return store.list()

    @app.post("/api/flags", status_code=201)
    def create_flag(body: dict, x_actor: str = Header("anonymous"), x_comment: str = Header("")):
        return with_etag(store.create(_parse_flag(body), x_actor, x_comment), 201)

    @app.get("/api/flags/{key}")
    def get_flag(key: str):
        return with_etag(store.get(key))

    @app.put("/api/flags/{key}")
    def put_flag(key: str, body: dict, if_match: Optional[str] = Header(None),
                 x_actor: str = Header("anonymous"), x_comment: str = Header("")):
        version = _parse_if_match(if_match)
        return with_etag(store.update(key, _parse_flag(body), version, x_actor, x_comment))

    @app.post("/api/flags/{key}/toggle")
    def toggle(key: str, body: ToggleIn, if_match: Optional[str] = Header(None), x_actor: str = Header("anonymous")):
        _check_env(body.env)
        version = _parse_if_match(if_match)
        return with_etag(store.set_on(key, body.env, body.on, version, x_actor, body.comment))

    @app.delete("/api/flags/{key}", status_code=204)
    def delete_flag(key: str, if_match: Optional[str] = Header(None), x_actor: str = Header("anonymous")):
        store.delete(key, _parse_if_match(if_match), x_actor)
        return Response(status_code=204)

    @app.get("/api/flags/{key}/audit")
    def audit(key: str):
        return store.audit(key)

    @app.post("/api/evaluate")
    def evaluate_endpoint(body: EvaluateIn):
        _check_env(body.env)
        return evaluate_all(store.docs(), body.env, body.context.model_dump())

    @app.post("/api/flags/{key}/preview")
    def preview(key: str, body: PreviewIn):
        """Evaluate current vs draft config over a population and report who flips."""
        _check_env(body.env)
        current = store.get(key)["flag"]
        draft = _parse_flag(body.draft)
        if draft["key"] != key:
            raise HTTPException(422, ["flag key cannot be changed"])
        others = store.docs()
        others.pop(key)
        errors = semantic_errors(draft, others)
        if errors:
            raise HTTPException(422, errors)
        contexts = ([c.model_dump() for c in body.contexts] if body.contexts is not None
                    else synthetic_contexts(body.sample_size))
        before_flags = {**others, key: current}
        after_flags = {**others, key: draft}
        before_counts: Counter = Counter()
        after_counts: Counter = Counter()
        changed = []
        for ctx in contexts:
            b = evaluate(current, body.env, ctx, before_flags.get)
            a = evaluate(draft, body.env, ctx, after_flags.get)
            before_counts[json.dumps(b["value"])] += 1
            after_counts[json.dumps(a["value"])] += 1
            if b["variation"] != a["variation"]:
                changed.append({"context": ctx, "before": b, "after": a})
        return {
            "total": len(contexts),
            "changed": len(changed),
            "before": dict(sorted(before_counts.items())),
            "after": dict(sorted(after_counts.items())),
            "examples": changed[:20],
        }

    @app.get("/api/sdk/{env}")
    def sdk_payload(env: str, request: Request):
        """Everything a client needs to evaluate locally, with a content-hash ETag for cheap polling."""
        _check_env(env)
        flags = {}
        for k, f in store.docs().items():
            flags[k] = {**f, "environments": {env: f["environments"][env]}}
        payload = {"env": env, "flags": flags}
        body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        tag = '"' + hashlib.sha256(body.encode()).hexdigest()[:20] + '"'
        if request.headers.get("if-none-match") == tag:
            return Response(status_code=304, headers={"ETag": tag})
        return Response(body, media_type="application/json", headers={"ETag": tag, "Cache-Control": "no-cache"})

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/styles.css", include_in_schema=False)
    def styles():
        return FileResponse(WEB_DIR / "styles.css")

    # Compiled TypeScript (``npm run build``); absent until the UI has been built.
    app.mount("/dist", StaticFiles(directory=WEB_DIR / "dist", check_dir=False), name="dist")
    return app
