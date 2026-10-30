"""Race benchmark: many clients book the same slot at once.

Compares the transactional ``Store.book`` against ``naive_book``
(check-then-insert without holding the write lock) and counts how often each
ends up with more than one booking in the slot.
"""
from __future__ import annotations

import tempfile
import threading
from pathlib import Path

from .store import BookingError, BookingRequest, Store, naive_book


def _trial(mode: str, clients: int, pause: float, workdir: Path, n: int) -> int:
    store = Store(str(workdir / f"race-{mode}-{n}.db"))
    rid = store.create_resource("Room", "Europe/London", "08:00", "18:00").id
    barrier = threading.Barrier(clients)
    wins: list[int] = []
    lock = threading.Lock()

    def client(i: int) -> None:
        req = BookingRequest(rid, f"client {i}", f"user{i}", "2026-11-02", "10:00", 60)
        barrier.wait()
        try:
            ok = naive_book(store, req, pause) if mode == "naive" else store.book(req)[0] == 201
        except BookingError:
            ok = False
        if ok:
            with lock:
                wins.append(i)

    threads = [threading.Thread(target=client, args=(i,)) for i in range(clients)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return len(wins)


def run_race(trials: int = 20, clients: int = 8, pause: float = 0.02) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    with tempfile.TemporaryDirectory() as tmp:
        for mode in ("naive", "transactional"):
            winners = [_trial(mode, clients, pause, Path(tmp), n) for n in range(trials)]
            out[mode] = {
                "trials": trials,
                "clients_per_trial": clients,
                "trials_double_booked": sum(w > 1 for w in winners),
                "max_bookings_in_one_slot": max(winners),
                "mean_bookings_per_slot": sum(winners) / trials,
            }
    return out
