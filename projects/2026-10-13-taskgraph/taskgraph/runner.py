"""Parallel, cache-aware execution of a task graph."""
from __future__ import annotations

import subprocess
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path

from .cache import Cache, fingerprint
from .config import Project
from .graph import select, validate

CACHED, RAN, FAILED, SKIPPED = "cached", "ran", "failed", "skipped"


@dataclass
class Result:
    name: str
    status: str
    seconds: float = 0.0
    output: str = ""
    reason: str = ""


def _execute(project: Project, name: str) -> tuple[int, str]:
    task = project.tasks[name]
    try:
        p = subprocess.run(task.cmd, cwd=project.root, capture_output=True, text=True)
    except OSError as e:
        return 127, f"cannot execute {task.cmd[0]!r}: {e}"
    return p.returncode, p.stdout + p.stderr


def run(project: Project, targets: list[str] | None = None, jobs: int = 4,
        force: bool = False, dry_run: bool = False) -> dict[str, Result]:
    """Run targets and their dependencies; returns a Result per selected task.

    A task runs as soon as all its deps have finished. If a dep fails, every
    task downstream of it is skipped; unrelated branches keep running.
    """
    validate(project)
    names = select(project, targets)
    cache = Cache(project.root)
    results: dict[str, Result] = {}
    fps: dict[str, str] = {}
    pending = set(names)
    running: dict = {}
    lock = threading.Lock()

    def finish_task(name: str, fp: str, t0: float, code: int, out: str) -> Result:
        if code == 0:
            with lock:
                cache.record(name, fp)
            return Result(name, RAN, time.monotonic() - t0, out)
        return Result(name, FAILED, time.monotonic() - t0, out, f"exit code {code}")

    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        while pending or running:
            for name in sorted(pending):
                deps = project.tasks[name].deps
                if any(d in pending or d in running.values() for d in deps):
                    continue
                pending.discard(name)
                bad = [d for d in deps if results[d].status in (FAILED, SKIPPED)]
                if bad:
                    results[name] = Result(name, SKIPPED, reason=f"dependency '{bad[0]}' did not succeed")
                    continue
                fps[name] = fingerprint(project.tasks[name], project.root, fps)
                if not force and cache.is_fresh(project.tasks[name], project.root, fps[name]):
                    results[name] = Result(name, CACHED)
                elif dry_run:
                    results[name] = Result(name, RAN, reason="dry run")
                else:
                    t0 = time.monotonic()
                    fut = pool.submit(lambda n=name, t=t0, f=fps[name]: finish_task(n, f, t, *_execute(project, n)))
                    running[fut] = name
            if running:
                done, _ = wait(list(running), return_when=FIRST_COMPLETED)
                for fut in done:
                    name = running.pop(fut)
                    results[name] = fut.result()
    if not dry_run:
        cache.save()
    return results
