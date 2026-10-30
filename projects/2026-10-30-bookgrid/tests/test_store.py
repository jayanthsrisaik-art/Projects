import threading

import pytest

from bookgrid.store import BookingError, naive_book

from conftest import req


def test_single_booking_and_listing(store, london):
    status, body, replayed = store.book(req(london.id))
    assert status == 201 and not replayed
    [occ] = body["booked"]
    assert occ["local_start"] == "2026-11-02T10:00+00:00"
    listed = store.list_occurrences(london.id, "2026-11-02", "2026-11-02")
    assert [o["title"] for o in listed] == ["Meeting"]


def test_buffer_blocks_back_to_back_but_allows_after_gap(store, london):
    store.book(req(london.id, start="10:00", dur=60))
    with pytest.raises(BookingError) as err:
        store.book(req(london.id, start="11:00", dur=30, title="too close"))
    assert err.value.status == 409
    assert err.value.details[0]["problems"][0]["kind"] == "conflict"
    # The buffer also applies before an existing booking: 09:00-09:55 + 10 > 10:00.
    with pytest.raises(BookingError):
        store.book(req(london.id, start="09:00", dur=55))
    assert store.book(req(london.id, start="11:10", dur=30))[0] == 201
    assert store.book(req(london.id, start="09:00", dur=50))[0] == 201


def test_half_open_intervals_without_buffer(store, newyork):
    store.book(req(newyork.id, start="10:00", dur=60))
    assert store.book(req(newyork.id, start="11:00", dur=60))[0] == 201
    assert store.book(req(newyork.id, start="09:00", dur=60))[0] == 201


def test_opening_hours_and_closed_days(store, london):
    with pytest.raises(BookingError) as err:
        store.book(req(london.id, start="17:30", dur=60))
    assert err.value.details[0]["problems"][0]["kind"] == "outside_hours"
    with pytest.raises(BookingError) as err:
        store.book(req(london.id, date="2026-11-07"))  # Saturday
    assert err.value.details[0]["problems"][0]["kind"] == "closed_day"


def test_recurring_reject_is_all_or_nothing(store, london):
    store.book(req(london.id, date="2026-11-04", title="blocker"))
    with pytest.raises(BookingError) as err:
        store.book(req(london.id, date="2026-11-02", rrule="FREQ=DAILY;COUNT=5"))
    assert err.value.status == 409
    assert [d["recurrence_date"] for d in err.value.details] == ["2026-11-04"]
    listed = store.list_occurrences(london.id, "2026-11-01", "2026-11-30")
    assert [o["title"] for o in listed] == ["blocker"], "nothing from the series was written"


def test_recurring_skip_books_the_rest(store, london):
    store.book(req(london.id, date="2026-11-04", title="blocker"))
    _, body, _ = store.book(req(london.id, date="2026-11-02", rrule="FREQ=WEEKLY;BYDAY=MO,WE,FR;COUNT=6",
                                on_conflict="skip"))
    assert [o["recurrence_date"] for o in body["skipped"]] == ["2026-11-04"]
    assert len(body["booked"]) == 5
    assert body["skipped"][0]["problems"][0]["with"][0]["title"] == "blocker"


def test_skip_mode_with_everything_blocked_is_still_409(store, london):
    with pytest.raises(BookingError) as err:
        store.book(req(london.id, date="2026-11-07", rrule="FREQ=WEEKLY;BYDAY=SA,SU;COUNT=2",
                       on_conflict="skip"))
    assert err.value.status == 409


def test_dst_recurring_series_keeps_local_time(store, newyork):
    _, body, _ = store.book(req(newyork.id, date="2026-10-29", start="11:00",
                                rrule="FREQ=DAILY;COUNT=5"))
    assert [o["start_utc"][11:16] for o in body["booked"]] == ["15:00", "15:00", "15:00", "16:00", "16:00"]
    assert {o["local_start"][11:16] for o in body["booked"]} == {"11:00"}


def test_gap_note_is_stored(store, newyork):
    _, body, _ = store.book(req(newyork.id, date="2027-03-13", start="02:30", rrule="FREQ=DAILY;COUNT=2"))
    a, b = body["booked"]
    assert a["note"] is None and a["local_start"].startswith("2027-03-13T02:30")
    assert b["local_start"].startswith("2027-03-14T03:30") and "does not exist" in b["note"]


def test_idempotency_replay_and_mismatch(store, london):
    r = req(london.id)
    first = store.book(r, "key-12345678")
    again = store.book(r, "key-12345678")
    assert again[2] is True and again[1] == first[1]
    assert len(store.list_occurrences(london.id, "2026-11-02", "2026-11-02")) == 1
    with pytest.raises(BookingError, match="different body"):
        store.book(req(london.id, start="14:00"), "key-12345678")


def test_failed_booking_is_not_cached_under_key(store, london):
    store.book(req(london.id, title="blocker"))
    with pytest.raises(BookingError):
        store.book(req(london.id, start="10:30", title="late"), "retry-key-1")
    occ = store.list_occurrences(london.id, "2026-11-02", "2026-11-02")[0]
    store.cancel(occ["id"])
    assert store.book(req(london.id, start="10:30", title="late"), "retry-key-1")[0] == 201


def test_cancel_scopes(store, london):
    _, body, _ = store.book(req(london.id, rrule="FREQ=DAILY;COUNT=5"))
    ids = [o["id"] for o in body["booked"]]
    assert store.cancel(ids[1], "this")["cancelled"] == 1
    assert store.cancel(ids[1], "this")["cancelled"] == 0  # idempotent
    assert store.cancel(ids[3], "following")["cancelled"] == 2
    assert store.cancel(ids[0], "series")["cancelled"] == 2
    assert store.list_occurrences(london.id, "2026-11-01", "2026-11-30") == []
    # Cancelled slots are free again.
    assert store.book(req(london.id))[0] == 201


def test_move_checks_conflicts_but_ignores_itself(store, london):
    _, a, _ = store.book(req(london.id, start="10:00", title="A"))
    store.book(req(london.id, start="13:00", title="B"))
    oid = a["booked"][0]["id"]
    moved = store.move(oid, "2026-11-02", "10:30")  # overlaps only its old self
    assert moved["local_start"].endswith("10:30+00:00") and moved["moved"]
    with pytest.raises(BookingError) as err:
        store.move(oid, "2026-11-02", "12:30")
    assert err.value.status == 409
    store.cancel(oid)
    with pytest.raises(BookingError, match="cancelled"):
        store.move(oid, "2026-11-02", "15:00")


def test_validation_errors(store, london):
    for bad in (req(london.id, dur=3), req(london.id, start="25:00"), req(london.id, date="2026-13-01"),
                req(london.id, rrule="FREQ=YEARLY;COUNT=2"), req(999)):
        with pytest.raises(BookingError):
            store.book(bad)
    with pytest.raises(BookingError, match="timezone"):
        store.create_resource("X", "Mars/Olympus")
    with pytest.raises(BookingError, match="already exists"):
        store.create_resource("Thames", "Europe/London")


def test_free_slots_agree_with_booking(store, london):
    store.book(req(london.id, start="09:30", dur=15, title="standup"))
    store.book(req(london.id, start="14:00", dur=60, title="review"))
    free = store.free_slots(london.id, "2026-11-02", 60, 30)
    starts = [s["local_start"][11:16] for s in free["slots"]]
    assert starts == ["08:00", "10:00", "10:30", "11:00", "11:30", "12:00", "12:30",
                      "15:30", "16:00", "16:30", "17:00"]
    # Every grid start is bookable iff free_slots offered it.
    for h in range(8, 18):
        for m in (0, 30):
            t = f"{h:02d}:{m:02d}"
            ok = store.preview(req(london.id, start=t, dur=60))["blocked"] == 0
            assert ok == (t in starts), t


def test_free_slots_closed_day(store, london):
    assert store.free_slots(london.id, "2026-11-08", 30)["closed"] is True


def test_transactional_booking_never_double_books(store, london):
    wins = []

    def client(i):
        try:
            if store.book(req(london.id, title=f"c{i}"))[0] == 201:
                wins.append(i)
        except BookingError:
            pass

    threads = [threading.Thread(target=client, args=(i,)) for i in range(12)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(wins) == 1
    assert len(store.list_occurrences(london.id, "2026-11-02", "2026-11-02")) == 1


def test_naive_baseline_can_double_book(store, london):
    barrier = threading.Barrier(6)
    results = []

    def client(i):
        barrier.wait()
        results.append(naive_book(store, req(london.id, title=f"n{i}"), pause=0.05))

    threads = [threading.Thread(target=client, args=(i,)) for i in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sum(results) > 1  # the bug the transactional path prevents
