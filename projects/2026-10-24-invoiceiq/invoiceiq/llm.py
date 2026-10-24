"""Optional Claude extractor. Not used by tests or default evaluation.

Requires `pip install anthropic` and ANTHROPIC_API_KEY. Validation errors from
the previous attempt are sent back so the model can correct itself. The
client is injectable so the request/parse logic is testable offline.
"""
import json
import os
import re

PROMPT = """Extract the invoice below into JSON with exactly these keys:
invoice_number, invoice_date (YYYY-MM-DD), vendor, currency (USD/EUR/GBP),
line_items (list of {{description, quantity, unit_price, amount}}),
subtotal, tax, total. Money values are strings with two decimals and a '.'
decimal mark. Use "0.00" for tax if there is none. Reply with JSON only.
{feedback}
--- INVOICE ---
{text}
"""


class ClaudeExtractor:
    def __init__(self, client=None, model: str | None = None):
        self.name = "claude"
        self.model = model or os.environ.get("INVOICEIQ_MODEL", "claude-haiku-5-5")
        if client is None:
            import anthropic  # imported lazily: optional dependency
            client = anthropic.Anthropic()
        self.client = client

    def extract(self, text: str, feedback: list[str] | None = None, attempt: int = 0) -> dict:
        fb = ""
        if feedback:
            fb = "Your previous answer failed these checks, fix them:\n- " + "\n- ".join(feedback)
        msg = self.client.messages.create(
            model=self.model, max_tokens=1500,
            messages=[{"role": "user", "content": PROMPT.format(feedback=fb, text=text)}])
        raw = "".join(b.text for b in msg.content if getattr(b, "type", "text") == "text")
        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            return {}
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return {}
