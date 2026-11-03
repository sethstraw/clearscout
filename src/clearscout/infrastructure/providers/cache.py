"""On-disk memoization of model calls, keyed so that stale answers cannot survive.

The key covers what the model is sent, so a CV edit invalidates exactly the
stages whose request carries the changed text.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any

from platformdirs import user_cache_dir

from clearscout.infrastructure.artifacts.files import FILE_MODE, private_directory


def cache_key(
    *,
    provider: str,
    model: str,
    stage: str,
    max_tokens: int,
    system: str,
    user: str,
    schema: dict[str, Any],
    extra: str = "",
    payload_digest: str = "",
) -> str:
    """Derive the cache key for one model call.

    Everything that can change the correct answer is in the key and nothing else
    is. ``schema`` must be the schema sent on the wire, not the Pydantic model's own.
    """
    payload = json.dumps(
        {
            "provider": provider,
            "model": model,
            "stage": stage,
            "max_tokens": max_tokens,
            "system": system,
            "user": user,
            "schema": schema,
            "extra": extra,
            "payload": payload_digest,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.blake2s(payload.encode("utf-8"), digest_size=16).hexdigest()


def default_cache_directory() -> Path:
    """Return the per-user directory the cache uses when a caller names none."""
    return Path(user_cache_dir("clearscout")) / "responses"


@dataclass(frozen=True)
class CachedResponse:
    """Validated response text and the build that originally produced it."""

    value: str
    revision: str = ""
    package_version: str = ""
    # SQLite CURRENT_TIMESTAMP in UTC; the row holds no provider response id.
    stored_at: str = ""


class ResponseCache:
    """A small SQLite cache that stores response text and original build provenance.

    ``enabled=False`` produces an object with the same interface that never
    stores or returns anything. Values are stored and returned as text, never
    as deserialized Python objects, since a cache file is untrusted local data.
    """

    def __init__(self, enabled: bool = True, directory: Path | None = None) -> None:
        """Open the cache, creating its directory when caching is enabled."""
        self.enabled = enabled
        self.directory = directory or default_cache_directory()
        self._cache: sqlite3.Connection | None = None
        # One connection serves every thread a client's concurrent calls run on.
        self._lock = threading.Lock()
        if enabled:
            private_directory(self.directory)
            database = self.directory / "responses.sqlite3"
            descriptor = os.open(database, os.O_RDWR | os.O_CREAT, FILE_MODE)
            os.close(descriptor)
            with suppress(OSError, NotImplementedError):  # pragma: no cover - platform dependent
                database.chmod(FILE_MODE)
            self._cache = sqlite3.connect(database, timeout=30, check_same_thread=False)
            self._cache.execute("PRAGMA busy_timeout = 30000")
            self._cache.execute("PRAGMA journal_mode = WAL")
            self._cache.execute("PRAGMA trusted_schema = OFF")
            self._cache.execute("BEGIN IMMEDIATE")
            self._cache.execute(
                """
                CREATE TABLE IF NOT EXISTS responses (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                ) STRICT
                """
            )
            columns = {row[1] for row in self._cache.execute("PRAGMA table_info(responses)")}
            for name in ("revision", "package_version"):
                if name not in columns:
                    self._cache.execute(f"ALTER TABLE responses ADD COLUMN {name} TEXT")
            self._cache.commit()

    def get(self, key: str) -> str | None:
        """Return the cached raw JSON payload for ``key``, if present."""
        entry = self.get_entry(key)
        return None if entry is None else entry.value

    def get_entry(self, key: str) -> CachedResponse | None:
        """Read response and source identity atomically; a missing identity reads blank."""
        with self._lock:
            if self._cache is None:
                return None
            row = self._cache.execute(
                "SELECT value, revision, package_version, updated_at FROM responses WHERE key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        return CachedResponse(str(row[0]), row[1] or "", row[2] or "", row[3] or "")

    def entries(self) -> Iterator[tuple[str, CachedResponse]]:
        """Yield every stored key with its response and source identity."""
        with self._lock:
            if self._cache is None:
                return
            rows = self._cache.execute(
                "SELECT key, value, revision, package_version, updated_at FROM responses"
            ).fetchall()
        for key, value, revision, package_version, stored_at in rows:
            yield (
                str(key),
                CachedResponse(str(value), revision or "", package_version or "", stored_at or ""),
            )

    def set(self, key: str, value: str, *, revision: str = "", package_version: str = "") -> None:
        """Store response text and its original build identity together."""
        with self._lock:
            if self._cache is None:
                return
            self._cache.execute(
                """
                INSERT INTO responses (key, value, revision, package_version)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    revision = excluded.revision,
                    package_version = excluded.package_version,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (key, value, revision or None, package_version or None),
            )
            self._cache.commit()

    def close(self) -> None:
        """Release the on-disk cache handles."""
        with self._lock:
            if self._cache is not None:
                self._cache.close()
                self._cache = None

    def __enter__(self) -> ResponseCache:
        """Enter a context that closes the cache on exit."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the cache when leaving the context."""
        self.close()
