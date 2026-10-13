"""Dependency graph utilities: validation, selection and ordering."""
from __future__ import annotations

from .config import ConfigError, Project


def find_cycle(project: Project) -> list[str] | None:
    """Return one dependency cycle as [a, b, ..., a], or None."""
    WHITE, GREY, BLACK = 0, 1, 2
    color = {n: WHITE for n in project.tasks}
    stack: list[str] = []

    def visit(n: str) -> list[str] | None:
        color[n] = GREY
        stack.append(n)
        for d in project.tasks[n].deps:
            if color[d] == GREY:
                return stack[stack.index(d):] + [d]
            if color[d] == WHITE:
                found = visit(d)
                if found:
                    return found
        stack.pop()
        color[n] = BLACK
        return None

    for n in project.tasks:
        if color[n] == WHITE:
            found = visit(n)
            if found:
                return found
    return None


def validate(project: Project) -> None:
    cycle = find_cycle(project)
    if cycle:
        raise ConfigError("dependency cycle: " + " -> ".join(cycle))


def select(project: Project, targets: list[str] | None) -> set[str]:
    """Targets plus all transitive dependencies (all tasks if no targets)."""
    if not targets:
        return set(project.tasks)
    out: set[str] = set()
    todo = list(targets)
    while todo:
        n = todo.pop()
        if n not in project.tasks:
            raise ConfigError(f"unknown task '{n}'")
        if n in out:
            continue
        out.add(n)
        todo.extend(project.tasks[n].deps)
    return out


def levels(project: Project, names: set[str]) -> list[list[str]]:
    """Group tasks into waves; every task's deps are in earlier waves."""
    depth: dict[str, int] = {}

    def d(n: str) -> int:
        if n not in depth:
            deps = project.tasks[n].deps
            depth[n] = 0 if not deps else 1 + max(d(x) for x in deps)
        return depth[n]

    for n in names:
        d(n)
    out: list[list[str]] = [[] for _ in range(max(depth.values(), default=-1) + 1)]
    for n in sorted(names):
        out[depth[n]].append(n)
    return out
