"""Seeded synthetic bug-tracker generator.

Each *bug* (latent defect) has a component, an exception type, a bug-specific
message, a call stack and a symptom phrase.  The first report of a bug is filed
at a random time; later duplicates are filed by other users and differ the way
real duplicates do:

* paraphrased titles (synonyms, different templates, dropped words);
* run-specific values in messages and traces (ids, hex addresses, paths, e-mails);
* line numbers that shift with the release, different install prefixes;
* traces that are missing, truncated, or in a different order of sections;
* a wrong or missing component field.

Hard negatives are produced on purpose: "sibling" bugs share component,
exception type and symptom wording with another bug but have a different
message key and different inner frames.  Ground truth (``report_id -> bug_id``)
is written to a separate labels file so it can never leak into features.
"""

from __future__ import annotations

import json
import re
import random
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

COMPONENTS: dict[str, dict] = {
    "checkout": {"lang": "py", "files": ["checkout/cart.py", "checkout/pricing.py", "checkout/coupons.py", "payments/gateway.py"],
                 "funcs": ["apply_coupon", "compute_total", "add_line_item", "validate_cart", "charge_card", "round_currency", "lookup_tax_rate", "reserve_stock"],
                 "objects": ["coupon", "cart total", "tax", "line item", "card payment", "discount code"],
                 "pages": ["checkout page", "cart", "payment step"]},
    "auth": {"lang": "py", "files": ["auth/session.py", "auth/tokens.py", "auth/oauth.py", "users/models.py"],
             "funcs": ["refresh_token", "decode_jwt", "login_user", "verify_otp", "load_session", "revoke_tokens", "callback_handler", "hash_password"],
             "objects": ["login", "password reset", "two-factor code", "SSO login", "session refresh", "logout"],
             "pages": ["login page", "account settings", "SSO redirect"]},
    "search": {"lang": "js", "files": ["search/query.js", "search/ranker.js", "search/filters.js", "ui/results.js"],
               "funcs": ["buildQuery", "applyFilters", "rankResults", "highlightTerms", "paginate", "parseFacet", "fetchSuggestions", "renderResults"],
               "objects": ["search results", "filters", "autocomplete", "pagination", "facet counts", "sorting"],
               "pages": ["search page", "results list", "header search box"]},
    "uploads": {"lang": "py", "files": ["uploads/storage.py", "uploads/thumbnails.py", "uploads/scanner.py", "media/encoder.py"],
                "funcs": ["store_chunk", "make_thumbnail", "scan_file", "finalize_upload", "detect_mime", "resize_image", "transcode_video", "compute_checksum"],
                "objects": ["file upload", "image thumbnail", "video upload", "attachment", "avatar upload", "bulk import"],
                "pages": ["upload dialog", "media library", "profile picture"]},
    "billing": {"lang": "py", "files": ["billing/invoices.py", "billing/proration.py", "billing/plans.py", "billing/webhooks.py"],
                "funcs": ["generate_invoice", "prorate_change", "apply_credit", "sync_plan", "handle_webhook", "format_amount", "close_period", "calculate_usage"],
                "objects": ["invoice", "plan upgrade", "proration", "refund", "usage charges", "credit note"],
                "pages": ["billing page", "invoice PDF", "plan picker"]},
    "notifications": {"lang": "js", "files": ["notify/dispatcher.js", "notify/templates.js", "notify/push.js", "ui/bell.js"],
                      "funcs": ["dispatchEvent", "renderTemplate", "sendPush", "markRead", "batchDigest", "resolveRecipients", "scheduleReminder", "loadPreferences"],
                      "objects": ["email notification", "push notification", "daily digest", "reminder", "unread badge", "mention alert"],
                      "pages": ["notification bell", "settings page", "inbox"]},
}

EXCEPTIONS = {
    "py": ["KeyError", "TypeError", "ValueError", "AttributeError", "IndexError", "ZeroDivisionError", "TimeoutError", "PermissionError"],
    "js": ["TypeError", "RangeError", "ReferenceError", "SyntaxError", "TimeoutError"],
}

KEY_WORDS = ["currency_code", "tenant_id", "locale", "plan_tier", "refresh_window", "mime_type", "page_size",
             "region", "tax_profile", "device_token", "digest_hour", "quota_limit", "facet_name", "retry_after",
             "billing_anchor", "thumbnail_size", "otp_secret", "sort_field", "template_slug", "chunk_index",
             "discount_pct", "timezone", "scope_list", "encoding", "credit_balance", "badge_count", "redirect_uri",
             "max_results", "invoice_number", "stock_level"]

MESSAGE_TEMPLATES = {
    "KeyError": ["'{k}'"],
    "TypeError": ["unsupported operand type(s) for +: 'NoneType' and '{k}'", "'{k}' object is not subscriptable",
                  "Cannot read properties of undefined (reading '{k}')", "{k} is not a function"],
    "ValueError": ["invalid literal for {k}: '{v}'", "{k} must be positive, got {n}"],
    "AttributeError": ["'NoneType' object has no attribute '{k}'"],
    "IndexError": ["list index out of range while reading {k}"],
    "ZeroDivisionError": ["division by zero in {k}"],
    "TimeoutError": ["{k} request timed out after {n}ms", "timed out waiting for {k} lock {h}"],
    "PermissionError": ["[Errno 13] Permission denied: '{p}' ({k})"],
    "RangeError": ["Invalid array length for {k}: {n}", "{k} out of range: {n}"],
    "ReferenceError": ["{k} is not defined"],
    "SyntaxError": ["Unexpected token < in JSON at position {n} ({k})"],
}

TITLE_TEMPLATES = [
    "{obj} fails with {exc}",
    "{exc} when {verb} {obj}",
    "Error {verb} {obj} on {page}",
    "Can't {verb_base} {obj} - {exc}",
    "{page}: {obj} broken",
    "{obj} crashes ({exc})",
    "{verb} {obj} throws {exc}",
    "Unable to {verb_base} {obj}",
]

VERBS = {"apply": ["applying", "using", "adding"], "load": ["loading", "opening", "viewing"],
         "save": ["saving", "submitting", "updating"], "send": ["sending", "triggering", "receiving"]}

SYNONYMS = {"fails": ["breaks", "errors out", "stops working"], "broken": ["not working", "fails", "crashes"],
            "crashes": ["fails", "blows up", "errors"], "Error": ["Crash", "Exception", "Problem"],
            "Unable": ["Cannot", "Impossible"], "throws": ["raises", "gives"]}

FILLER = ["This started this morning.", "Happens for several customers.", "Reproducible every time.",
          "Only some accounts are affected.", "Workaround: refresh the page.", "Blocking our release.",
          "Seen on Chrome and Firefox.", "Customer escalated via support.", "Not sure if related to the last deploy.",
          "Logs attached below.", "First noticed in staging."]

PREFIXES_PY = ["/srv/app", "/opt/service/releases/{ver}", "/home/deploy/app-{ver}", "/usr/src/app"]
PREFIXES_JS = ["/app/dist", "/var/www/{ver}/static", "/srv/web/build"]
FRAMEWORK_PY = [("framework/handlers.py", "dispatch"), ("framework/middleware.py", "__call__")]
FRAMEWORK_JS = [("vendor/runtime.js", "invokeHandler"), ("vendor/router.js", "handle")]


@dataclass
class Bug:
    bug_id: str
    component: str
    lang: str
    exc: str
    msg_template: str
    key: str
    frames: list[tuple[str, str, int]]  # (file, func, line) innermost last
    verb: str
    obj: str
    page: str


def _make_bug(rng: random.Random, bug_id: str, component: str, sibling_of: Bug | None) -> Bug:
    spec = COMPONENTS[component]
    lang = spec["lang"]
    exc = sibling_of.exc if sibling_of else rng.choice(EXCEPTIONS[lang])
    key = rng.choice(KEY_WORDS)
    if sibling_of:
        while key == sibling_of.key:
            key = rng.choice(KEY_WORDS)
    tmpl = rng.choice(MESSAGE_TEMPLATES[exc])
    fw = rng.choice(FRAMEWORK_PY if lang == "py" else FRAMEWORK_JS)
    depth = rng.randint(2, 4)
    inner = [(rng.choice(spec["files"]), rng.choice(spec["funcs"]), rng.randint(20, 400)) for _ in range(depth)]
    if sibling_of:  # shares the outer component frame, differs in the inner ones
        inner[0] = sibling_of.frames[1]
    frames = [(fw[0], fw[1], rng.randint(30, 200)), *inner]
    verb = sibling_of.verb if sibling_of else rng.choice(list(VERBS))
    obj = sibling_of.obj if sibling_of else rng.choice(spec["objects"])
    page = rng.choice(spec["pages"])
    return Bug(bug_id, component, lang, exc, tmpl, key, frames, verb, obj, page)


def _fill_message(rng: random.Random, bug: Bug) -> str:
    return bug.msg_template.format(
        k=bug.key, v=rng.choice(["abc", "n/a", "", "1e9", "-"]), n=rng.randint(1, 99999),
        h=f"0x{rng.getrandbits(40):x}", p=f"/var/tmp/{rng.getrandbits(32):08x}/{bug.key}.lock")


def _trace(rng: random.Random, bug: Bug, version: str, mode: str) -> str:
    frames = bug.frames
    if mode == "truncated":
        frames = frames[-2:]
    shift = rng.choice([0, 0, 3, -2, 7, 12])
    if bug.lang == "py":
        prefix = rng.choice(PREFIXES_PY).format(ver=version)
        lines = ["Traceback (most recent call last):"]
        for f, fn, ln in frames:
            lines.append(f'  File "{prefix}/{f}", line {max(1, ln + shift)}, in {fn}')
            lines.append("    ...")
        lines.append(f"{bug.exc}: {_fill_message(rng, bug)}")
    else:
        prefix = rng.choice(PREFIXES_JS).format(ver=version)
        lines = [f"Uncaught {bug.exc}: {_fill_message(rng, bug)}"]
        for f, fn, ln in reversed(frames):
            lines.append(f"    at {fn} ({prefix}/{f}:{max(1, ln + shift)}:{rng.randint(5, 80)})")
    return "\n".join(lines)


def _title(rng: random.Random, bug: Bug) -> str:
    verb_base = bug.verb
    verb = rng.choice(VERBS[bug.verb])
    t = rng.choice(TITLE_TEMPLATES).format(obj=bug.obj, exc=bug.exc, verb=verb, verb_base=verb_base, page=bug.page)
    for word, alts in SYNONYMS.items():
        pat = re.compile(rf"\b{word}\b")
        if pat.search(t) and rng.random() < 0.5:
            t = pat.sub(rng.choice(alts), t, count=1)
    t = t[0].upper() + t[1:]
    return t


def _body(rng: random.Random, bug: Bug, version: str, user: str, request_id: str) -> str:
    parts = []
    parts.append(f"Steps: go to the {bug.page} and try {rng.choice(VERBS[bug.verb])} the {bug.obj}.")
    parts.append(rng.choice(FILLER))
    if rng.random() < 0.5:
        parts.append(f"Affected user: {user}, request id {request_id}.")
    roll = rng.random()
    if roll < 0.25:
        mode = "none"
    elif roll < 0.45:
        mode = "truncated"
    else:
        mode = "full"
    if mode == "none":
        # reporter paraphrases the error instead of pasting a trace
        if rng.random() < 0.6:
            parts.append(f"The console shows `{bug.exc}: {_fill_message(rng, bug)}`.")
        else:
            parts.append(f"It just shows a generic error. Version {version}.")
    else:
        parts.append(f"Version {version}. Trace:\n" + _trace(rng, bug, version, mode))
    if rng.random() < 0.3:
        parts.append(rng.choice(FILLER))
    return "\n".join(parts)


def generate(n_bugs: int = 420, seed: int = 7, days: int = 240, sibling_rate: float = 0.35,
             start: datetime = datetime(2026, 1, 5, tzinfo=timezone.utc)) -> tuple[list[dict], dict[str, str]]:
    """Return (reports, labels) where labels maps report_id -> bug_id."""
    rng = random.Random(seed)
    bugs: list[Bug] = []
    comps = list(COMPONENTS)
    for i in range(n_bugs):
        sib = None
        if bugs and rng.random() < sibling_rate:
            sib = rng.choice(bugs)
        comp = sib.component if sib else rng.choice(comps)
        bugs.append(_make_bug(rng, f"BUG-{i:04d}", comp, sib))

    reports: list[dict] = []
    labels: dict[str, str] = {}
    for bug in bugs:
        first = start + timedelta(minutes=rng.randint(0, days * 24 * 60))
        # heavy-tailed number of duplicates: many singletons, a few popular bugs
        n_dups = min(int(rng.paretovariate(1.6)) - 1 + (1 if rng.random() < 0.45 else 0), 12)
        # most duplicates arrive within days, a long tail arrives weeks or months later
        delays = [rng.expovariate(1 / 120) if rng.random() < 0.7 else rng.uniform(0, 120 * 24) for _ in range(n_dups)]
        times = [first] + sorted(first + timedelta(hours=h) for h in delays)
        for t in times:
            if t > start + timedelta(days=days):
                continue
            # release train: a new minor version every ~30 days; some users lag one release
            minor = 2 + (t - start).days // 30 - rng.choice([0, 0, 0, 1])
            version = f"3.{max(2, minor)}.{rng.randint(0, 5)}"
            user = f"{rng.choice(['ana', 'li', 'sam', 'raj', 'kim', 'olu', 'eva'])}{rng.randint(1, 999)}@example.com"
            req = str(uuid.UUID(int=rng.getrandbits(128), version=4))
            comp = bug.component
            r = rng.random()
            if r < 0.12:
                comp = "unknown"
            elif r < 0.18:
                comp = rng.choice(comps)
            rid = f"R{len(reports):05d}"
            reports.append({
                "id": rid,
                "created_at": t.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "title": _title(rng, bug),
                "body": _body(rng, bug, version, user, req),
                "component": comp,
                "version": version,
            })
            labels[rid] = bug.bug_id
    reports.sort(key=lambda r: (r["created_at"], r["id"]))
    return reports, labels


def write(out_dir: Path, **kwargs) -> tuple[Path, Path]:
    reports, labels = generate(**kwargs)
    out_dir.mkdir(parents=True, exist_ok=True)
    rp, lp = out_dir / "reports.jsonl", out_dir / "labels.csv"
    with rp.open("w") as f:
        for r in reports:
            f.write(json.dumps(r) + "\n")
    with lp.open("w") as f:
        f.write("report_id,bug_id\n")
        for rid, bid in labels.items():
            f.write(f"{rid},{bid}\n")
    return rp, lp
