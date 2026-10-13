"""Money is handled as integer cents; floats never touch ledger amounts."""
from decimal import Decimal, InvalidOperation


def to_cents(text: str) -> int:
    try:
        d = Decimal(text.strip())
    except InvalidOperation as exc:
        raise ValueError(f"bad amount {text!r}") from exc
    if d != d.quantize(Decimal("0.01")) or d < 0:
        raise ValueError(f"amount must be non-negative with <=2 decimals: {text!r}")
    return int(d * 100)


def fmt(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}"
