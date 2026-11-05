"""Event-time window operator.

Semantics (all times in event-time seconds unless noted):

* watermark ``wm = max(ts seen) - delay``; it never moves backwards and is
  undefined (None) before the first accepted record.
* A window ``[start, end)`` *fires* (first result, ``insert``) once ``wm >= end``.
* After firing, a window is kept for ``lateness`` more seconds of watermark.
  A late event that lands in a kept window produces an ``update`` with a
  higher revision. The window is purged once ``end + lateness <= wm``.
* Fixed windows (tumbling/hopping): an (event, window) pair is dropped as
  late when ``end + lateness <= wm`` at the moment the event arrives.
* Session windows: an event is dropped when ``ts < wm - lateness`` *unless*
  it falls inside a session that is still held (``start <= ts <= last event``),
  in which case it only refines that session. Both cases guarantee an accepted
  event can never overlap a session that has already been purged, so the
  streaming result equals the batch sessionisation of the accepted events. When a late event merges sessions that were already
  emitted, the old results are ``retract``-ed and the merged session is
  emitted as a fresh ``insert`` once the watermark is past its end.
* At end of input ``flush()`` advances the watermark to +infinity.

The operator is deterministic: the same input in the same order produces the
same emissions, which is what makes checkpoint/replay exactly-once.
"""
from __future__ import annotations

import heapq
from dataclasses import dataclass

from .model import Acc, Event, WindowSpec


@dataclass
class WinState:
    acc: Acc
    emitted: bool = False
    revision: int = 0


class Operator:
    def __init__(self, spec: WindowSpec, delay: int, lateness: int):
        if delay < 0 or lateness < 0:
            raise ValueError("delay and lateness must be >= 0")
        self.spec = spec
        self.delay = delay
        self.lateness = lateness
        self.max_ts: int | None = None
        self.flushed = False
        # key -> {(start, end): WinState}
        self.windows: dict[str, dict[tuple[int, int], WinState]] = {}
        self._fire_heap: list[tuple[int, str, int]] = []   # (end, key, start)
        self._purge_heap: list[tuple[int, str, int]] = []  # (end + lateness, key, start)
        self.stats = {"events": 0, "pairs_accepted": 0, "pairs_late": 0,
                      "duplicates": 0, "inserts": 0, "updates": 0, "retracts": 0}

    # ------------------------------------------------------------------ time
    @property
    def watermark(self) -> int | None:
        if self.flushed:
            return None
        return None if self.max_ts is None else self.max_ts - self.delay

    def _passed(self, t: int) -> bool:
        """True when the watermark is >= t."""
        if self.flushed:
            return True
        wm = self.watermark
        return wm is not None and wm >= t

    # ------------------------------------------------------------ emissions
    def _emit(self, out: list, kind: str, key: str, w: tuple[int, int], st: WinState, ctx: dict):
        rec = {"kind": kind, "key": key, "wstart": w[0], "wend": w[1],
               "revision": st.revision, **st.acc.values(),
               "wm": self.watermark, "trigger_offset": ctx.get("offset"),
               "trigger_arrival": ctx.get("arrival")}
        out.append(rec)
        self.stats[kind + "s"] += 1

    def _track(self, key: str, w: tuple[int, int]):
        heapq.heappush(self._fire_heap, (w[1], key, w[0]))
        heapq.heappush(self._purge_heap, (w[1] + self.lateness, key, w[0]))

    # -------------------------------------------------------------- process
    def process(self, e: Event, offset: int | None = None):
        """Feed one event. Returns (emissions, side_outputs)."""
        if self.flushed:
            raise RuntimeError("operator already flushed")
        out: list[dict] = []
        side: list[dict] = []
        ctx = {"offset": offset, "arrival": e.arrival}
        self.stats["events"] += 1
        if self.spec.merging:
            self._process_session(e, out, side, ctx)
        else:
            self._process_fixed(e, out, side, ctx)
        if self.max_ts is None or e.ts > self.max_ts:
            self.max_ts = e.ts
        self._fire(out, ctx)
        self._purge()
        return out, side

    def _side(self, side, e, reason, w=None, offset=None):
        side.append({"offset": offset, "event_id": e.id, "key": e.key, "ts": e.ts,
                     "reason": reason, "wstart": None if w is None else w[0],
                     "wend": None if w is None else w[1], "wm": self.watermark})

    def _process_fixed(self, e: Event, out, side, ctx):
        kw = self.windows.setdefault(e.key, {})
        dup = False
        for w in self.spec.assign(e.ts):
            if self._passed(w[1] + self.lateness):
                self.stats["pairs_late"] += 1
                self._side(side, e, "late", w, ctx["offset"])
                continue
            st = kw.get(w)
            if st is None:
                st = kw[w] = WinState(Acc())
                self._track(e.key, w)
            if not st.acc.add(e):
                dup = True
                continue
            self.stats["pairs_accepted"] += 1
            if st.emitted:
                st.revision += 1
                self._emit(out, "update", e.key, w, st, ctx)
        if dup:
            self.stats["duplicates"] += 1
            self._side(side, e, "duplicate", None, ctx["offset"])
        if not kw:
            del self.windows[e.key]

    def _process_session(self, e: Event, out, side, ctx):
        wm = self.watermark
        kw = self.windows.setdefault(e.key, {})
        if wm is not None and e.ts < wm - self.lateness and not any(
                w[0] <= e.ts and e.ts + self.spec.gap <= w[1] for w in kw):
            self.stats["pairs_late"] += 1
            self._side(side, e, "late", (e.ts, e.ts + self.spec.gap), ctx["offset"])
            if not kw:
                del self.windows[e.key]
            return
        if any(e.id in st.acc.ids for st in kw.values()):
            self.stats["duplicates"] += 1
            self._side(side, e, "duplicate", None, ctx["offset"])
            return
        self.stats["pairs_accepted"] += 1
        start, end = e.ts, e.ts + self.spec.gap
        overlapping = sorted(w for w in kw if w[0] < end and w[1] > start)
        acc = Acc()
        acc.add(e)
        for w in overlapping:
            start, end = min(start, w[0]), max(end, w[1])
        merged = (start, end)
        if len(overlapping) == 1 and overlapping[0] == merged:
            # Event fell inside an existing session: bounds unchanged.
            st = kw[merged]
            st.acc.merge(acc)
            if st.emitted:
                st.revision += 1
                self._emit(out, "update", e.key, merged, st, ctx)
            return
        for w in overlapping:
            old = kw.pop(w)
            if old.emitted:
                self._emit(out, "retract", e.key, w, old, ctx)
            acc.merge(old.acc)
        kw[merged] = WinState(acc)
        self._track(e.key, merged)

    def _fire(self, out, ctx):
        h = self._fire_heap
        while h and self._passed(h[0][0]):
            end, key, start = heapq.heappop(h)
            st = self.windows.get(key, {}).get((start, end))
            if st is None or st.emitted:
                continue  # stale entry (merged away / already fired)
            st.emitted = True
            self._emit(out, "insert", key, (start, end), st, ctx)

    def _purge(self):
        h = self._purge_heap
        while h and self._passed(h[0][0]):
            _, key, start = heapq.heappop(h)
            kw = self.windows.get(key)
            if not kw:
                continue
            for w in [w for w in kw if w[0] == start]:
                if self._passed(w[1] + self.lateness):
                    del kw[w]
            if not kw:
                del self.windows[key]

    def flush(self):
        """End of input: watermark -> +inf, fire everything, drop all state."""
        out: list[dict] = []
        if self.flushed:
            return out
        self.flushed = True
        self._fire(out, {"offset": None, "arrival": None})
        self._purge()
        return out

    # ----------------------------------------------------------- checkpoint
    def open_windows(self) -> int:
        return sum(len(v) for v in self.windows.values())

    def to_state(self) -> dict:
        return {
            "spec": self.spec.describe(), "delay": self.delay, "lateness": self.lateness,
            "max_ts": self.max_ts, "flushed": self.flushed, "stats": dict(self.stats),
            "windows": [
                {"key": k, "start": w[0], "end": w[1], "emitted": st.emitted,
                 "revision": st.revision, "acc": st.acc.to_state()}
                for k in sorted(self.windows) for w, st in sorted(self.windows[k].items())
            ],
        }

    @classmethod
    def from_state(cls, spec: WindowSpec, state: dict) -> "Operator":
        if state["spec"] != spec.describe():
            raise ValueError(f"checkpoint was taken with window {state['spec']}, not {spec.describe()}")
        op = cls(spec, state["delay"], state["lateness"])
        op.max_ts = state["max_ts"]
        op.flushed = state["flushed"]
        op.stats.update(state["stats"])
        for row in state["windows"]:
            w = (row["start"], row["end"])
            op.windows.setdefault(row["key"], {})[w] = WinState(
                Acc.from_state(row["acc"]), row["emitted"], row["revision"])
            op._track(row["key"], w)
        return op
