from pme.manifest import Dependency
from pme.resolve import MemoryIndex, Release, resolve
from pme.semver import Constraint, Version


def release(name, version, deps=None):
    return Release(name, Version.parse(version), deps or {}, "sha256:x")


def test_mvs_raises_transitive_minimum():
    index = MemoryIndex({
        "a": [release("a", "1.0.0", {"c": "1.0.0"})],
        "b": [release("b", "1.0.0", {"c": "1.2.0"})],
        "c": [release("c", "1.0.0"), release("c", "1.2.0"), release("c", "1.5.0")],
    })
    result = resolve({"a": "1.0.0", "b": "1.0.0"}, index)
    assert result.packages["c"].version == Version.parse("1.2.0")


def test_paths_to_reports_root_to_dependency():
    index = MemoryIndex({
        "a": [release("a", "1.0.0", {"b": "1.0.0"})],
        "b": [release("b", "1.0.0")],
    })
    result = resolve({"a": "1.0.0"}, index)
    assert result.paths_to("b", "app") == [["app", "a", "b"]]
