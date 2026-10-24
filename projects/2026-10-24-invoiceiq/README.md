# invoiceiq

Invoice text → validated JSON, with a **verify-and-repair loop** and an explicit accept gate.
The point is not extraction accuracy on its own; it is that wrong extractions are *detected* and routed to
review instead of being silently accepted — the failure mode that matters when an LLM (or any extractor)
feeds a finance system.

## Problem
Free-text invoices come in different layouts, number formats (`1,234.56` vs `1.234,56`), date styles and
OCR damage. An extractor will sometimes be wrong. Invoices are redundant by construction
(`qty × unit = amount`, `Σ amounts = subtotal`, `subtotal + tax = total`), so a deterministic validator can
catch most errors and its messages can be fed back to the extractor for another attempt.

## Architecture
```
text ─► Extractor ─► normalize ─► validate ─► ok ──────────────► status "ok"
          ▲                          │
          └──── error messages ◄─────┘ (up to 3 attempts; stops early if the same failure repeats)
                                     └► still failing ─────────► status "needs_review"
```
| module | role |
|---|---|
| `local.py` | offline rule-based extractor (default). Attempt 0 strict; attempt ≥1 also reads `desc xN amount` lines |
| `llm.py` | optional Claude extractor (injectable client); gets validator errors as feedback |
| `validate.py` | arithmetic/format identities; returns human-readable errors |
| `pipeline.py` | retry loop, normalisation, never raises on extractor failure |
| `gen.py` | seeded synthetic invoices in 4 layouts + optional OCR-style corruption, with gold labels |
| `evaluate.py` | compares `single_pass` / `validated` / `repair` modes |

## Setup & usage
```bash
pip install -r requirements.txt          # only pytest; Python 3.11+
python -m invoiceiq extract samples/qty_marker.txt
python -m invoiceiq eval --set test      # or --set dev, --json
python -m invoiceiq samples samples      # regenerate samples/ (deterministic)
python -m pytest -q
```
`extract` exits 0 for `ok`, 2 for `needs_review`. Claude mode (not exercised here, see limitations):
`pip install anthropic; export ANTHROPIC_API_KEY=...; python -m invoiceiq eval --extractor claude`
(model via `INVOICEIQ_MODEL`, default `claude-haiku-5-5`).

`samples/` holds one clean and one corrupted invoice per layout with `*.expected.json` gold output.

## Measured results (local extractor, synthetic data, 25% of docs corrupted)
Run with `python -m invoiceiq eval --set <set>`. *dev* = seed 1, 60 docs (rules were written looking at it);
*test* = seed 2, 200 docs, generated after the rules were fixed.

| set | mode | accuracy | coverage | accepted precision | silent errors | corrupted docs flagged |
|---|---|---|---|---|---|---|
| test | single_pass (no validation) | 0.525 | 1.000 | 0.525 | 95 | 0/60 |
| test | validated (1 attempt) | 0.525 | 0.525 | 1.000 | 0 | 60/60 |
| test | repair (≤3 attempts) | 0.700 | 0.700 | 1.000 | 0 | 60/60 |
| dev | repair | 0.817 | 0.817 | 1.000 | 0 | 11/11 |

*accuracy* = whole-document exact match vs gold over all docs; *coverage* = share auto-accepted;
*silent errors* = accepted but wrong. Repair accuracy equals the clean-document share (140/200): every clean
document is recovered, every corrupted one is sent to review, none are wrongly accepted. Per-field accuracy
(test, repair) is in the CLI output; invoice number, date, vendor and currency are 1.000.

## Design decisions
- **Exact arithmetic (tolerance 0).** A first run with a 1-cent tolerance let a last-digit swap in a subtotal
  (…44 → …43) through as "valid"; a test caught it, so the default is exact and `validate(tol=...)` is opt-in.
- **Reject, don't guess.** OCR letters (`2O.00`) are not parsed as numbers; the line is dropped and validation fails.
- **Lenient repair is scoped** to lines carrying an explicit `xN` marker, so it cannot turn a damaged 3-number
  line into a self-consistent but wrong one.
- **Extractor failures are data**: exceptions and unparsable model replies become `needs_review`.
- Stdlib only for the core; the Anthropic SDK is a lazy optional import.

## Limitations
- Evaluation data is synthetic and the corruption model is simple (one damaged line per document); the 100%
  accepted precision applies to *this* generator, not to real invoices. Passing the identities does not prove
  correctness (vendor, number or date errors, or errors that cancel out, are invisible to them).
- The local extractor knows four layouts and English labels; unknown layouts end in `needs_review`.
- Only USD/EUR/GBP, single tax line, no discounts/shipping/multi-page invoices.
- Exact tolerance may over-reject real invoices with per-line tax rounding.
- The Claude extractor is tested only against a fake client; it has **not** been run against the live API
  and no accuracy numbers are claimed for it.
