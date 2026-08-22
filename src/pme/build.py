from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from .errors import BuildError, ManifestError
from .lockfile import Lockfile, dumps, load_lockfile
from .manifest import Manifest, load_manifest
from .store import package_path


@dataclass(frozen=True)
class Target:
    name: str
    entry: Path
    kind: str
    output: Path | None


@dataclass
class BuildPlan:
    root: Path
    roots: list[Path]
    targets: list[Target]
    profile: str
    lock: Lockfile


def _path_roots(manifest: Manifest, seen: set[Path] | None = None) -> list[Path]:
    seen = seen or set()
    roots: list[Path] = []
    for dep in sorted(manifest.dependencies.values(), key=lambda d: d.name):
        if dep.path is None or dep.path in seen:
            continue
        seen.add(dep.path)
        child = load_manifest(dep.path / "emerald.toml")
        roots.extend(_path_roots(child, seen))
        roots.append(child.root / "src")
    return roots


def _registry_order(lock: Lockfile) -> list[str]:
    result: list[str] = []
    visiting: set[str] = set()
    visited: set[str] = set()
    def visit(name: str) -> None:
        if name in visited:
            return
        if name in visiting:
            raise ManifestError(f"dependency cycle involving `{name}`", "E_LOCK_CYCLE")
        visiting.add(name)
        pkg = lock.packages[name]
        for child in sorted(pkg.deps):
            if child not in lock.packages:
                raise ManifestError(f"locked dependency `{child}` is missing", "E_LOCK_PACKAGE")
            visit(child)
        visiting.remove(name)
        visited.add(name)
        result.append(name)
    for name in sorted(lock.packages):
        visit(name)
    return result


def make_plan(manifest_path: Path, profile: str = "debug", selected: str | None = None) -> BuildPlan:
    manifest = load_manifest(manifest_path)
    lock_path = manifest.root / "emerald.lock"
    registry_deps = [d for d in manifest.dependencies.values() if d.path is None]
    lock = load_lockfile(lock_path) if registry_deps or lock_path.exists() else Lockfile()
    for dep in registry_deps:
        package = lock.packages.get(dep.name)
        if package is None or dep.constraint is None or not dep.constraint.allows(package.version):
            raise ManifestError("emerald.lock is out of date; run `pme install`", "E_LOCK_STALE", str(lock_path))
    roots = _path_roots(manifest)
    for name in _registry_order(lock):
        root = package_path(lock.packages[name]) / "src"
        if not root.is_dir():
            raise BuildError(f"store entry for `{name}` is missing; run `pme install`", "E_STORE_MISSING")
        roots.append(root)
    out = manifest.root / "target" / profile
    targets = [Target(x.name, manifest.root / x.entry, "bin", out / x.name) for x in manifest.bins]
    if manifest.lib is not None:
        lib_name = f"{manifest.name}-lib" if any(x.name == manifest.name for x in targets) else manifest.name
        targets.append(Target(lib_name, manifest.root / manifest.lib, "lib", None))
    if selected:
        targets = [x for x in targets if x.name == selected]
        if not targets:
            raise BuildError(f"unknown build target `{selected}`", "E_BUILD_TARGET")
    return BuildPlan(manifest.root, roots, targets, profile, lock)


def _compiler() -> Path:
    found = shutil.which(os.environ.get("PME_EMERALDC", "emeraldc"))
    if not found:
        raise BuildError("could not find `emeraldc` on PATH", "E_RESOLVE_COMPILER")
    return Path(found).resolve()


def _fingerprint(plan: BuildPlan, target: Target, mode: str, keep_c: bool, compiler: Path) -> str:
    digest = hashlib.sha256()
    stat = compiler.stat()
    digest.update(f"{compiler}\0{stat.st_size}\0{stat.st_mtime_ns}\0{mode}\0{keep_c}\0{plan.profile}".encode())
    digest.update((plan.root / "emerald.toml").read_bytes())
    digest.update(dumps(plan.lock).encode())
    source_roots = [plan.root / "src", *plan.roots]
    for root in source_roots:
        if root.is_dir():
            for path in sorted(root.rglob("*.rald")):
                digest.update(str(path.relative_to(root)).encode() + b"\0" + path.read_bytes())
    digest.update(str(target.entry).encode())
    return digest.hexdigest()


def write_plan(plan: BuildPlan) -> None:
    path = plan.root / "target" / ".pme" / "build-plan.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"profile": plan.profile, "roots": [str(x) for x in plan.roots],
               "targets": [{**asdict(x), "entry": str(x.entry), "output": str(x.output) if x.output else None}
                           for x in plan.targets]}
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def compile_plan(plan: BuildPlan, mode: str = "build", keep_c: bool = False,
                 json_output: bool = False, proof: bool = False) -> list[Path]:
    compiler = _compiler()
    artifacts: list[Path] = []
    for target in plan.targets:
        if not target.entry.is_file():
            raise BuildError(f"target entry does not exist: {target.entry}", "E_BUILD_ENTRY", str(target.entry))
        effective_mode = "check" if target.kind == "lib" and mode == "build" else mode
        fp = _fingerprint(plan, target, effective_mode, keep_c, compiler)
        fp_path = plan.root / "target" / ".pme" / "fingerprints" / f"{target.name}-{effective_mode}.sha256"
        artifact = target.output if effective_mode == "build" else None
        if fp_path.is_file() and fp_path.read_text().strip() == fp and (artifact is None or artifact.exists()):
            if artifact:
                artifacts.append(artifact)
            continue
        args = [str(compiler)]
        for root in plan.roots:
            args.extend(["-I", str(root)])
        if effective_mode == "check":
            args.append("--check")
        elif effective_mode == "emit-c":
            args.append("--emit-c")
        elif artifact:
            artifact.parent.mkdir(parents=True, exist_ok=True)
            temporary_dir = plan.root / "target" / ".tmp"
            temporary_dir.mkdir(parents=True, exist_ok=True)
            temporary_artifact = temporary_dir / f"{target.name}-{os.getpid()}"
            args.extend(["-o", str(temporary_artifact)])
        if keep_c:
            args.append("--keep-c")
        if proof:
            args.append("--proof")
        if json_output:
            args.append("--json")
        args.append(str(target.entry))
        result = subprocess.run(args, cwd=plan.root, check=False)
        if result.returncode:
            raise BuildError(f"emeraldc failed for target `{target.name}`", "E_BUILD_COMPILER")
        if artifact:
            if not temporary_artifact.exists():
                raise BuildError(f"emeraldc did not produce `{artifact.name}`", "E_BUILD_OUTPUT")
            os.replace(temporary_artifact, artifact)
        fp_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(dir=fp_path.parent, prefix=".fingerprint-", text=True)
        with os.fdopen(fd, "w") as handle:
            handle.write(fp + "\n")
        os.replace(temp, fp_path)
        if artifact:
            artifacts.append(artifact)
    write_plan(plan)
    return artifacts
