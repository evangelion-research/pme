from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import io
import os
from pathlib import Path
import shutil
import tarfile
import tempfile

from .errors import PmeError
from .lockfile import LockedPackage
from .registry import Registry


def emerald_home() -> Path:
    return Path(os.environ.get("PME_HOME", Path.home() / ".emerald"))


def _digest(checksum: str) -> str:
    if not checksum.startswith("sha256:") or len(checksum) != 71:
        raise PmeError(f"unsupported checksum `{checksum}`", "E_STORE_CHECKSUM")
    return checksum.removeprefix("sha256:")


def package_path(package: LockedPackage) -> Path:
    digest = _digest(package.checksum)
    return emerald_home() / "store" / f"{package.name}-{package.version}-{digest[:12]}"


@contextmanager
def _store_lock():
    home = emerald_home()
    home.mkdir(parents=True, exist_ok=True)
    with (home / ".lock").open("a+b") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _safe_extract(data: bytes, destination: Path) -> None:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        for member in archive.getmembers():
            target = (destination / member.name).resolve()
            if destination.resolve() not in (target, *target.parents) or member.issym() or member.islnk():
                raise PmeError(f"unsafe path in package archive: `{member.name}`", "E_STORE_ARCHIVE")
        archive.extractall(destination, filter="data")


def materialize(package: LockedPackage, registry: Registry) -> Path:
    destination = package_path(package)
    digest = _digest(package.checksum)
    with _store_lock():
        if destination.is_dir():
            return destination
        cache = emerald_home() / "cache" / "tarballs" / f"{digest}.tar.gz"
        if cache.is_file():
            data = cache.read_bytes()
        else:
            if not package.url:
                raise PmeError(f"locked package `{package.name}` has no download URL", "E_STORE_MISSING")
            data = registry.download(package.url)
        actual = hashlib.sha256(data).hexdigest()
        if actual != digest:
            raise PmeError(f"checksum mismatch for `{package.name}`: expected {digest}, got {actual}", "E_STORE_CHECKSUM")
        cache.parent.mkdir(parents=True, exist_ok=True)
        if not cache.exists():
            cache_tmp = cache.with_suffix(".tmp")
            cache_tmp.write_bytes(data)
            os.replace(cache_tmp, cache)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=f".{package.name}-", dir=destination.parent))
        try:
            _safe_extract(data, temporary)
            # Accept archives with either package files at root or one wrapping directory.
            children = list(temporary.iterdir())
            source = children[0] if len(children) == 1 and children[0].is_dir() else temporary
            if not (source / "emerald.toml").is_file() or not (source / "src").is_dir():
                raise PmeError(f"package `{package.name}` archive lacks emerald.toml or src/", "E_STORE_ARCHIVE")
            if source != temporary:
                os.replace(source, destination)
            else:
                os.replace(temporary, destination)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
    return destination


def verify(package: LockedPackage) -> None:
    digest = _digest(package.checksum)
    cache = emerald_home() / "cache" / "tarballs" / f"{digest}.tar.gz"
    if not package_path(package).is_dir() or not cache.is_file():
        raise PmeError(f"store entry for `{package.name}` is missing", "E_STORE_MISSING")
    actual = hashlib.sha256(cache.read_bytes()).hexdigest()
    if actual != digest:
        raise PmeError(f"store entry for `{package.name}` is corrupt", "E_STORE_CHECKSUM")
