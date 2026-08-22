from __future__ import annotations

import json
from pathlib import Path
import re
import sys

import click
import tomlkit

from .errors import PmeError, ManifestError
from .lockfile import Lockfile, LockedPackage, dumps, load_lockfile, write_lockfile
from .manifest import NAME_RE, find_manifest, load_manifest
from .registry import Registry
from .resolve import resolve
from .semver import Constraint, Version


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
