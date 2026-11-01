"""Text and stack-trace normalisation.

Bug reports for the same defect differ in exactly the places that carry no
information about the defect: memory addresses, request ids, line numbers that
shift between releases, install paths, user e-mails.  Raw lexical retrieval
treats those as rare (high-IDF) tokens and is pulled towards whichever report
happens to share an id.  This module strips them and extracts the structural
signal: exception type, a placeholder-normalised message and the call frames.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# --- placeholder substitution -------------------------------------------------

_SUBS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"https?://\S+"), " <url> "),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), " <email> "),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), " <uuid> "),
    (re.compile(r"\b0x[0-9a-f]+\b", re.I), " <hex> "),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2})?(\.\d+)?Z?\b"), " <ts> "),
    (re.compile(r"(?<![\w<])(/[\w.-]+){2,}/?"), " <path> "),
    # numbers, including ones glued to a unit ("30000ms", "5MB") but not inside identifiers ("utf8")
    (re.compile(r"(?<![A-Za-z_\d.])\d+(\.\d+)*"), "<num>"),
]


def mask_variables(text: str) -> str:
    """Replace run-specific values (ids, addresses, paths, numbers) with placeholders."""
    for pattern, repl in _SUBS:
        text = pattern.sub(repl, text)
    return re.sub(r"\s+", " ", text).strip()


# --- tokenisation -------------------------------------------------------------

STOPWORDS = frozenset(
    """a an the and or but if then when while of to in on at for from by with without into
    is are was were be been being it its this that these those i we you they he she me my our
    your their them us do does did doing have has had not no can could should would will just
    so very also get got getting gets after before again still please hi hello thanks thank
    steps expected actual result happens happened see seeing seems""".split()
)

_TOKEN = re.compile(r"<\w+>|[a-z][a-z0-9_]*|\d+")


def _stem(tok: str) -> str:
    """A deliberately tiny suffix stripper (enough to merge fail/fails/failed/failing)."""
    if tok.startswith("<") or len(tok) <= 4:
        return tok
    for suf in ("ing", "ed", "es", "s"):
        if tok.endswith(suf) and len(tok) - len(suf) >= 3:
            return tok[: -len(suf)]
    return tok


def tokens(text: str) -> list[str]:
    """Normalised tokens: placeholders masked, lowercase, stopwords dropped, light stemming.

    Identifiers like ``currency_code`` are kept whole *and* split into parts so that a
    reporter writing "currency code" still matches.
    """
    out: list[str] = []
    for tok in _TOKEN.findall(mask_variables(text).lower()):
        if tok in STOPWORDS or tok == "<num>":
            continue
        out.append(_stem(tok))
        if "_" in tok:
            out.extend(_stem(p) for p in tok.split("_") if p and p not in STOPWORDS)
    return out


def raw_tokens(text: str) -> list[str]:
    """Baseline tokenisation: lowercase alphanumeric runs, nothing else."""
    return re.findall(r"[a-z0-9]+", text.lower())


# --- stack traces -------------------------------------------------------------

_PY_FRAME = re.compile(r'^\s*File "(?P<file>[^"]+)", line (?P<line>\d+), in (?P<func>[\w<>.]+)', re.M)
_JS_FRAME = re.compile(r"^\s*at (?P<func>[\w$.<>]+) \((?P<file>[^():]+):(?P<line>\d+):\d+\)", re.M)
_EXC = re.compile(r"\b(?P<type>[A-Z][A-Za-z]*(?:Error|Exception|Timeout|Fault))\b(?::[ \t]*(?P<msg>[^\n`]*))?")


def frame_key(path: str, func: str) -> str:
    """Stable frame identity: last two path components without extension + function.

    ``/srv/app-2.3.1/app/checkout/cart.py`` and ``C:\\deploy\\app\\checkout\\cart.py``
    both become ``checkout/cart:apply_coupon``; line numbers are discarded because
    they move between releases.
    """
    parts = [p for p in re.split(r"[\\/]", path) if p]
    tail = parts[-2:] if len(parts) >= 2 else parts
    if tail:
        tail[-1] = re.sub(r"\.\w+$", "", tail[-1])
    return f"{'/'.join(tail)}:{func}"


@dataclass(frozen=True)
class Parsed:
    exc_type: str | None
    message: str  # placeholder-normalised exception message ('' if none found)
    frames: tuple[str, ...] = field(default_factory=tuple)  # innermost frame LAST

    @property
    def has_trace(self) -> bool:
        return bool(self.frames)

    @property
    def top_frame(self) -> str | None:
        return self.frames[-1] if self.frames else None


def parse_report_text(text: str) -> Parsed:
    """Extract exception type, normalised message and frames from free-form report text.

    Python tracebacks list the innermost frame last; JS stacks list it first, so JS
    frames are reversed to give a single convention (innermost last).
    """
    py = [frame_key(m["file"], m["func"]) for m in _PY_FRAME.finditer(text)]
    js = [frame_key(m["file"], m["func"]) for m in _JS_FRAME.finditer(text)]
    frames = tuple(py) if len(py) >= len(js) else tuple(reversed(js))

    exc_type, message = None, ""
    matches = list(_EXC.finditer(text))
    if matches:
        # Prefer a match that carries a message; Python puts it after the frames,
        # JS before them, so pick the longest-message candidate.
        best = max(matches, key=lambda m: len((m["msg"] or "").strip()))
        exc_type = best["type"]
        message = mask_variables((best["msg"] or "").strip())
    return Parsed(exc_type=exc_type, message=message, frames=frames)
