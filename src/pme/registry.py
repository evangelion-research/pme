from __future__ import annotations

import json
import os
from urllib.parse import quote

import httpx

from .errors import PmeError
from .resolve import Release
from .semver import Version

DEFAULT_REGISTRY = "https://evangelion-research.github.io/pme-index"


def shard(name: str) -> str:
    return f"{name[:2]}/{name[2:4]}"


class Registry:
    def __init__(self, base_url: str | None = None, client: httpx.Client | None = None):
        self.base_url = (base_url or os.environ.get("PME_REGISTRY") or DEFAULT_REGISTRY).rstrip("/")
        self.client = client or httpx.Client(timeout=20, follow_redirects=True)

    def versions(self, name: str) -> list[Release]:
        url = f"{self.base_url}/index/{shard(name)}/{quote(name)}.json"
        try:
            response = self.client.get(url)
            if response.status_code == 404:
                return []
            response.raise_for_status()
            result = []
            for line in response.text.splitlines():
                if not line.strip():
                    continue
                item = json.loads(line)
                result.append(Release(item["name"], Version.parse(item["version"]), item.get("deps", {}),
                                      item["checksum"], item.get("url"), item.get("emerald"), item.get("yanked", False)))
            return sorted(result, key=lambda r: r.version)
        except (httpx.HTTPError, json.JSONDecodeError, KeyError, ValueError) as exc:
            raise PmeError(f"failed to read registry entry for `{name}`: {exc}", "E_REGISTRY_FETCH", "registry", exit_code=3) from exc

    def download(self, url: str) -> bytes:
        try:
            response = self.client.get(url)
            response.raise_for_status()
            return response.content
        except httpx.HTTPError as exc:
            raise PmeError(f"failed to download package: {exc}", "E_REGISTRY_DOWNLOAD", "registry", exit_code=3) from exc
