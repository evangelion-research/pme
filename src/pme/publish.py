from __future__ import annotations

import gzip
import hashlib
import io
from pathlib import Path
import subprocess
import tarfile

from .errors import ManifestError, PmeError
from .manifest import Manifest


def validate(manifest: Manifest) -> None:
    if manifest.lib is None:
        raise ManifestError("published packages require a `[lib]` target", "E_PUBLISH_LIB")
    if not isinstance(manifest.package.get("license"), str) or not manifest.package["license"].strip():
        raise ManifestError("published packages require `package.license`", "E_PUBLISH_LICENSE")
    if any(dep.path is not None for dep in (*manifest.dependencies.values(), *manifest.dev_dependencies.values())):
        raise ManifestError("published packages cannot contain path dependencies", "E_PUBLISH_PATH_DEP")
    modules: dict[str, Path] = {}
    source = manifest.root / "src"
    for path in source.rglob("*.rald"):
        name = str(path.relative_to(source)).replace("/", ".").removesuffix(".rald")
        if name in modules:
            raise ManifestError(f"ambiguous module layout: `{modules[name]}` and `{path}`", "E_IMPORT_AMBIGUOUS")
        modules[name] = path


def tracked_files(root: Path) -> list[Path]:
    try:
        result = subprocess.run(["git", "ls-files", "-z"], cwd=root, check=True, capture_output=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise PmeError("publishing requires a git worktree", "E_PUBLISH_GIT") from exc
    files = [root / item.decode() for item in result.stdout.split(b"\0") if item]
    return sorted(p for p in files if p.is_file() and not str(p.relative_to(root)).startswith("target/"))


def archive(manifest: Manifest) -> tuple[bytes, str]:
    validate(manifest)
    raw = io.BytesIO()
    prefix = f"{manifest.name}-{manifest.version}"
    with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as zipped:
        with tarfile.open(fileobj=zipped, mode="w") as tar:
            for path in tracked_files(manifest.root):
                relative = path.relative_to(manifest.root)
                info = tar.gettarinfo(str(path), arcname=f"{prefix}/{relative}")
                info.mtime = info.uid = info.gid = 0
                info.uname = info.gname = ""
                info.mode = 0o755 if path.stat().st_mode & 0o111 else 0o644
                with path.open("rb") as handle:
                    tar.addfile(info, handle)
    data = raw.getvalue()
    return data, "sha256:" + hashlib.sha256(data).hexdigest()
