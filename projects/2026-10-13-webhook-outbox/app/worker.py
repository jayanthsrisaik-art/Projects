"""Delivery worker: retry policy, backoff, and dead-lettering."""
from dataclasses import dataclass
from typing import Callable

from . import store
from .signing import sign

# transport(url, body, headers) -> HTTP status code; may raise on network errors.
Transport = Callable[[str, bytes, dict], int]


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 6
    base_delay: int = 30      # seconds
    max_delay: int = 3600

    def delay(self, attempts_made: int) -> int:
        """Exponential backoff: base * 2^(n-1), capped. Deterministic (no jitter) for testability."""
        return min(self.base_delay * 2 ** (attempts_made - 1), self.max_delay)


def is_permanent_failure(status: int) -> bool:
    """4xx means the receiver rejected the request; retrying won't help (except 408/429)."""
    return 400 <= status < 500 and status not in (408, 429)


def httpx_transport(url: str, body: bytes, headers: dict) -> int:
    import httpx
    return httpx.post(url, content=body, headers=headers, timeout=10).status_code


def run_once(conn, now: int, transport: Transport = httpx_transport,
             policy: RetryPolicy = RetryPolicy(), limit: int = 50) -> dict:
    stats = {"delivered": 0, "retried": 0, "dead": 0}
    for d in store.due_deliveries(conn, now, limit):
        body = d["payload"].encode()
        headers = {
            "Content-Type": "application/json",
            "X-Signature": sign(d["secret"], body, now),
            "X-Delivery-Id": str(d["id"]),
            "X-Delivery-Attempt": str(d["attempts"] + 1),
        }
        status_code, error = None, None
        try:
            status_code = transport(d["url"], body, headers)
        except Exception as exc:  # network failure: treat as retryable
            error = f"{type(exc).__name__}: {exc}"
        attempts = d["attempts"] + 1

        if status_code is not None and 200 <= status_code < 300:
            new_status, next_at = "delivered", d["next_attempt_at"]
            stats["delivered"] += 1
        elif (status_code is not None and is_permanent_failure(status_code)) or attempts >= policy.max_attempts:
            new_status, next_at = "dead", d["next_attempt_at"]
            stats["dead"] += 1
        else:
            new_status, next_at = "pending", now + policy.delay(attempts)
            stats["retried"] += 1
        if error is None and status_code is not None and not 200 <= status_code < 300:
            error = f"HTTP {status_code}"
        with conn:
            conn.execute(
                "UPDATE deliveries SET status=?, attempts=?, next_attempt_at=?, "
                "last_status_code=?, last_error=? WHERE id=?",
                (new_status, attempts, next_at, status_code, error, d["id"]),
            )
    return stats
