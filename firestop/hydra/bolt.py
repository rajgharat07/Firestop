"""Bolt check for doctor; HTTP is the primary transport."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from neo4j import AsyncGraphDatabase

from firestop.config import Settings, get_settings

# HydraDB authenticates Bolt with the shared token as the password. The username
# is required by the handshake and ignored by the server.
_BOLT_USER = "neo4j"


class BoltClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._driver = AsyncGraphDatabase.driver(
            self._settings.hydradb_bolt_url,
            auth=(_BOLT_USER, self._settings.hydradb_auth_token),
            max_connection_pool_size=16,
        )

    async def __aenter__(self) -> BoltClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._driver.close()

    async def verify(self) -> bool:
        try:
            await self._driver.verify_connectivity()
        except Exception:
            return False
        return True

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[Any]:
        session = self._driver.session(database=self._settings.hydradb_graph_id)
        try:
            yield session
        finally:
            await session.close()

    async def run(
        self, query: str, parameters: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        async with self._session() as session:
            result = await session.run(query, parameters or {})
            return [record.data() async for record in result]
