"""Offline rule-based extractor (the default; needs no network or API key).

attempt 0 is strict. attempt >= 1 additionally accepts the two-number line
form "<description> x<qty> <amount>" and derives the unit price, which is how
the pipeline's repair loop recovers documents the strict pass could not read.
"""
import datetime as dt
import re
from decimal import Decimal

from .numbers import money, parse_number, qty

MONTHS = {m: i for i, m in enumerate(
    "jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}
_SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP"}
_CODES = ("USD", "EUR", "GBP")

_NUM_RE = re.compile(r"(?i)(?:invoice\s*(?:no\.?|number|#|:)|rechnung\s*nr\.?)\s*[:#]?\s*([A-Z0-9][A-Z0-9\-/]*)")
_SUBTOTAL = re.compile(r"(?i)^\W*(?:sub-?\s?total|net\s+total)\b")
_TAX = re.compile(r"(?i)^\W*(?:sales\s+)?(?:tax|vat)\b")
_TOTAL = re.compile(r"(?i)^\W*(?:grand\s+total|total\s+due|amount\s+due|total)\b")
_SKIP = re.compile(r"(?i)\b(invoice|date|issued|due|bill|description|item)\b")


def _tokens(line: str) -> list[str]:
    line = line.replace("|", " ")
    toks = []
    for t in line.split():
        if t.lower() in ("x", "=", "@"):
            continue
        toks.append(t.strip("*"))
    return toks


def _last_number(line: str):
    for t in reversed(_tokens(line)):
        try:
            return parse_number(t)
        except ValueError:
            continue
    return None


def _date(text: str) -> str | None:
    lines = [l for l in text.splitlines() if not re.search(r"(?i)\bdue\b", l)]
    lines.sort(key=lambda l: 0 if re.search(r"(?i)date|issued", l) else 1)
    for l in lines:
        try:
            if m := re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", l):
                return dt.date(*map(int, m.groups())).isoformat()
            if m := re.search(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b", l):
                d, mo, y = map(int, m.groups())
                return dt.date(y, mo, d).isoformat()
            if m := re.search(r"\b(\d{1,2})\s+([A-Za-z]{3})[a-z]*\.?,?\s+(\d{4})\b", l):
                if m.group(2).lower() in MONTHS:
                    return dt.date(int(m.group(3)), MONTHS[m.group(2).lower()], int(m.group(1))).isoformat()
            if m := re.search(r"\b([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})\b", l):
                if m.group(1).lower() in MONTHS:
                    return dt.date(int(m.group(3)), MONTHS[m.group(1).lower()], int(m.group(2))).isoformat()
        except ValueError:
            continue
    return None


def _currency(text: str) -> str | None:
    for code in _CODES:
        if re.search(rf"\b{code}\b", text):
            return code
    for sym, code in _SYMBOLS.items():
        if sym in text:
            return code
    return None


def _item(line: str, attempt: int):
    toks = _tokens(line)
    toks = [t.lstrip("$€£") for t in toks]
    try:  # strict: <desc> <qty> <unit> <amount>
        a, u, q = (parse_number(t) for t in toks[-1:-4:-1])
        desc = " ".join(toks[:-3])
    except ValueError:
        if attempt < 1:
            return None
        try:  # lenient: <desc> x<qty> <amount>
            if not re.fullmatch(r"[xX]\d+", toks[-2]):
                return None
            q, a = Decimal(toks[-2][1:]), parse_number(toks[-1])
            u = a / q
            desc = " ".join(toks[:-2])
        except (ValueError, IndexError, ZeroDivisionError):
            return None
    if len(toks) < 4 or not re.search(r"[A-Za-z]", desc):
        return None
    return {"description": desc, "quantity": qty(q), "unit_price": money(u), "amount": money(a)}


class LocalExtractor:
    name = "local-rules"

    def extract(self, text: str, feedback: list[str] | None = None, attempt: int = 0) -> dict:
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        out: dict = {
            "invoice_number": None, "invoice_date": _date(text),
            "vendor": lines[0].lstrip("# ").strip() if lines else None,
            "currency": _currency(text), "line_items": [],
            "subtotal": None, "tax": "0.00", "total": None,
        }
        if m := _NUM_RE.search(text):
            out["invoice_number"] = m.group(1)
        in_items = True
        for l in lines[1:]:
            if _SUBTOTAL.match(l):
                in_items = False
                n = _last_number(l)
                out["subtotal"] = money(n) if n is not None else None
            elif _TAX.match(l):
                in_items = False
                n = _last_number(l)
                if n is not None:
                    out["tax"] = money(n)
            elif _TOTAL.match(l):
                in_items = False
                n = _last_number(l)
                out["total"] = money(n) if n is not None else None
            elif in_items and not _SKIP.search(l):
                if (it := _item(l, attempt)) is not None:
                    out["line_items"].append(it)
        return out
