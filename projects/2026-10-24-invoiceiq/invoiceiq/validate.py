"""Deterministic checks that make extraction errors detectable.

Invoices are redundant by construction (qty*unit=amount, sum(amounts)=subtotal,
subtotal+tax=total), so a wrong digit usually breaks an identity. The errors
returned here double as feedback for the next extraction attempt.
"""
import datetime as dt
from decimal import Decimal, InvalidOperation

CURRENCIES = {"USD", "EUR", "GBP"}
EXACT = Decimal("0")


def _d(x):
    try:
        return Decimal(str(x))
    except (InvalidOperation, ValueError):
        return None


def validate(data: dict, tol: Decimal = EXACT) -> list[str]:
    """tol=0 by default: a 1-cent digit swap slipped through a 0.01 tolerance in
    testing. Raise it for real invoices whose tax is rounded per line."""
    errors: list[str] = []
    for f in ("invoice_number", "vendor"):
        if not str(data.get(f) or "").strip():
            errors.append(f"missing {f}")
    try:
        dt.date.fromisoformat(str(data.get("invoice_date")))
    except ValueError:
        errors.append("invoice_date missing or not ISO YYYY-MM-DD")
    if data.get("currency") not in CURRENCIES:
        errors.append(f"currency must be one of {sorted(CURRENCIES)}")

    subtotal, total = _d(data.get("subtotal")), _d(data.get("total"))
    tax = _d(data.get("tax"))
    if subtotal is None:
        errors.append("missing subtotal")
    if total is None:
        errors.append("missing total")
    if tax is None:
        errors.append("missing tax")

    items = data.get("line_items") or []
    if not items:
        errors.append("no line items found")
    amounts = []
    for i, it in enumerate(items, 1):
        q, u, a = _d(it.get("quantity")), _d(it.get("unit_price")), _d(it.get("amount"))
        if None in (q, u, a):
            errors.append(f"line {i}: non-numeric quantity/unit_price/amount")
            continue
        amounts.append(a)
        if abs(q * u - a) > tol:
            errors.append(f"line {i}: quantity*unit_price={q * u} but amount={a}")
    if subtotal is not None and items and abs(sum(amounts, Decimal(0)) - subtotal) > tol:
        errors.append(f"sum of line amounts {sum(amounts, Decimal(0))} != subtotal {subtotal}")
    if None not in (subtotal, tax, total) and abs(subtotal + tax - total) > tol:
        errors.append(f"subtotal {subtotal} + tax {tax} != total {total}")
    return errors
