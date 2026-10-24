from invoiceiq.local import LocalExtractor
from invoiceiq.pipeline import normalize, run
from tests.test_local import QTY, US


def test_repair_recovers_qty_marker():
    r = run(LocalExtractor(), QTY)
    assert r.status == "ok" and r.attempts == 2
    assert r.history[0] and r.history[1] == []


def test_single_attempt_flags_it():
    assert run(LocalExtractor(), QTY, max_attempts=1).status == "needs_review"


def test_clean_doc_one_attempt():
    r = run(LocalExtractor(), US)
    assert r.status == "ok" and r.attempts == 1


def test_corrupted_doc_routed_to_review_not_accepted():
    r = run(LocalExtractor(), US.replace("81.64", "81.84"))
    assert r.status == "needs_review"
    assert any("!= total" in e for e in r.errors)


class Scripted:
    """Fake LLM: wrong first, right once it sees feedback."""
    name = "scripted"

    def __init__(self):
        self.calls = []

    def extract(self, text, feedback=None, attempt=0):
        self.calls.append(feedback)
        d = LocalExtractor().extract(US)
        if feedback is None:
            d["total"] = "999.00"
        return d


def test_feedback_is_passed_to_extractor():
    ex = Scripted()
    r = run(ex, US)
    assert r.status == "ok" and r.attempts == 2
    assert ex.calls[0] is None and any("!= total" in e for e in ex.calls[1])


class Crashy:
    name = "crashy"

    def extract(self, text, feedback=None, attempt=0):
        raise RuntimeError("api down")


def test_extractor_exception_becomes_review():
    r = run(Crashy(), "x")
    assert r.status == "needs_review" and "api down" in r.errors[0]


class Stuck:
    name = "stuck"
    n = 0

    def extract(self, text, feedback=None, attempt=0):
        Stuck.n += 1
        return {}


def test_stops_when_failure_repeats():
    Stuck.n = 0
    run(Stuck(), "x", max_attempts=5)
    assert Stuck.n == 2


def test_normalize_idempotent():
    d = LocalExtractor().extract(US)
    assert normalize(normalize(d)) == normalize(d)
