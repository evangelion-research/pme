from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PmeError(Exception):
    message: str
    code: str = "E_PME"
    kind: str = "io"
    file: str | None = None
    notes: list[dict[str, str]] = field(default_factory=list)
    exit_code: int = 1

    def __str__(self) -> str:
        return self.message

    def as_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "kind": self.kind, "severity": "error", "code": self.code,
            "message": self.message, "notes": self.notes,
        }
        if self.file:
            value["file"] = self.file
        return value


class ManifestError(PmeError):
    def __init__(self, message: str, code: str = "E_MANIFEST", file: str = "emerald.toml"):
        super().__init__(message, code, "manifest", file)


class ResolveError(PmeError):
    def __init__(self, message: str, code: str, notes: list[dict[str, str]] | None = None):
        super().__init__(message, code, "resolve", "emerald.toml", notes or [])
