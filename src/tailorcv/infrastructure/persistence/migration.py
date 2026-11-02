"""Explicit Alembic operations; application startup never calls this module."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import CheckConstraint, create_engine, inspect
from sqlalchemy.engine.reflection import Inspector

from tailorcv.infrastructure.persistence.database import (
    default_database_url,
    prepare_database_location,
    protect_database_file,
    sqlite_path,
)
from tailorcv.infrastructure.persistence.models import Base


@dataclass(frozen=True)
class MigrationStatus:
    """Expected revision and missing schema objects without mutating the database."""

    database_url: str
    current_revision: str | None
    head_revision: str
    database_exists: bool
    missing_tables: tuple[str, ...] = ()
    missing_columns: tuple[str, ...] = ()
    changed_constraints: tuple[str, ...] = ()

    @property
    def current(self) -> bool:
        """Return whether the revision and expected table set are current."""
        return (
            self.database_exists
            and self.current_revision == self.head_revision
            and not self.missing_tables
            and not self.missing_columns
            and not self.changed_constraints
        )


def _normalized_sql(value: object) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value).strip().lower()).strip("() ")


def _changed_constraints(inspector: Inspector, existing_tables: set[str]) -> tuple[str, ...]:
    changed: list[str] = []
    for table_name, table in Base.metadata.tables.items():
        if table_name not in existing_tables:
            continue
        actual = {
            item.get("name"): _normalized_sql(item.get("sqltext"))
            for item in inspector.get_check_constraints(table_name)
            if item.get("name")
        }
        for constraint in table.constraints:
            if not isinstance(constraint, CheckConstraint) or not constraint.name:
                continue
            constraint_name = str(constraint.name)
            if actual.get(constraint_name) != _normalized_sql(constraint.sqltext):
                changed.append(f"{table_name}.{constraint.name}")
    return tuple(sorted(changed))


def migrations_directory() -> Path:
    """Return the package-owned Alembic script directory."""
    return Path(__file__).with_name("migrations")


def alembic_config(database_url: str | None = None) -> Config:
    """Build Alembic configuration without a machine-specific config file."""
    config = Config()
    config.set_main_option("script_location", str(migrations_directory()))
    config.set_main_option("sqlalchemy.url", database_url or default_database_url())
    return config


def status(database_url: str | None = None) -> MigrationStatus:
    """Inspect the database revision without creating a missing SQLite file."""
    url = database_url or default_database_url()
    script = ScriptDirectory.from_config(alembic_config(url))
    head = script.get_current_head()
    if head is None:
        raise RuntimeError("TailorCV's migration package has no head revision.")
    path = sqlite_path(url)
    exists = path is None or path.is_file()
    if not exists:
        return MigrationStatus(url, None, head, False)
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            current = MigrationContext.configure(connection).get_current_revision()
            inspector = inspect(connection)
            existing_tables = set(inspector.get_table_names())
            missing_columns = tuple(
                sorted(
                    f"{table_name}.{column.name}"
                    for table_name, table in Base.metadata.tables.items()
                    if table_name in existing_tables
                    for column in table.columns
                    if column.name
                    not in {item["name"] for item in inspector.get_columns(table_name)}
                )
            )
            changed_constraints = _changed_constraints(inspector, existing_tables)
    finally:
        engine.dispose()
    missing_tables = tuple(sorted(set(Base.metadata.tables) - existing_tables))
    return MigrationStatus(
        url,
        current,
        head,
        True,
        missing_tables,
        missing_columns,
        changed_constraints,
    )


def upgrade(database_url: str | None = None, revision: str = "head") -> MigrationStatus:
    """Run an explicitly requested upgrade and return the resulting status."""
    url = database_url or default_database_url()
    prepare_database_location(url)
    command.upgrade(alembic_config(url), revision)
    protect_database_file(url)
    return status(url)


def downgrade(database_url: str | None = None, revision: str = "base") -> MigrationStatus:
    """Run an explicitly requested recovery downgrade."""
    url = database_url or default_database_url()
    command.downgrade(alembic_config(url), revision)
    return status(url)
