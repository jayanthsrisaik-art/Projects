"""Extract -> validate -> (feedback -> re-extract) with an explicit accept gate."""
from dataclasses import dataclass, field

from .numbers import money, qty
from .validate import validate


@dataclass
class Result:
    data: dict
    errors: list[str]
    attempts: int
    status: str  # "ok" (passed validation) | "needs_review"
    history: list[list[str]] = field(default_factory=list)


def normalize(data: dict) -> dict:
    """Canonical form so model output and gold compare field by field."""
    def m(v):
        try:
            return money(v)
        except Exception:
            return v
    out = dict(data)
    for k in ("subtotal", "tax", "total"):
        if out.get(k) is not None:
            out[k] = m(out[k])
    items = []
    for it in out.get("line_items") or []:
        it = dict(it)
        for k in ("unit_price", "amount"):
            if it.get(k) is not None:
                it[k] = m(it[k])
        try:
            it["quantity"] = qty(it["quantity"])
        except Exception:
            pass
        items.append(it)
    out["line_items"] = items
    return out


def run(extractor, text: str, max_attempts: int = 3) -> Result:
    """Never raises on bad extractions: failures become status='needs_review'."""
    feedback: list[str] | None = None
    history: list[list[str]] = []
    data, errors = {}, ["no extraction"]
    for attempt in range(max_attempts):
        try:
            data = normalize(extractor.extract(text, feedback=feedback, attempt=attempt))
            errors = validate(data)
        except Exception as e:  # extractor/API failure is a review case, not a crash
            data, errors = {}, [f"extractor error: {type(e).__name__}: {e}"]
        history.append(errors)
        if not errors:
            return Result(data, [], attempt + 1, "ok", history)
        if attempt + 1 < max_attempts and history[-1] == (history[-2] if len(history) > 1 else None):
            break  # identical failure twice: another try will not help
        feedback = errors
    return Result(data, errors, len(history), "needs_review", history)
