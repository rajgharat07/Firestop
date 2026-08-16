"""Rate-limited npm registry client with on-disk reduced cache."""

from __future__ import annotations

import asyncio
import gzip
import time
from pathlib import Path
from urllib.parse import quote

import httpx
import orjson

from firestop.config import Settings, get_settings
from firestop.npm.packument import Dependency, Packument, Release, parse_packument
from firestop.schema.model import DependencyKind

_RETRY_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
_CACHE_VERSION = 1


class RateLimiter:
    """Token bucket shared by every worker."""

    def __init__(self, per_second: float) -> None:
        self._interval = 1.0 / per_second if per_second > 0 else 0.0
        self._lock = asyncio.Lock()
        self._next_slot = 0.0

    async def acquire(self) -> None:
        if self._interval <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            wait = max(0.0, self._next_slot - now)
            self._next_slot = max(now, self._next_slot) + self._interval
        if wait:
            await asyncio.sleep(wait)


class RegistryClient:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        cache_dir: Path | None = None,
        max_versions: int = 40,
    ) -> None:
        self._settings = settings or get_settings()
        self._max_versions = max_versions
        self._cache_dir = cache_dir or Path("data/registry")
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._limiter = RateLimiter(self._settings.crawl_requests_per_second)
        self._client = httpx.AsyncClient(
            base_url=self._settings.npm_registry_url,
            timeout=httpx.Timeout(60.0, connect=15.0),
            follow_redirects=True,
            headers={
                # Identifying the client is basic manners for a bulk crawl, and
                # gives the registry someone to throttle deliberately.
                "User-Agent": "firestop/0.1 (+https://github.com/hydra-db/hydradb hackathon)",
                "Accept": "application/json",
            },
            limits=httpx.Limits(
                max_connections=max(8, self._settings.crawl_concurrency * 2),
                max_keepalive_connections=self._settings.crawl_concurrency,
            ),
        )
        self.fetched = 0
        self.cache_hits = 0
        self.missing = 0
        self.failed = 0

    async def __aenter__(self) -> RegistryClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    async def packument(self, name: str, *, refresh: bool = False) -> Packument | None:
        """Fetch one package, from cache when possible. None if it does not exist."""
        if not refresh:
            cached = self._read_cache(name)
            if cached is not None:
                self.cache_hits += 1
                return cached

        document = await self._get(name)
        if document is None:
            return None

        parsed = parse_packument(document, max_versions=self._max_versions)
        if not parsed.name:
            # A document without a name is unusable, and caching it would make
            # the problem permanent.
            self.failed += 1
            return None

        self._write_cache(parsed)
        self.fetched += 1
        return parsed

    def load_cached(self, name: str) -> Packument | None:
        """Read a package from the cache without touching the network.

        The linking pass reads every discovered package twice -- once for its
        publish timeline, once for its dependency declarations -- and must not
        issue a request to do it.
        """
        return self._read_cache(name)

    async def _get(self, name: str) -> dict | None:
        # Scoped names carry a slash that has to survive as %2F rather than
        # becoming a path separator.
        path = f"/{quote(name, safe='')}"
        attempts = 4

        for attempt in range(attempts):
            await self._limiter.acquire()
            try:
                response = await self._client.get(path)
            except httpx.HTTPError:
                if attempt == attempts - 1:
                    self.failed += 1
                    return None
                await asyncio.sleep(_backoff(attempt))
                continue

            if response.status_code == 404:
                self.missing += 1
                return None

            if response.status_code in _RETRY_STATUS and attempt < attempts - 1:
                await asyncio.sleep(_retry_after(response) or _backoff(attempt))
                continue

            if response.status_code != 200:
                self.failed += 1
                return None

            try:
                return orjson.loads(response.content)
            except orjson.JSONDecodeError:
                self.failed += 1
                return None

        self.failed += 1
        return None

    def _cache_path(self, name: str) -> Path:
        return self._cache_dir / f"{quote(name, safe='')}.json.gz"

    def _read_cache(self, name: str) -> Packument | None:
        path = self._cache_path(name)
        if not path.exists():
            return None
        try:
            raw = orjson.loads(gzip.decompress(path.read_bytes()))
        except (OSError, orjson.JSONDecodeError, gzip.BadGzipFile):
            return None
        if raw.get("cache_version") != _CACHE_VERSION:
            return None
        return _deserialize(raw)

    def _write_cache(self, packument: Packument) -> None:
        path = self._cache_path(packument.name)
        payload = gzip.compress(orjson.dumps(_serialize(packument)), compresslevel=6)
        # Written via a temporary file so an interrupted crawl cannot leave a
        # half-written document that later reads as corrupt.
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(payload)
        temporary.replace(path)


def _serialize(packument: Packument) -> dict:
    return {
        "cache_version": _CACHE_VERSION,
        "name": packument.name,
        "maintainers": [list(person) for person in packument.maintainers],
        "version_times": packument.version_times,
        "releases": [
            {
                "version": release.version,
                "published_at": release.published_at,
                "integrity": release.integrity,
                "deprecated": release.deprecated,
                "publisher": release.publisher,
                "dependencies": [
                    [dep.name, dep.range, str(dep.kind)] for dep in release.dependencies
                ],
            }
            for release in packument.releases
        ],
    }


def _deserialize(raw: dict) -> Packument:
    return Packument(
        name=str(raw.get("name") or ""),
        maintainers=[(str(p[0]), str(p[1])) for p in raw.get("maintainers") or []],
        version_times={str(k): int(v) for k, v in (raw.get("version_times") or {}).items()},
        releases=[
            Release(
                version=str(entry["version"]),
                published_at=int(entry["published_at"]),
                integrity=str(entry.get("integrity") or ""),
                deprecated=bool(entry.get("deprecated")),
                publisher=str(entry.get("publisher") or ""),
                dependencies=[
                    Dependency(name=d[0], range=d[1], kind=DependencyKind(d[2]))
                    for d in entry.get("dependencies") or []
                ],
            )
            for entry in raw.get("releases") or []
        ],
    )


def _retry_after(response: httpx.Response) -> float | None:
    header = response.headers.get("retry-after")
    if not header:
        return None
    try:
        return min(float(header), 30.0)
    except ValueError:
        return None


def _backoff(attempt: int) -> float:
    return min(0.5 * (2**attempt), 8.0)
