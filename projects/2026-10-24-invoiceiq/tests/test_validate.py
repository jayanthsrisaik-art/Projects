import copy

from invoiceiq.validate import validate

GOOD = {
    "invoice_number": "INV-1", "invoice_date": "2024-03-15", "vendor": "Acme", "currency": "USD",
    "line_items": [{"description": "A", "quantity": "2", "unit_price": "10.00", "amount": "20.00"},
                   {"description": "B", "quantity": "1", "unit_price": "5.00", "amount": "5.00"}],
    "subtotal": "25.00", "tax": "2.00", "total": "27.00",
}


def test_good_passes():
    assert validate(GOOD) == []


def _bad(**kw):
    d = copy.deepcopy(GOOD)
    d.update(kw)
    return validate(d)


def test_total_mismatch():
    assert any("!= total" in e for e in _bad(total="28.00"))


def test_subtotal_mismatch():
    assert any("!= subtotal" in e for e in _bad(subtotal="24.00"))


def test_line_arithmetic():
    d = copy.deepcopy(GOOD)
    d["line_items"][0]["amount"] = "21.00"
    assert any("line 1" in e for e in validate(d))


def test_exact_by_default_tolerance_opt_in():
    from decimal import Decimal
    d = dict(GOOD, total="27.01")
    assert validate(d) != []
    assert validate(d, tol=Decimal("0.01")) == []
    assert validate(dict(GOOD, total="27.02"), tol=Decimal("0.01")) != []


def test_missing_and_bad_fields():
    errs = _bad(invoice_date="15/03/2024", currency="JPY", invoice_number="", total=None, line_items=[])
    text = " ".join(errs)
    for needle in ("ISO", "currency", "invoice_number", "missing total", "no line items"):
        assert needle in text


def test_non_numeric_line():
    d = copy.deepcopy(GOOD)
    d["line_items"][1]["amount"] = "5.0O"
    assert any("non-numeric" in e for e in validate(d))
