from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import click
import tomlkit

from .errors import PmeError, ManifestError
from .lockfile import Lockfile, LockedPackage, dumps, load_lockfile, write_lockfile
from .manifest import NAME_RE, find_manifest, load_manifest
from .registry import Registry
from .resolve import resolve
from .semver import Constraint, Version
from .store import emerald_home


class Context:
    def __init__(self, json_output: bool, quiet: bool):
        self.json = json_output
        self.quiet = quiet

    def emit(self, message: str, **data: object) -> None:
        if self.quiet:
            return
        click.echo(json.dumps({"status": "ok", "message": message, **data}) if self.json else message)


def _manifest_path() -> Path:
    return find_manifest()


def _resolved_lock(manifest_path: Path) -> Lockfile:
    manifest = load_manifest(manifest_path)
    resolution = resolve(manifest.dependencies, Registry())
    return Lockfile({name: LockedPackage(name, release.version, release.checksum,
                    tuple(sorted(release.deps)), release.url)
                    for name, release in resolution.packages.items()})


def _install(ctx: Context, locked: bool) -> None:
    manifest_path = _manifest_path()
    desired = _resolved_lock(manifest_path)
    lock_path = manifest_path.with_name("emerald.lock")
    if locked:
        try:
            current = load_lockfile(lock_path)
        except ManifestError as exc:
            raise ManifestError("`--locked` requires an up-to-date emerald.lock", "E_LOCK_STALE", str(lock_path)) from exc
        if dumps(current) != dumps(desired):
            raise ManifestError("emerald.lock is out of date and `--locked` forbids changes", "E_LOCK_STALE", str(lock_path))
    else:
        write_lockfile(desired, lock_path)
    registry = Registry()
    from .store import materialize
    for package in desired.packages.values():
        materialize(package, registry)
    ctx.emit(f"locked {len(desired.packages)} package(s)", packages=len(desired.packages))


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.option("json_output", "--json", is_flag=True, help="Emit newline-delimited JSON.")
@click.option("quiet", "-q", "--quiet", is_flag=True, help="Suppress normal output.")
@click.version_option(package_name="emerald-pme")
@click.pass_context
def cli(click_ctx: click.Context, json_output: bool, quiet: bool) -> None:
    """Package manager and build driver for Emerald."""
    click_ctx.obj = Context(json_output, quiet)


@cli.command("init")
@click.argument("name", required=False)
@click.pass_obj
def init_command(ctx: Context, name: str | None) -> None:
    """Create a new Emerald package in the current directory."""
    root = Path.cwd()
    name = name or root.name.lower().replace("-", "_")
    if not NAME_RE.fullmatch(name):
        raise ManifestError(f"invalid package name `{name}`", "E_MANIFEST_NAME")
    manifest = root / "emerald.toml"
    if manifest.exists():
        raise ManifestError("emerald.toml already exists", "E_INIT_EXISTS")
    src = root / "src"
    src.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        f'[package]\nname = "{name}"\nversion = "0.1.0"\n\n[dependencies]\n\n[[bin]]\nname = "{name}"\nentry = "src/main.rald"\n',
        encoding="utf-8")
    entry = src / "main.rald"
    if not entry.exists():
        entry.write_text('# Welcome to Emerald.\nprint("Hello, world!")\n', encoding="utf-8")
    ignore = root / ".gitignore"
    if not ignore.exists():
        ignore.write_text("target/\n", encoding="utf-8")
    ctx.emit(f"created package `{name}`", package=name)


def _split_spec(spec: str) -> tuple[str, str | None]:
    match = re.fullmatch(r"([a-z][a-z0-9_]{1,63})(?:@(.+))?", spec)
    if not match:
        raise ManifestError(f"invalid package specification `{spec}`", "E_MANIFEST_DEPENDENCY")
    return match.group(1), match.group(2)


@cli.command("add")
@click.argument("package")
@click.pass_obj
def add_command(ctx: Context, package: str) -> None:
    """Add PACKAGE[@VERSION] and update the lockfile."""
    name, requested = _split_spec(package)
    path = _manifest_path()
    document = tomlkit.parse(path.read_text(encoding="utf-8"))
    if requested is None:
        releases = [r for r in Registry().versions(name) if not r.yanked]
        if not releases:
            from .errors import ResolveError
            raise ResolveError(f"package `{name}` was not found in the registry", "E_RESOLVE_NOT_FOUND")
        requested = str(max(releases, key=lambda r: r.version).version)
    Constraint.parse(requested)
    if "dependencies" not in document:
        document["dependencies"] = tomlkit.table()
    document["dependencies"][name] = requested
    path.write_text(tomlkit.dumps(document), encoding="utf-8")
    _install(ctx, False)


@cli.command("remove")
@click.argument("package")
@click.pass_obj
def remove_command(ctx: Context, package: str) -> None:
    """Remove a direct dependency and update the lockfile."""
    path = _manifest_path()
    document = tomlkit.parse(path.read_text(encoding="utf-8"))
    dependencies = document.get("dependencies", {})
    if package not in dependencies:
        raise ManifestError(f"dependency `{package}` is not present", "E_MANIFEST_DEPENDENCY")
    del dependencies[package]
    path.write_text(tomlkit.dumps(document), encoding="utf-8")
    _install(ctx, False)


@cli.command("install")
@click.option("locked", "--locked", is_flag=True, help="Refuse to change emerald.lock.")
@click.pass_obj
def install_command(ctx: Context, locked: bool) -> None:
    """Resolve dependencies and write emerald.lock."""
    _install(ctx, locked)


@cli.command("tree")
@click.pass_obj
def tree_command(ctx: Context) -> None:
    """Print locked packages and their dependencies."""
    lock = load_lockfile(_manifest_path().with_name("emerald.lock"))
    if ctx.json:
        ctx.emit("dependency tree", packages=[{"name": p.name, "version": str(p.version), "deps": list(p.deps)}
                                               for p in sorted(lock.packages.values(), key=lambda p: p.name)])
        return
    for package in sorted(lock.packages.values(), key=lambda p: p.name):
        ctx.emit(f"{package.name} {package.version}" + (f" -> {', '.join(package.deps)}" if package.deps else ""))


@cli.command("why")
@click.argument("package")
@click.pass_obj
def why_command(ctx: Context, package: str) -> None:
    """Print every dependency path from this package to PACKAGE."""
    manifest = load_manifest(_manifest_path())
    resolution = resolve(manifest.dependencies, Registry())
    paths = resolution.paths_to(package, manifest.name)
    if not paths:
        raise ManifestError(f"package `{package}` is not in the dependency graph", "E_RESOLVE_NOT_FOUND")
    if ctx.json:
        ctx.emit("dependency paths", package=package, paths=paths)
    else:
        for path in paths:
            ctx.emit(" -> ".join(path))


@cli.command("update")
@click.argument("package", required=False)
@click.pass_obj
def update_command(ctx: Context, package: str | None) -> None:
    """Raise direct dependency minimums to their latest compatible versions."""
    path = _manifest_path()
    document = tomlkit.parse(path.read_text(encoding="utf-8"))
    dependencies = document.get("dependencies", {})
    names = [package] if package else sorted(dependencies)
    registry = Registry()
    for name in names:
        if name not in dependencies:
            raise ManifestError(f"dependency `{name}` is not present", "E_MANIFEST_DEPENDENCY")
        if not isinstance(dependencies[name], str):
            if package:
                raise ManifestError(f"path dependency `{name}` cannot be updated", "E_MANIFEST_DEPENDENCY")
            continue
        constraint = Constraint.parse(str(dependencies[name]))
        compatible = [r for r in registry.versions(name) if not r.yanked and
                      (r.version.major == constraint.lower.major)]
        if compatible:
            dependencies[name] = str(max(compatible, key=lambda r: r.version).version)
    path.write_text(tomlkit.dumps(document), encoding="utf-8")
    _install(ctx, False)


def _build(ctx: Context, target: str | None, release: bool, keep_c: bool, mode: str = "build") -> list[Path]:
    from .build import compile_plan, make_plan
    plan = make_plan(_manifest_path(), "release" if release else "debug", target)
    artifacts = compile_plan(plan, mode, keep_c, ctx.json)
    ctx.emit(f"built {len(plan.targets)} target(s)", targets=[x.name for x in plan.targets])
    return artifacts


@cli.command("build")
@click.argument("target", required=False)
@click.option("release", "--release", is_flag=True)
@click.option("keep_c", "--keep-c", is_flag=True)
@click.pass_obj
def build_command(ctx: Context, target: str | None, release: bool, keep_c: bool) -> None:
    """Build all targets, or one named TARGET."""
    _build(ctx, target, release, keep_c)


@cli.command("check")
@click.argument("target", required=False)
@click.option("proof", "--proof", is_flag=True, help="Enable compiler proof mode.")
@click.pass_obj
def check_command(ctx: Context, target: str | None, proof: bool) -> None:
    """Typecheck targets without emitting artifacts."""
    from .build import compile_plan, make_plan
    plan = make_plan(_manifest_path(), "debug", target)
    compile_plan(plan, "check", json_output=ctx.json, proof=proof)
    ctx.emit(f"checked {len(plan.targets)} target(s)", targets=[x.name for x in plan.targets])


@cli.command("emit-c")
@click.argument("target", required=False)
@click.pass_obj
def emit_c_command(ctx: Context, target: str | None) -> None:
    """Emit generated C for inspection."""
    _build(ctx, target, False, False, "emit-c")


@cli.command("run", context_settings={"ignore_unknown_options": True})
@click.argument("target", required=False)
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
@click.pass_obj
def run_command(ctx: Context, target: str | None, args: tuple[str, ...]) -> None:
    """Build and run a binary target."""
    manifest = load_manifest(_manifest_path())
    if target is None:
        if len(manifest.bins) != 1:
            raise ManifestError("select a binary target when the package has zero or multiple binaries", "E_BUILD_TARGET")
        target = manifest.bins[0].name
    artifacts = _build(ctx, target, False, False)
    if not artifacts:
        raise ManifestError(f"`{target}` is not an executable target", "E_BUILD_TARGET")
    result = subprocess.run([str(artifacts[0]), *args], check=False)
    if result.returncode:
        raise SystemExit(result.returncode)


@cli.command("test")
@click.pass_obj
def test_command(ctx: Context) -> None:
    """Compile and run every tests/*.rald file."""
    from .build import BuildPlan, Target, compile_plan, make_plan
    manifest = load_manifest(_manifest_path())
    tests = sorted((manifest.root / "tests").glob("*.rald"))
    if not tests:
        ctx.emit("no tests found", tests=0)
        return
    plan = make_plan(manifest.path)
    plan.targets = [Target(f"test-{p.stem}", p, "bin", manifest.root / "target" / "debug" / "tests" / p.stem)
                    for p in tests]
    artifacts = compile_plan(plan, json_output=ctx.json)
    for artifact in artifacts:
        result = subprocess.run([str(artifact)], check=False)
        if result.returncode:
            raise ManifestError(f"test `{artifact.name}` failed", "E_TEST_FAILED")
    ctx.emit(f"passed {len(tests)} test(s)", tests=len(tests))


@cli.command("clean")
@click.option("store", "--store", is_flag=True, help="Also prune unreferenced store entries.")
@click.pass_obj
def clean_command(ctx: Context, store: bool) -> None:
    """Remove build artifacts."""
    root = _manifest_path().parent
    shutil.rmtree(root / "target", ignore_errors=True)
    removed = 0
    if store:
        keep: set[Path] = set()
        lock_path = root / "emerald.lock"
        if lock_path.exists():
            from .store import package_path
            keep = {package_path(p).resolve() for p in load_lockfile(lock_path).packages.values()}
        store_root = emerald_home() / "store"
        if store_root.is_dir():
            for entry in store_root.iterdir():
                if entry.resolve() not in keep:
                    shutil.rmtree(entry) if entry.is_dir() else entry.unlink()
                    removed += 1
    ctx.emit("cleaned build artifacts", pruned=removed)


def _credentials() -> tuple[Path, str]:
    path = emerald_home() / "credentials.toml"
    try:
        data = tomlkit.parse(path.read_text(encoding="utf-8"))
        token = data.get("token")
    except Exception as exc:
        raise ManifestError("no registry credentials; run `pme login`", "E_AUTH_REQUIRED", str(path)) from exc
    if not isinstance(token, str) or not token:
        raise ManifestError("no registry credentials; run `pme login`", "E_AUTH_REQUIRED", str(path))
    return path, token


@cli.command("login")
@click.option("token", "--token", prompt=True, hide_input=True, envvar="PME_TOKEN")
@click.pass_obj
def login_command(ctx: Context, token: str) -> None:
    """Store a registry bearer token."""
    path = emerald_home() / "credentials.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(tomlkit.dumps({"token": token}))
    os.chmod(path, 0o600)
    ctx.emit("stored registry credentials")


@cli.command("publish")
@click.option("dry_run", "--dry-run", is_flag=True, help="Create and validate without uploading.")
@click.pass_obj
def publish_command(ctx: Context, dry_run: bool) -> None:
    """Create and publish a reproducible package archive."""
    from .publish import archive
    manifest = load_manifest(_manifest_path())
    registry = Registry()
    if any(r.version == manifest.version for r in registry.versions(manifest.name)):
        raise ManifestError(f"`{manifest.name}@{manifest.version}` is already published", "E_PUBLISH_EXISTS")
    data, checksum = archive(manifest)
    metadata = {"name": manifest.name, "version": str(manifest.version), "checksum": checksum,
                "deps": {name: dep.constraint.raw for name, dep in manifest.dependencies.items() if dep.constraint},
                "emerald": manifest.package.get("emerald"), "yanked": False}
    if not dry_run:
        _, token = _credentials()
        registry.publish(metadata, data, token)
    ctx.emit(("prepared" if dry_run else "published") + f" {manifest.name}@{manifest.version}",
             package=manifest.name, version=str(manifest.version), checksum=checksum, bytes=len(data))


@cli.command("yank")
@click.argument("package")
@click.argument("version")
@click.pass_obj
def yank_command(ctx: Context, package: str, version: str) -> None:
    """Mark a published version as unavailable for new resolutions."""
    try:
        parsed = Version.parse(version)
    except ValueError as exc:
        raise ManifestError(str(exc), "E_MANIFEST_VERSION") from exc
    _, token = _credentials()
    Registry().yank(package, parsed, token)
    ctx.emit(f"yanked {package}@{parsed}", package=package, version=str(parsed))


@cli.command("verify")
@click.pass_obj
def verify_command(ctx: Context) -> None:
    """Verify every package in the local store against the lockfile."""
    lock = load_lockfile(_manifest_path().with_name("emerald.lock"))
    from .store import verify
    for package in lock.packages.values():
        verify(package)
    ctx.emit(f"verified {len(lock.packages)} package(s)", packages=len(lock.packages))


def main() -> None:
    try:
        cli(standalone_mode=False)
    except PmeError as exc:
        json_output = "--json" in sys.argv
        click.echo(json.dumps(exc.as_dict()) if json_output else f"error[{exc.code}]: {exc.message}", err=True)
        raise SystemExit(exc.exit_code) from exc
    except click.ClickException as exc:
        exc.show()
        raise SystemExit(exc.exit_code) from exc


if __name__ == "__main__":
    main()
