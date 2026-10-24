"""Deterministic synthetic invoices in four layouts, with gold labels.

Optional corruption simulates OCR/transcription damage to a single line
(letter-for-digit, digit swap, dropped line). Gold labels stay clean.
"""
import datetime as dt
import random
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from .numbers import money, qty

VENDORS = ["Northwind Traders", "Blue Harbor Supply", "Kestrel Industrial", "Lumen Print Co",
           "Orchard & Vine Ltd", "Pioneer Cable Works", "Tandem Logistics", "Quartz Office Goods"]
CLIENTS = ["Acme Corp", "Globex LLC", "Initech", "Umbrella Foods", "Hooli Labs"]
ITEMS = ["Widget A", "Cat6 Cable 2m", "USB-C Hub", "Paper Ream A4", "Toner Cartridge", "Standing Desk",
         "Label Roll 500", "Safety Gloves", "Pallet Wrap", "LED Panel 60W", "Hex Bolt M8", "Service Hour"]
MON = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
TEMPLATES = ("us_plain", "eu_table", "pipe_md", "qty_marker")


@dataclass
class Doc:
    text: str
    gold: dict
    template: str
    corrupted: str | None  # None or the corruption kind


def _fmt(amount: str, eu: bool) -> str:
    whole, frac = amount.split(".")
    neg = whole.startswith("-")
    whole = whole.lstrip("-")
    groups = []
    while whole:
        groups.insert(0, whole[-3:])
        whole = whole[:-3]
    sep, dec = (".", ",") if eu else (",", ".")
    return ("-" if neg else "") + sep.join(groups) + dec + frac


def _gold(rng: random.Random, currency: str):
    n = rng.randint(1, 4)
    items, sub = [], Decimal(0)
    for desc in rng.sample(ITEMS, n):
        q = rng.randint(1, 9)
        u = Decimal(rng.randint(100, 99999)) / 100
        a = (u * q).quantize(Decimal("0.01"))
        items.append({"description": desc, "quantity": qty(q), "unit_price": money(u), "amount": money(a)})
        sub += a
    rate = rng.choice([0, 5, 8, 10, 20])
    tax = (sub * rate / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    day = dt.date(2024, 1, 1) + dt.timedelta(days=rng.randint(0, 700))
    gold = {
        "invoice_number": f"{rng.choice(['INV', 'R', 'BL'])}-{day.year}-{rng.randint(1, 9999):04d}",
        "invoice_date": day.isoformat(), "vendor": rng.choice(VENDORS), "currency": currency,
        "line_items": items, "subtotal": money(sub), "tax": money(tax), "total": money(sub + tax),
    }
    return gold, rate, day


def _render(t: str, g: dict, rate: int, day: dt.date, rng: random.Random) -> str:
    cur, eu = g["currency"], t == "eu_table"
    f = lambda v: _fmt(v, eu)
    sym = {"USD": "$", "GBP": "£", "EUR": "€"}[cur]
    has_tax = g["tax"] != "0.00"
    L = []
    if t == "us_plain":
        L += [g["vendor"], f"INVOICE #{g['invoice_number']}", f"Date: {g['invoice_date']}",
              f"Bill To: {rng.choice(CLIENTS)}", "Description Qty Unit Price Amount"]
        L += [f"{i['description']} {i['quantity']} {f(i['unit_price'])} {f(i['amount'])}" for i in g["line_items"]]
        L.append(f"Subtotal: {f(g['subtotal'])}")
        if has_tax:
            L.append(f"Tax ({rate}%): {f(g['tax'])}")
        L.append(f"Total: {cur} {f(g['total'])}")
    elif t == "eu_table":
        L += [g["vendor"], f"Invoice No: {g['invoice_number']}",
              f"Invoice Date: {day.day:02d}.{day.month:02d}.{day.year}", "Item Qty x Price = Amount"]
        L += [f"{i['description']} {i['quantity']} x {f(i['unit_price'])} = {f(i['amount'])}" for i in g["line_items"]]
        L.append(f"Net total {f(g['subtotal'])} {cur}")
        if has_tax:
            L.append(f"VAT {rate}% {f(g['tax'])} {cur}")
        L.append(f"Amount due: {f(g['total'])} {cur}")
    elif t == "pipe_md":
        due = day + dt.timedelta(days=30)
        L += [f"# {g['vendor']}", f"Invoice number: {g['invoice_number']}",
              f"Issued: {MON[day.month - 1]} {day.day}, {day.year}",
              f"Due Date: {MON[due.month - 1]} {due.day}, {due.year}", "| Description | Qty | Unit | Amount |"]
        L += [f"| {i['description']} | {i['quantity']} | {sym}{f(i['unit_price'])} | {sym}{f(i['amount'])} |"
              for i in g["line_items"]]
        L.append(f"Sub-total {sym}{f(g['subtotal'])}")
        if has_tax:
            L.append(f"Sales tax {sym}{f(g['tax'])}")
        L.append(f"Grand Total {sym}{f(g['total'])}")
    else:  # qty_marker: no unit price column
        L += [g["vendor"], f"Invoice: {g['invoice_number']}",
              f"Date: {day.day} {MON[day.month - 1]} {day.year}", "Item  Qty  Amount"]
        L += [f"{i['description']} x{i['quantity']} {f(i['amount'])}" for i in g["line_items"]]
        L.append(f"Subtotal {f(g['subtotal'])}")
        if has_tax:
            L.append(f"Tax {f(g['tax'])}")
        L.append(f"TOTAL DUE {cur} {f(g['total'])}")
    return "\n".join(L) + "\n"


def _corrupt(text: str, rng: random.Random):
    lines = text.splitlines()
    idx = [i for i, l in enumerate(lines) if any(ch.isdigit() for ch in l)
           and i > 3 and not l.lstrip().startswith("|  Description")]
    idx = [i for i in idx if "Description" not in lines[i] and not lines[i].startswith(("Item", "| Description"))]
    i = rng.choice(idx)
    kind = rng.choice(["ocr_letter", "digit_swap", "drop_line"])
    if kind == "drop_line":
        if len(lines[i]) and not lines[i].lower().startswith(("subtotal", "sub-total", "net total", "total", "grand", "amount", "tax", "vat", "sales")):
            del lines[i]
        else:
            kind = "digit_swap"
    if kind != "drop_line":
        pos = [k for k, ch in enumerate(lines[i]) if ch.isdigit()]
        # only touch the numeric tail so the invoice number/date stay clean
        pos = [k for k in pos if k >= len(lines[i].rstrip()) - 7]
        k = rng.choice(pos)
        ch = lines[i][k]
        new = rng.choice("Ol") if kind == "ocr_letter" else rng.choice([c for c in "0123456789" if c != ch])
        lines[i] = lines[i][:k] + new + lines[i][k + 1:]
    return "\n".join(lines) + "\n", kind


def make_doc(rng: random.Random, corrupt_p: float = 0.25) -> Doc:
    t = rng.choice(TEMPLATES)
    cur = {"us_plain": rng.choice(["USD", "GBP"]), "eu_table": "EUR",
           "pipe_md": rng.choice(["USD", "GBP"]), "qty_marker": rng.choice(["USD", "EUR"])}[t]
    gold, rate, day = _gold(rng, cur)
    text = _render(t, gold, rate, day, rng)
    kind = None
    if rng.random() < corrupt_p:
        text, kind = _corrupt(text, rng)
    return Doc(text, gold, t, kind)


def make_set(seed: int, n: int, corrupt_p: float = 0.25) -> list[Doc]:
    rng = random.Random(seed)
    return [make_doc(rng, corrupt_p) for _ in range(n)]
