"""Download/cache the OSV npm bulk zip and iterate JSON members."""

from __future__ import annotations

import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import orjson

from firestop.config import Settings, get_settings


class BulkExport:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        cache_path: Path | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._cache_path = cache_path or Path("data/osv/npm-all.zip")
        self.downloaded_bytes = 0
        self.malformed = 0

    @property
    def path(self) -> Path:
        return self._cache_path

    @property
    def is_cached(self) -> bool:
        return self._cache_path.exists() and self._cache_path.stat().st_size > 0

    async def download(self, *, refresh: bool = False) -> Path:
        """Fetch the archive unless a copy is already on disk."""
        if self.is_cached and not refresh:
            return self._cache_path

        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        # Streamed to a temporary file so an interrupted download cannot leave a
        # truncated zip that later reads as corrupt.
        temporary = self._cache_path.with_suffix(".part")

        async with (
            httpx.AsyncClient(
                timeout=httpx.Timeout(300.0, connect=15.0), follow_redirects=True
            ) as client,
            client.stream("GET", self._settings.osv_bulk_url) as response,
        ):
            response.raise_for_status()
            with temporary.open("wb") as handle:
                async for chunk in response.aiter_bytes(1 << 16):
                    handle.write(chunk)
                    self.downloaded_bytes += len(chunk)

        temporary.replace(self._cache_path)
        return self._cache_path

    def records(self) -> Iterator[dict[str, Any]]:
        """Yield every advisory document in the archive."""
        with zipfile.ZipFile(self._cache_path) as archive:
            for entry in archive.infolist():
                if entry.is_dir() or not entry.filename.endswith(".json"):
                    continue
                try:
                    document = orjson.loads(archive.read(entry))
                except orjson.JSONDecodeError:
                    self.malformed += 1
                    continue
                if isinstance(document, dict):
                    yield document
