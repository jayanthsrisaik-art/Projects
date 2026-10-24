from decimal import Decimal

import pytest

from invoiceiq.numbers import money, parse_number, qty


@pytest.mark.parametrize("tok,want", [
    ("20.00", "20.00"), ("1,234.56", "1234.56"), ("1.234,56", "1234.56"),
    ("10,00", "10.00"), ("$5.5", "5.5"), ("1,234", "1234"), ("1.234", "1234"),
    ("2", "2"), ("12,345,678.90", "12345678.90"),
])
def test_parse(tok, want):
    assert parse_number(tok) == Decimal(want)


@pytest.mark.parametrize("bad", ["1O.00", "abc", "", "12.", "1.2.3,4.5.6x", "(8%):"])
def test_parse_rejects(bad):
    with pytest.raises(ValueError):
        parse_number(bad)


def test_money_qty():
    assert money("2.5") == "2.50" and money("0.005") == "0.01"
    assert qty("3.0") == "3" and qty(2) == "2"
