"""Incremental, idempotent ingestion of JSONL files into date-partitioned gzip files.

Per input file the run is: read new lines past the checkpoint -> validate -> dedupe ->
late-data check -> write partition batch files atomically -> commit state in one
transaction. Batch file names are derived from (source, line range), so a crash between
file write and state commit is repaired by simply re-running (the same files are rewritten).
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from .contract import Contract, parse_ts, validate
from .state import State


@dataclass
class Report:
    read: int = 0
    accepted: int = 0
    duplicates: int = 0
    invalid: int = 0
    late: int = 0
    malformed: int = 0
    drift: Counter = field(default_factory=Counter)
    quarantine_reasons: Counter = field(default_factory=Counter)
    partitions: set = field(default_factory=set)
    watermark: str | None = None

    def to_dict(self) -> dict:
        return {
            "read": self.read, "accepted": self.accepted, "duplicates": self.duplicates,
            "invalid": self.invalid, "late": self.late, "malformed": self.malformed,
            "unknown_fields": dict(sorted(self.drift.items())),
            "quarantine_reasons": dict(sorted(self.quarantine_reasons.items())),
            "partitions": sorted(self.partitions), "watermark": self.watermark,
        }


def _write_gz_atomic(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    # mtime=0 keeps output bytes deterministic across reruns
    with open(tmp, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz:
        for r in records:
            gz.write((json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n").encode())
    os.replace(tmp, path)


def _batch_name(source: str, start: int, end: int) -> str:
    h = hashlib.sha1(source.encode()).hexdigest()[:8]
    return f"part-{h}-{start:08d}-{end:08d}.jsonl.gz"


def ingest(contract: Contract, sources: list[str], lake: str, state_path: str,
           allowed_lateness: timedelta = timedelta(hours=24)) -> Report:
    """Ingest new lines from `sources` into `lake`. Safe to re-run."""
    lake_p = Path(lake)
    state = State(state_path)
    rep = Report()
    try:
        wm_raw = state.get_meta("max_event_time")
        max_time = parse_ts(wm_raw) if wm_raw else None
        for src in sorted(sources):
            start = state.checkpoint(src)
            good: dict[str, list[dict]] = defaultdict(list)
            quarantined: list[dict] = []
            batch_keys: dict[str, str] = {}
            line_no = start
            with open(src, encoding="utf-8") as fh:
                for i, line in enumerate(fh, start=1):
                    if i <= start:
                        continue
                    line_no = i
                    if not line.strip():
                        continue
                    rep.read += 1
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        rep.malformed += 1
                        rep.quarantine_reasons["malformed_json"] += 1
                        quarantined.append({"source": src, "line": i, "reasons": ["malformed_json"],
                                            "raw": line.rstrip("\n")})
                        continue
                    errs = validate(contract, rec)
                    if errs:
                        rep.invalid += 1
                        for e in errs:
                            rep.quarantine_reasons[e] += 1
                        quarantined.append({"source": src, "line": i, "reasons": errs, "record": rec})
                        continue
                    for k in rec.keys() - contract.names:
                        rep.drift[k] += 1
                    rec = {k: v for k, v in rec.items() if k in contract.names}
                    ts = parse_ts(rec[contract.time_field])
                    key = str(rec[contract.key])
                    if state.seen(key) or key in batch_keys:  # replays count as duplicates, not late
                        rep.duplicates += 1
                        continue
                    if max_time is not None and ts < max_time - allowed_lateness:
                        rep.late += 1
                        rep.quarantine_reasons["late_arrival"] += 1
                        quarantined.append({"source": src, "line": i, "reasons": ["late_arrival"],
                                            "record": rec})
                        continue
                    part = "dt=" + ts.strftime("%Y-%m-%d")
                    batch_keys[key] = part
                    good[part].append(rec)
                    if max_time is None or ts > max_time:
                        max_time = ts
            if line_no == start:
                continue
            name = _batch_name(src, start + 1, line_no)
            for part, recs in good.items():
                _write_gz_atomic(lake_p / contract.name / part / name, recs)
                rep.partitions.add(part)
                rep.accepted += len(recs)
            if quarantined:
                _write_gz_atomic(lake_p / "_quarantine" / contract.name / name, quarantined)
            try:
                for key, part in batch_keys.items():
                    state.mark_seen(key, part)
                state.set_checkpoint(src, line_no)
                if max_time is not None:
                    state.set_meta("max_event_time", max_time.isoformat())
                state.commit()
            except Exception:
                state.rollback()
                raise
        rep.watermark = (max_time - allowed_lateness).isoformat() if max_time else None
    finally:
        state.close()
    return rep


def read_partition(lake: str, dataset: str, part: str) -> list[dict]:
    out: list[dict] = []
    for f in sorted((Path(lake) / dataset / part).glob("*.jsonl.gz")):
        with gzip.open(f, "rt", encoding="utf-8") as fh:
            out.extend(json.loads(l) for l in fh)
    return out


def compact(lake: str, dataset: str) -> dict[str, int]:
    """Merge each partition's batch files into one `compact.jsonl.gz`, sorted by key.

    Returns {partition: records}. Records are unchanged; only file layout changes.
    """
    result: dict[str, int] = {}
    root = Path(lake) / dataset
    for pdir in sorted(p for p in root.iterdir() if p.is_dir() and p.name.startswith("dt=")):
        parts = sorted(pdir.glob("*.jsonl.gz"))
        if not parts:
            continue
        recs = read_partition(lake, dataset, pdir.name)
        recs.sort(key=lambda r: json.dumps(r, sort_keys=True))
        _write_gz_atomic(pdir / "compact.jsonl.gz", recs)
        for f in parts:
            if f.name != "compact.jsonl.gz":  # already replaced atomically above
                f.unlink()
        result[pdir.name] = len(recs)
    return result
