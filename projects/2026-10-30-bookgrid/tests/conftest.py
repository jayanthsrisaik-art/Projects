import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bookgrid.store import BookingRequest, Store  # noqa: E402


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "test.db"))


@pytest.fixture
def london(store):
    return store.create_resource("Thames", "Europe/London", "08:00", "18:00", buffer_minutes=10)


@pytest.fixture
def newyork(store):
    return store.create_resource("Hudson", "America/New_York", "00:00", "23:59",
                                 open_days=["MO", "TU", "WE", "TH", "FR", "SA", "SU"])


def req(rid, date="2026-11-02", start="10:00", dur=60, rrule=None, title="Meeting",
        on_conflict="reject"):
    return BookingRequest(rid, title, "ana", date, start, dur, rrule, on_conflict)
