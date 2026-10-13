"""Load and validate task definitions from a TOML file."""
from __future__ import annotations

import shlex
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Task:
    name: str
    cmd: tuple[str, ...]
    deps: tuple[str, ...] = ()
    inputs: tuple[str, ...] = ()
    outputs: tuple[str, ...] = ()
    description: str = ""


@dataclass
class Project:
    root: Path
    tasks: dict[str, Task] = field(default_factory=dict)


_KEYS = {"cmd", "deps", "inputs", "outputs", "description"}


def _str_list(name: str, key: str, value) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"task '{name}': '{key}' must be a list of strings")
    return tuple(value)


def parse(data: dict, root: Path) -> Project:
    raw_tasks = data.get("task")
    if not isinstance(raw_tasks, dict) or not raw_tasks:
        raise ConfigError("config must define at least one [task.<name>] table")
    project = Project(root=root)
    for name, body in raw_tasks.items():
        if not isinstance(body, dict):
            raise ConfigError(f"task '{name}' must be a table")
        unknown = set(body) - _KEYS
        if unknown:
            raise ConfigError(f"task '{name}': unknown keys {sorted(unknown)}")
        cmd = body.get("cmd")
        if isinstance(cmd, str):
            cmd = shlex.split(cmd)
        elif isinstance(cmd, list) and all(isinstance(c, str) for c in cmd):
            pass
        else:
            raise ConfigError(f"task '{name}': 'cmd' must be a string or list of strings")
        if not cmd:
            raise ConfigError(f"task '{name}': 'cmd' is empty")
        project.tasks[name] = Task(
            name=name,
            cmd=tuple(cmd),
            deps=_str_list(name, "deps", body.get("deps", [])),
            inputs=_str_list(name, "inputs", body.get("inputs", [])),
            outputs=_str_list(name, "outputs", body.get("outputs", [])),
            description=str(body.get("description", "")),
        )
    for t in project.tasks.values():
        for d in t.deps:
            if d not in project.tasks:
                raise ConfigError(f"task '{t.name}' depends on unknown task '{d}'")
    return project


def load(path: str | Path) -> Project:
    path = Path(path)
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"invalid TOML in {path}: {e}") from None
    return parse(data, path.resolve().parent)
