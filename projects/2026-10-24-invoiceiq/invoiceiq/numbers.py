"""Locale-tolerant money/quantity parsing."""
import re
from decimal import ROUND_HALF_UP, Decimal

_NUM = re.compile(r"\d[\d.,]*")
CENT = Decimal("0.01")


def parse_number(token: str) -> Decimal:
    """Parse '1,234.56', '1.234,56', '10,00', '$20.00' into a Decimal.

    The last '.' or ',' is the decimal mark when it is followed by 1-2 digits
    and is the only occurrence of that character (or the other mark appears
    before it); otherwise separators are treated as thousands marks.
    Raises ValueError for anything that is not purely numeric (e.g. '1O.00').
    """
    t = token.strip().lstrip("$€£")
    if not _NUM.fullmatch(t):
        raise ValueError(f"not a number: {token!r}")
    dec = None
    if "," in t and "." in t:
        dec = "," if t.rfind(",") > t.rfind(".") else "."
    elif "," in t and t.count(",") == 1 and re.search(r",\d{1,2}$", t):
        dec = ","
    elif "." in t and t.count(".") == 1 and re.search(r"\.\d{1,2}$", t):
        dec = "."
    if dec is None:
        digits = re.sub(r"[.,]", "", t)
    else:
        whole, frac = t.rsplit(dec, 1)
        digits = re.sub(r"[.,]", "", whole) + "." + frac
    if re.search(r"[.,]$", t) or digits.count(".") > 1:
        raise ValueError(f"malformed number: {token!r}")
    return Decimal(digits)


def money(value) -> str:
    return str(Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP))


def qty(value) -> str:
    d = Decimal(str(value)).normalize()
    return format(d, "f")
