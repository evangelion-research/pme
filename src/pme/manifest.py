from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
import tomllib
from typing import Any

from .errors import ManifestError
from .semver import Constraint, Version

NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")


@dataclass(frozen=True)
class Dependency:
    name: str
    constraint: Constraint | None = None
    path: Path | None = None


@dataclass(frozen=True)
class BinTarget:
    name: str
    entry: Path


@dataclass
class Manifest:
    path: Path
    name: str
    version: Version
    package: dict[str, Any]
    dependencies: dict[str, Dependency] = field(default_factory=dict)
    dev_dependencies: dict[str, Dependency] = field(default_factory=dict)
    bins: list[BinTarget] = field(default_factory=list)
    lib: Path | None = None

    @property
    def root(self) -> Path:
        return self.path.parent


def _dependencies(data: object, root: Path, section: str) -> dict[str, Dependency]:
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ManifestError(f"`[{section}]` must be a table")
    result = {}
    for name, value in data.items():
        if not NAME_RE.fullmatch(name):
            raise ManifestError(f"invalid dependency name `{name}`", "E_MANIFEST_NAME")
        if isinstance(value, str):
            result[name] = Dependency(name, Constraint.parse(value))
        elif isinstance(value, dict) and set(value) == {"path"} and isinstance(value["path"], str):
            result[name] = Dependency(name, path=(root / value["path"]).resolve())
        else:
            raise ManifestError(f"dependency `{name}` must be a version string or {{ path = \"...\" }}")
    return result


def load_manifest(path: str | Path = "emerald.toml") -> Manifest:
    path = Path(path).resolve()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ManifestError(f"manifest not found: {path}", "E_MANIFEST_NOT_FOUND", str(path)) from exc
    except tomllib.TOMLDecodeError as exc:
        raise ManifestError(f"invalid TOML: {exc}", "E_MANIFEST_TOML", str(path)) from exc
    package = data.get("package")
    if not isinstance(package, dict):
        raise ManifestError("missing `[package]` table", file=str(path))
    name, version = package.get("name"), package.get("version")
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise ManifestError("package name must match [a-z][a-z0-9_]{1,63}", "E_MANIFEST_NAME", str(path))
    if not isinstance(version, str):
        raise ManifestError("package version must be a string", "E_MANIFEST_VERSION", str(path))
    try:
        parsed_version = Version.parse(version)
    except ValueError as exc:
        raise ManifestError(str(exc), "E_MANIFEST_VERSION", str(path)) from exc
    raw_bins, raw_lib = data.get("bin", []), data.get("lib")
    if not raw_bins and raw_lib is None:
        raise ManifestError("at least one of `[lib]` or `[[bin]]` is required", file=str(path))
    if not isinstance(raw_bins, list) or any(not isinstance(x, dict) for x in raw_bins):
        raise ManifestError("`[[bin]]` targets must be tables", file=str(path))
    bins = []
    for item in raw_bins:
        entry = item.get("entry")
        if not isinstance(entry, str):
            raise ManifestError("each `[[bin]]` requires an `entry`", file=str(path))
        target_name = item.get("name", Path(entry).stem)
        if not isinstance(target_name, str) or not NAME_RE.fullmatch(target_name):
            raise ManifestError(f"invalid binary name `{target_name}`", "E_MANIFEST_NAME", str(path))
        bins.append(BinTarget(target_name, Path(entry)))
    if len({target.name for target in bins}) != len(bins):
        raise ManifestError("binary target names must be unique", "E_MANIFEST_NAME", str(path))
    lib = None
    if raw_lib is not None:
        if not isinstance(raw_lib, dict) or not isinstance(raw_lib.get("root"), str):
            raise ManifestError("`[lib]` requires a string `root`", file=str(path))
        lib = Path(raw_lib["root"])
    return Manifest(path, name, parsed_version, package,
                    _dependencies(data.get("dependencies"), path.parent, "dependencies"),
                    _dependencies(data.get("dev-dependencies"), path.parent, "dev-dependencies"), bins, lib)


def find_manifest(start: str | Path = ".") -> Path:
    current = Path(start).resolve()
    if current.is_file():
        current = current.parent
    for directory in (current, *current.parents):
        candidate = directory / "emerald.toml"
        if candidate.is_file():
            return candidate
    raise ManifestError("could not find `emerald.toml` in this directory or its parents", "E_MANIFEST_NOT_FOUND")
