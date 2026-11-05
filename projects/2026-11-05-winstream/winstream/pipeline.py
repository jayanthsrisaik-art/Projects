"""Source -> operator -> sink loop with periodic exactly-once checkpoints."""
from __future__ import annotations

from dataclasses import dataclass

from .engine import Operator
from .model import InvalidEvent, WindowSpec, parse_event
from .sink import CrashInjected, Sink


@dataclass
class RunReport:
    read: int
    resumed_from: int
    commits: int
    emissions: int
    flushed: bool
    stats: dict
    open_windows: int


def run(events_path: str, db_path: str, spec: WindowSpec, delay: int, lateness: int,
        checkpoint_every: int = 500, flush: bool = True,
        crash_after: int | None = None, crash_in_commit: int | None = None) -> RunReport:
    """Process ``events_path`` (JSONL, one event per line, arrival order).

    Resumes from the checkpoint in ``db_path`` if one exists. ``crash_after``
    raises CrashInjected after that many records were processed in this
    call without committing; ``crash_in_commit`` raises inside the N-th commit
    transaction (1-based) right before COMMIT.
    """
    if checkpoint_every <= 0:
        raise ValueError("checkpoint_every must be > 0")
    config = {"window": spec.describe(), "delay": delay, "lateness": lateness}
    sink = Sink(db_path)
    try:
        cp = sink.load_checkpoint()
        if cp is None:
            op = Operator(spec, delay, lateness)
            offset, seq = 0, 1
        else:
            if cp["config"] != config:
                raise ValueError(f"database was built with {cp['config']}, refusing to resume with {config}")
            op = Operator.from_state(spec, cp["state"])
            offset, seq = cp["next_offset"], cp["next_seq"]
        resumed_from = offset

        pending: list[dict] = []
        side: list[dict] = []
        commits = 0
        emitted = 0
        processed = 0
        since_commit = 0

        def commit(next_offset):
            nonlocal seq, commits, pending, side, emitted, since_commit
            commits += 1
            seq = sink.commit(pending, side, next_offset, seq, config, op.to_state(),
                              crash_before_commit=(crash_in_commit == commits))
            emitted += len(pending)
            pending, side, since_commit = [], [], 0

        with open(events_path, encoding="utf-8") as fh:
            for lineno, line in enumerate(fh):
                if lineno < offset:
                    continue
                if not line.strip():
                    offset = lineno + 1
                    continue
                if op.flushed:
                    raise ValueError("input grew after the stream was flushed; "
                                     "use --no-flush for incremental runs")
                try:
                    e = parse_event(line)
                except InvalidEvent as exc:
                    side.append({"reason": "invalid", "offset": lineno, "detail": str(exc)})
                else:
                    out, s = op.process(e, lineno)
                    pending.extend(out)
                    side.extend(s)
                processed += 1
                since_commit += 1
                if crash_after is not None and processed >= crash_after:
                    raise CrashInjected(f"crash injected after {processed} records")
                if since_commit >= checkpoint_every:
                    commit(lineno + 1)
                offset = lineno + 1
        if flush and not op.flushed:
            pending.extend(op.flush())
            commit(offset)
        elif since_commit or pending or side:
            commit(offset)
        return RunReport(processed, resumed_from, commits, emitted, op.flushed,
                         dict(op.stats), op.open_windows())
    finally:
        sink.close()


def run_until_done(events_path, db_path, spec, delay, lateness, checkpoint_every=500,
                   flush=True, crash_schedule=()):
    """Run with a list of injected crashes, restarting after each one.

    ``crash_schedule`` items are ("after", n) or ("commit", n). Returns the
    number of crashes survived and the final report.
    """
    crashes = 0
    for kind, n in list(crash_schedule) + [(None, None)]:
        try:
            report = run(events_path, db_path, spec, delay, lateness, checkpoint_every, flush,
                         crash_after=n if kind == "after" else None,
                         crash_in_commit=n if kind == "commit" else None)
            return crashes, report
        except CrashInjected:
            crashes += 1
    raise AssertionError("unreachable")
