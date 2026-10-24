import pytest

from invoiceiq.local import LocalExtractor

US = """Northwind Traders
INVOICE #INV-2024-0042
Date: 2024-03-15
Bill To: Acme Corp 12
Description Qty Unit Price Amount
Widget A 2 10.00 20.00
Cat6 Cable 2m 1 1,000.50 1,000.50
Subtotal: 1,020.50
Tax (8%): 81.64
Total: USD 1,102.14
"""


def test_us_plain():
    d = LocalExtractor().extract(US)
    assert d["invoice_number"] == "INV-2024-0042" and d["invoice_date"] == "2024-03-15"
    assert d["vendor"] == "Northwind Traders" and d["currency"] == "USD"
    assert [i["amount"] for i in d["line_items"]] == ["20.00", "1000.50"]
    assert d["line_items"][1]["description"] == "Cat6 Cable 2m"
    assert (d["subtotal"], d["tax"], d["total"]) == ("1020.50", "81.64", "1102.14")


def test_european_formats():
    t = """Tandem Logistics
Invoice No: R-2024-0007
Invoice Date: 05.11.2024
Item Qty x Price = Amount
Pallet Wrap 3 x 1.200,00 = 3.600,00
Net total 3.600,00 EUR
VAT 20% 720,00 EUR
Amount due: 4.320,00 EUR
"""
    d = LocalExtractor().extract(t)
    assert d["invoice_date"] == "2024-11-05" and d["currency"] == "EUR"
    assert d["line_items"][0]["unit_price"] == "1200.00"
    assert (d["subtotal"], d["tax"], d["total"]) == ("3600.00", "720.00", "4320.00")


def test_markdown_dates_prefers_issue_over_due():
    t = """# Lumen Print Co
Invoice number: BL-2024-1
Issued: Mar 15, 2024
Due Date: Apr 14, 2024
| Description | Qty | Unit | Amount |
| Toner Cartridge | 2 | £40.00 | £80.00 |
Sub-total £80.00
Grand Total £80.00
"""
    d = LocalExtractor().extract(t)
    assert d["invoice_date"] == "2024-03-15" and d["currency"] == "GBP"
    assert d["tax"] == "0.00" and len(d["line_items"]) == 1


QTY = "Acme\nInvoice: INV-9\nDate: 1 Feb 2024\nItem Qty Amount\nSafety Gloves x3 30.00\nSubtotal 30.00\nTOTAL DUE USD 30.00\n"


def test_qty_marker_needs_lenient_attempt():
    ex = LocalExtractor()
    assert ex.extract(QTY, attempt=0)["line_items"] == []
    it = ex.extract(QTY, attempt=1)["line_items"][0]
    assert (it["quantity"], it["unit_price"], it["amount"]) == ("3", "10.00", "30.00")


def test_ocr_letter_not_parsed_as_number():
    d = LocalExtractor().extract(US.replace("20.00", "2O.00"))
    assert len(d["line_items"]) == 1  # damaged line is dropped, not misread
