from __future__ import annotations

from dataclasses import dataclass
from functools import total_ordering
import re

from .errors import ManifestError

_SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$")


@total_ordering
@dataclass(frozen=True)
class Version:
    major: int
    minor: int
    patch: int
    prerelease: tuple[str, ...] = ()

    @classmethod
    def parse(cls, value: str) -> "Version":
        match = _SEMVER.fullmatch(value)
        if not match:
            raise ValueError(f"invalid semantic version `{value}`")
        parts = tuple((match.group(4) or "").split(".")) if match.group(4) else ()
        if any(part.isdigit() and len(part) > 1 and part.startswith("0") for part in parts):
            raise ValueError(f"invalid semantic version `{value}`: numeric prerelease identifiers cannot have leading zeroes")
        return cls(*(int(match.group(i)) for i in range(1, 4)), parts)

    def __str__(self) -> str:
        base = f"{self.major}.{self.minor}.{self.patch}"
        return base + ("-" + ".".join(self.prerelease) if self.prerelease else "")

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        left, right = (self.major, self.minor, self.patch), (other.major, other.minor, other.patch)
        if left != right:
            return left < right
        if not self.prerelease:
            return bool(other.prerelease) and False
        if not other.prerelease:
            return True
        for a, b in zip(self.prerelease, other.prerelease):
            if a == b:
                continue
            if a.isdigit() and b.isdigit():
                return int(a) < int(b)
            if a.isdigit() != b.isdigit():
                return a.isdigit()
            return a < b
        return len(self.prerelease) < len(other.prerelease)


@dataclass(frozen=True)
class Constraint:
    raw: str
    lower: Version
    upper: Version | None = None
    exact: bool = False

    @classmethod
    def parse(cls, value: str) -> "Constraint":
        raw = value.strip()
        op = next((x for x in (">=", "==", "^", "~") if raw.startswith(x)), "")
        text = raw[len(op):].strip()
        try:
            version = Version.parse(text)
        except ValueError as exc:
            raise ManifestError(str(exc), "E_MANIFEST_VERSION") from exc
        if op == "==":
            return cls(raw, version, version, True)
        if op == "^":
            if version.major:
                upper = Version(version.major + 1, 0, 0)
            elif version.minor:
                upper = Version(0, version.minor + 1, 0)
            else:
                upper = Version(0, 0, version.patch + 1)
            return cls(raw, version, upper)
        if op == "~":
            return cls(raw, version, Version(version.major, version.minor + 1, 0))
        return cls(raw, version)

    def allows(self, version: Version | str) -> bool:
        candidate = Version.parse(version) if isinstance(version, str) else version
        if self.exact:
            return candidate == self.lower
        return candidate >= self.lower and (self.upper is None or candidate < self.upper)


def parse_version(value: str) -> Version:
    return Version.parse(value)


def parse_constraint(value: str) -> Constraint:
    return Constraint.parse(value)
