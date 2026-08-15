"""Runtime configuration, read from the environment or a local .env file."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    hydradb_http_url: str = "http://127.0.0.1:8443"
    hydradb_bolt_url: str = "bolt://127.0.0.1:7687"
    hydradb_admin_url: str = "http://127.0.0.1:9090"
    hydradb_indexer_admin_url: str = "http://127.0.0.1:9091"
    hydradb_auth_token: str = "local-development-token-32-characters"
    hydradb_namespace: str = "firestop"
    hydradb_graph_id: str = "default"
    hydradb_cell_id: str = "cell-0"

    npm_registry_url: str = "https://registry.npmjs.org"

    # OSV public npm bulk export.
    osv_bulk_url: str = "https://osv-vulnerabilities.storage.googleapis.com/npm/all.zip"

    crawl_concurrency: int = 8
    crawl_requests_per_second: float = 15.0
    crawl_max_packages: int = 2000

    write_batch_size: int = Field(default=500, ge=1, le=5000)

    # Hydra rejects unbounded * patterns; keep ceilings explicit.
    max_path_length: int = Field(default=8, ge=1, le=32)
    max_paths_per_query: int = Field(default=2000, ge=1)


@lru_cache
def get_settings() -> Settings:
    return Settings()
