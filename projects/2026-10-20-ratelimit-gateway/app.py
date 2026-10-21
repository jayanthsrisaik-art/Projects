"""Demo FastAPI app protected by the rate-limit middleware.

Run: uvicorn app:app --reload
"""
from fastapi import FastAPI

from ratelimit.middleware import RateLimitMiddleware
from ratelimit.policy import PolicySet

CONFIG = {
    "rules": [
        {"prefix": "/", "algorithm": "sliding_counter", "limit": 60, "window": 60},
        {"prefix": "/search", "algorithm": "token_bucket", "limit": 5, "window": 10},
        {"prefix": "/login", "algorithm": "sliding_log", "limit": 3, "window": 60},
    ],
    "exempt": ["/health"],
}


def create_app(policies: PolicySet | None = None, clock=None) -> FastAPI:
    app = FastAPI(title="ratelimit-gateway demo")

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/search")
    def search(q: str = ""):
        return {"q": q, "results": []}

    @app.post("/login")
    def login():
        return {"ok": True}

    @app.get("/items")
    def items():
        return {"items": [1, 2, 3]}

    kwargs = {"clock": clock} if clock else {}
    app.add_middleware(RateLimitMiddleware, policies=policies or PolicySet.from_config(CONFIG), **kwargs)
    return app


app = create_app()
