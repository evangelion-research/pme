from pathlib import Path

import pytest

from pme.build import make_plan
from pme.errors import ManifestError


def write_package(root: Path, name: str, dependency: str = "") -> None:
    (root / "src").mkdir(parents=True)
    (root / "src" / "main.rald").write_text("", encoding="utf-8")
    (root / "emerald.toml").write_text(
        f'[package]\nname = "{name}"\nversion = "1.0.0"\n\n[dependencies]\n{dependency}\n'
        f'[[bin]]\nname = "{name}"\nentry = "src/main.rald"\n', encoding="utf-8")


def test_path_dependency_roots_are_dependencies_first(tmp_path):
    leaf, middle, app = tmp_path / "leaf", tmp_path / "middle", tmp_path / "app"
    write_package(leaf, "leaf")
    write_package(middle, "middle", 'leaf = { path = "../leaf" }')
    write_package(app, "app", 'middle = { path = "../middle" }')
    plan = make_plan(app / "emerald.toml")
    assert plan.roots == [leaf / "src", middle / "src"]


def test_stale_lock_is_rejected(tmp_path):
    write_package(tmp_path, "app", 'other = "1.0.0"')
    (tmp_path / "emerald.lock").write_text("version = 1\n", encoding="utf-8")
    with pytest.raises(ManifestError, match="out of date"):
        make_plan(tmp_path / "emerald.toml")
