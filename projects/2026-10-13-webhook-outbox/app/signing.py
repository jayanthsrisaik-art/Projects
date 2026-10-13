"""HMAC request signing (Stripe-style: `t=<unix>,v1=<hex>`)."""
import hashlib
import hmac


def sign(secret: str, body: bytes, timestamp: int) -> str:
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256)
    return f"t={timestamp},v1={mac.hexdigest()}"


def verify(secret: str, body: bytes, header: str, now: int, tolerance: int = 300) -> bool:
    """Receiver-side check: valid signature and timestamp within tolerance (replay guard)."""
    try:
        parts = dict(p.split("=", 1) for p in header.split(","))
        ts = int(parts["t"])
        given = parts["v1"]
    except (KeyError, ValueError):
        return False
    if abs(now - ts) > tolerance:
        return False
    expected = sign(secret, body, ts).split("v1=", 1)[1]
    return hmac.compare_digest(expected, given)
