"""Content-addressed fingerprints and the on-disk cache."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .config import Project, Task


def _file_digest(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def expand_inputs(root: Path, patterns: tuple[str, ...]) -> list[Path]:
    files: set[Path] = set()
    for pat in patterns:
        for p in root.glob(pat):
            if p.is_file():
                files.add(p)
    return sorted(files)


def fingerprint(task: Task, root: Path, dep_fingerprints: dict[str, str]) -> str:
    """Hash of the command, input file contents and upstream fingerprints.

    Input *contents* are hashed, not mtimes, so touching a file does not
    invalidate the cache, and a change upstream cascades downstream.
    """
    payload = {
        "name": task.name,
        "cmd": list(task.cmd),
        "inputs": {str(p.relative_to(root)): _file_digest(p) for p in expand_inputs(root, task.inputs)},
        "deps": {d: dep_fingerprints[d] for d in sorted(task.deps)},
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def outputs_exist(task: Task, root: Path) -> bool:
    return all(any(root.glob(o)) for o in task.outputs)


class Cache:
    def __init__(self, root: Path):
        self.path = root / ".taskgraph" / "cache.json"
        try:
            self.entries: dict[str, str] = json.loads(self.path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            self.entries = {}

    def is_fresh(self, task: Task, root: Path, fp: str) -> bool:
        return self.entries.get(task.name) == fp and outputs_exist(task, root)

    def record(self, name: str, fp: str) -> None:
        self.entries[name] = fp

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.entries, indent=2, sort_keys=True))
        tmp.replace(self.path)
