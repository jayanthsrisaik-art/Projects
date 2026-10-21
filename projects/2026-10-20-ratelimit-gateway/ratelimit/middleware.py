"""Pure-ASGI middleware that enforces a PolicySet and emits RateLimit headers."""
from __future__ import annotations

import json
import math
import time
from typing import Callable

from .policy import PolicySet


def client_key(scope, trusted_proxy_header: str | None = None) -> str:
    """Identify the caller: API key header, else (optionally trusted) forwarded IP, else peer IP."""
    headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
    if headers.get("x-api-key"):
        return "key:" + headers["x-api-key"]
    if trusted_proxy_header and headers.get(trusted_proxy_header.lower()):
        return "ip:" + headers[trusted_proxy_header.lower()].split(",")[0].strip()
    client = scope.get("client")
    return "ip:" + (client[0] if client else "unknown")


class RateLimitMiddleware:
    def __init__(self, app, policies: PolicySet, clock: Callable[[], float] = time.monotonic,
                 trusted_proxy_header: str | None = None, sweep_every: int = 1000):
        self.app = app
        self.policies = policies
        self.clock = clock
        self.trusted_proxy_header = trusted_proxy_header
        self.sweep_every = sweep_every
        self._n = 0

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        rule = self.policies.match(scope["path"])
        if rule is None:
            await self.app(scope, receive, send)
            return

        now = self.clock()
        self._n += 1
        if self.sweep_every and self._n % self.sweep_every == 0:
            self.policies.evict_idle(now)

        key = f"{rule.prefix}|{client_key(scope, self.trusted_proxy_header)}"
        d = rule.limiter.check(key, now)
        info = [
            (b"ratelimit-limit", str(d.limit).encode()),
            (b"ratelimit-remaining", str(d.remaining).encode()),
            (b"ratelimit-reset", str(math.ceil(d.reset_after)).encode()),
        ]
        if d.allowed:
            async def send_with_headers(message):
                if message["type"] == "http.response.start":
                    message = {**message, "headers": [*message.get("headers", []), *info]}
                await send(message)
            await self.app(scope, receive, send_with_headers)
            return

        retry = max(1, math.ceil(d.retry_after))
        body = json.dumps({"error": "rate_limited", "retry_after_seconds": retry}).encode()
        await send({"type": "http.response.start", "status": 429, "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
            (b"retry-after", str(retry).encode()), *info]})
        await send({"type": "http.response.body", "body": body})
