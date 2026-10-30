from conftest import NOW, utc

from slotwise.ics import _fold, booking_ics


def test_ics_escaping_folding_and_status(store, ets):
    b, token, _ = store.book("intro-call", utc(2026, 11, 2, 15), "Lovelace, Ada; Countess", "ada@example.com", NOW)
    text = booking_ics(b, ets["intro-call"])
    assert "\r\n" in text and "\n" not in text.replace("\r\n", "")
    unfolded = text.replace("\r\n ", "")
    assert "SUMMARY:Intro call with Lovelace\\, Ada\\; Countess" in unfolded
    assert "DTSTAMP:20261030T120000Z" in text and "STATUS:CONFIRMED" in text
    assert all(len(line.encode()) <= 75 for line in text.split("\r\n"))
    store.cancel(b.id, token)
    assert "STATUS:CANCELLED" in booking_ics(store.get_booking(b.id), ets["intro-call"])


def test_fold_respects_multibyte_boundaries():
    line = "SUMMARY:" + "é" * 80
    folded = _fold(line)
    assert folded.replace("\r\n ", "") == line
    assert all(len(p.encode()) <= 75 for p in folded.split("\r\n"))
