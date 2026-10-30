import threading

import pytest
from conftest import NOW, utc

from slotwise.availability import iso_z
from slotwise.store import BadToken, Conflict, NotFound, SlotUnavailable, Store

MON_10 = utc(2026, 11, 2, 15, 0)  # Mon 10:00 New York


def open_slots(store, slug="intro-call", day=(2026, 11, 2)):
    a = utc(*day)
    return [iso_z(s) for s in store.available_slots(slug, a, a.replace(day=a.day + 1), NOW)]


def test_booking_removes_slot_and_buffer_neighbour(store):
    before = open_slots(store)
    b, token, replayed = store.book("intro-call", MON_10, "Ada", "ada@example.com", NOW)
    assert not replayed and b.status == "confirmed" and b.host_date == "2026-11-02"
    after = open_slots(store)
    # 10:00 taken; 10:30 lost to this booking's 15-min after-buffer; 09:30 lost
    # because a 09:30 booking's own buffer (until 10:15) would overlap it.
    assert set(before) - set(after) == {"2026-11-02T14:30:00Z", "2026-11-02T15:00:00Z", "2026-11-02T15:30:00Z"}


def test_rejects_unaligned_and_unavailable_starts(store):
    with pytest.raises(SlotUnavailable):
        store.book("intro-call", utc(2026, 11, 2, 15, 10), "A", "a@x.io", NOW)  # off-grid
    with pytest.raises(SlotUnavailable):
        store.book("intro-call", utc(2026, 11, 2, 17, 0), "A", "a@x.io", NOW)  # lunch
    with pytest.raises(SlotUnavailable):
        store.book("intro-call", utc(2026, 10, 30, 13, 0), "A", "a@x.io", NOW)  # inside notice
    with pytest.raises(NotFound):
        store.book("nope", MON_10, "A", "a@x.io", NOW)


def test_concurrent_requests_for_one_slot_book_exactly_once(store):
    results, barrier = [], threading.Barrier(12)

    def worker(i):
        s = Store(store.path)  # separate connections, like separate API workers
        barrier.wait()
        try:
            s.book("intro-call", MON_10, f"user{i}", f"u{i}@example.com", NOW)
            results.append("ok")
        except SlotUnavailable:
            results.append("taken")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count("ok") == 1 and results.count("taken") == 11
    assert len([b for b in store.list_bookings("intro-call") if b.status == "confirmed"]) == 1


def test_concurrent_adjacent_slots_respect_buffer(store):
    # 10:00 and 10:30 are both open, but booking one blocks the other.
    results = []
    barrier = threading.Barrier(2)

    def worker(start):
        s = Store(store.path)
        barrier.wait()
        try:
            s.book("intro-call", start, "x", "x@example.com", NOW)
            results.append(iso_z(start))
        except SlotUnavailable:
            pass

    ts = [threading.Thread(target=worker, args=(t,)) for t in (MON_10, utc(2026, 11, 2, 15, 30))]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(results) == 1


def test_daily_cap_is_enforced_at_write_time(store):
    for h in (14, 15, 16, 18):  # 09:00, 10:00, 11:00, 13:00 NY
        store.book("intro-call", utc(2026, 11, 2, h, 0), "a", "a@x.io", NOW)
    assert open_slots(store) == []
    with pytest.raises(SlotUnavailable):
        store.book("intro-call", utc(2026, 11, 2, 20, 0), "a", "a@x.io", NOW)


def test_idempotent_replay_and_key_misuse(store):
    b1, t1, r1 = store.book("intro-call", MON_10, "Ada", "ada@x.io", NOW, idempotency_key="k1")
    b2, t2, r2 = store.book("intro-call", MON_10, "Ada", "ada@x.io", NOW, idempotency_key="k1")
    assert (b1.id, t1, r1) == (b2.id, t2, False) and r2 is True
    with pytest.raises(Conflict):
        store.book("intro-call", utc(2026, 11, 2, 16, 0), "Ada", "ada@x.io", NOW, idempotency_key="k1")
    assert len(store.list_bookings()) == 1


def test_cancel_frees_slot_and_is_idempotent(store):
    b, token, _ = store.book("intro-call", MON_10, "Ada", "ada@x.io", NOW)
    with pytest.raises(BadToken):
        store.cancel(b.id, "wrong")
    assert store.cancel(b.id, token).status == "cancelled"
    assert store.cancel(b.id, token).status == "cancelled"
    assert "2026-11-02T15:00:00Z" in open_slots(store)


def test_reschedule_is_atomic_and_can_shift_into_own_buffer(store):
    b, token, _ = store.book("intro-call", MON_10, "Ada", "ada@x.io", NOW)
    # 10:30 is blocked only by this booking's own buffer; moving there must work.
    new, new_token = store.reschedule(b.id, token, utc(2026, 11, 2, 15, 30), NOW)
    assert new.start == utc(2026, 11, 2, 15, 30) and new_token != token
    old = store.get_booking(b.id)
    assert old.status == "cancelled" and old.rescheduled_to == new.id
    with pytest.raises(Conflict):
        store.reschedule(b.id, token, utc(2026, 11, 2, 16, 0), NOW)
    # Failed reschedule leaves the original untouched.
    other, _, _ = store.book("intro-call", utc(2026, 11, 2, 19, 0), "Bo", "bo@x.io", NOW)
    with pytest.raises(SlotUnavailable):
        store.reschedule(new.id, new_token, other.start, NOW)
    assert store.get_booking(new.id).status == "confirmed"


def test_busy_block_hides_slots(store):
    store.add_busy_block("intro-call", utc(2026, 11, 2, 14, 0), utc(2026, 11, 2, 15, 0), "dentist")
    got = open_slots(store)
    assert "2026-11-02T14:00:00Z" not in got and "2026-11-02T14:30:00Z" not in got
    assert "2026-11-02T15:00:00Z" in got


def test_tokens_are_stored_hashed(store):
    import sqlite3
    b, token, _ = store.book("intro-call", MON_10, "Ada", "ada@x.io", NOW)
    row = sqlite3.connect(store.path).execute("SELECT token_hash FROM bookings WHERE id=?", (b.id,)).fetchone()
    assert token not in row[0] and len(row[0]) == 64
