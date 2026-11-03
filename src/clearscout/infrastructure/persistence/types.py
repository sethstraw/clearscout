"""Explicit portable scalar representations for durable business data."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import String
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator[datetime]):
    """Store aware instants as fixed-width canonical UTC ISO-8601 text.

    SQLite's deprecated default adapters are intentionally bypassed. The text
    representation sorts chronologically, retains microseconds, and round-trips
    as an aware UTC ``datetime`` on every supported Python version.
    """

    impl = String(27)
    cache_ok = True

    @staticmethod
    def serialize(value: datetime) -> str:
        """Return canonical UTC text and refuse timezone-ambiguous values."""
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("ClearScout datetimes must be timezone-aware.")
        return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")

    @staticmethod
    def parse(value: str) -> datetime:
        """Read only the fixed-width UTC representation written by this type."""
        if len(value) != 27 or not value.endswith("Z"):
            raise ValueError("Stored ClearScout datetimes must use canonical UTC text.")
        parsed = datetime.fromisoformat(value)
        if UTCDateTime.serialize(parsed) != value:
            raise ValueError("Stored ClearScout datetimes must use canonical UTC text.")
        return parsed.astimezone(UTC)

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> str | None:
        """Serialize an application value without a DB-API datetime adapter."""
        del dialect
        return None if value is None else self.serialize(value)

    def process_result_value(self, value: str | None, dialect: Dialect) -> datetime | None:
        """Restore a stored value as an aware UTC datetime."""
        del dialect
        if value is None:
            return None
        return self.parse(value)
