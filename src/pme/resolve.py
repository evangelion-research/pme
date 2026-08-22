from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Protocol

from .errors import ResolveError
from .manifest import Dependency
from .semver import Constraint, Version


@dataclass(frozen=True)
class Release:
    name: str
    version: Version
    deps: Mapping[str, str | Constraint] = field(default_factory=dict)
    checksum: str = ""
    url: str | None = None
    emerald: str | None = None
    yanked: bool = False


class Index(Protocol):
    def versions(self, name: str) -> list[Release]: ...


class MemoryIndex:
    def __init__(self, packages: Mapping[str, list[Release]]):
        self.packages = packages

    def versions(self, name: str) -> list[Release]:
        return list(self.packages.get(name, []))


@dataclass
class Resolution:
    packages: dict[str, Release]
    parents: dict[str, set[str]]

    def paths_to(self, package: str, root: str = "root") -> list[list[str]]:
        if package not in self.packages:
            return []
        paths: list[list[str]] = []
        def walk(node: str, suffix: list[str]) -> None:
            parents = self.parents.get(node) or {"root"}
            for parent in sorted(parents):
                if parent == "root":
                    paths.append([root, node, *suffix])
                elif parent not in suffix and parent != node:
                    walk(parent, [node, *suffix])
        walk(package, [])
        return paths


def resolve(dependencies: Mapping[str, Dependency | str | Constraint], index: Index) -> Resolution:
    requirements: dict[str, list[tuple[Constraint, str]]] = {}
    selected: dict[str, Release] = {}
    parents: dict[str, set[str]] = {}

    def add_requirement(name: str, value: Dependency | str | Constraint, parent: str) -> None:
        if isinstance(value, Dependency):
            if value.path is not None:
                return
            assert value.constraint is not None
            constraint = value.constraint
        else:
            constraint = Constraint.parse(value) if isinstance(value, str) else value
        requirements.setdefault(name, []).append((constraint, parent))
        parents.setdefault(name, set()).add(parent)

    for name, constraint in dependencies.items():
        add_requirement(name, constraint, "root")

    changed = True
    while changed:
        changed = False
        for name in sorted(requirements):
            constraints = requirements[name]
            releases = [r for r in index.versions(name) if not r.yanked]
            if not releases:
                raise ResolveError(f"package `{name}` was not found in the registry", "E_RESOLVE_NOT_FOUND")
            allowed = [r for r in releases if all(c.allows(r.version) for c, _ in constraints)]
            if not allowed:
                majors = {c.lower.major for c, _ in constraints}
                code = "E_RESOLVE_MAJOR_CONFLICT" if len(majors) > 1 else "E_RESOLVE_NO_VERSION"
                notes = [{"label": "required by", "value": f"{parent} requires {name} {c.raw}"} for c, parent in constraints]
                raise ResolveError(f"no published version of `{name}` satisfies all requirements", code, notes)
            # MVS chooses the smallest release satisfying the accumulated minimums.
            choice = min(allowed, key=lambda r: r.version)
            old = selected.get(name)
            if old == choice:
                continue
            selected[name] = choice
            changed = True
            # Remove requirements contributed by the previous selected release.
            if old:
                for child in old.deps:
                    requirements[child] = [(c, p) for c, p in requirements.get(child, []) if p != name]
                    parents.get(child, set()).discard(name)
            for child, constraint in choice.deps.items():
                add_requirement(child, constraint, name)
    return Resolution(selected, parents)
