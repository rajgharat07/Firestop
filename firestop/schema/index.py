"""Look up Release vertices already in the graph by package@version."""

from __future__ import annotations

from firestop.hydra.client import HydraClient

ReleaseIndex = dict[str, dict[str, int]]

_QUERY = "MATCH (r:Release) RETURN r.package AS package, r.version AS version, r.id AS id"


async def release_index(client: HydraClient) -> ReleaseIndex:
    """Every release in the graph, keyed by package name and version.

    Streamed rather than paged: one NDJSON response is served from a single
    pinned snapshot, so the index cannot mix a package's releases from before and
    after a concurrent ingest batch.
    """
    index: ReleaseIndex = {}

    async for row in client.stream(_QUERY):
        package = row.get("package")
        version = row.get("version")
        release = row.get("id")
        if not package or not version or release is None:
            continue
        index.setdefault(str(package), {})[str(version)] = int(release)

    return index


def count_releases(index: ReleaseIndex) -> int:
    return sum(len(versions) for versions in index.values())
