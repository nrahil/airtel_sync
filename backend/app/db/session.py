"""Engine and session factories."""

from __future__ import annotations

from collections.abc import Iterator
from functools import lru_cache
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event, inspect, make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import get_settings
from app.db.models import Base


def make_engine(url: str) -> Engine:
    parsed = make_url(url)
    if parsed.get_backend_name() != "sqlite":
        return create_engine(url, pool_pre_ping=True)

    kwargs: dict[str, Any] = {"connect_args": {"check_same_thread": False}}
    database = parsed.database
    if not database or database == ":memory:":
        kwargs["poolclass"] = StaticPool  # one shared in-memory DB across connections
    engine = create_engine(url, **kwargs)

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn: Any, _record: Any) -> None:
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys = ON")
        cur.close()

    return engine


def init_db(engine: Engine) -> None:
    """Create tables and guard triggers. Idempotent. (Alembic migrations come with Postgres.)"""
    database = engine.url.database
    if engine.url.get_backend_name() == "sqlite" and database and database != ":memory:":
        Path(database).parent.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(engine)
    if engine.url.get_backend_name() == "sqlite":
        _add_missing_nullable_columns(engine)


def _add_missing_nullable_columns(engine: Engine) -> None:
    """Dev convenience: a local SQLite file from an earlier phase gains new nullable columns.

    ``create_all`` creates missing tables but never alters existing ones. Only nullable
    columns are added (anything else needs a real migration, which arrives with Postgres).
    """
    existing = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not existing.has_table(table.name):
                continue
            have = {c["name"] for c in existing.get_columns(table.name)}
            for column in table.columns:
                if column.name in have:
                    continue
                if not column.nullable:
                    raise RuntimeError(
                        f"{table.name}.{column.name} is new and NOT NULL; recreate the dev database"
                    )
                ddl = column.type.compile(dialect=engine.dialect)
                conn.exec_driver_sql(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {ddl}')


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    return make_engine(get_settings().database_url)


@lru_cache(maxsize=1)
def get_sessionmaker() -> sessionmaker[Session]:
    return sessionmaker(get_engine(), expire_on_commit=False)


def get_db() -> Iterator[Session]:
    """FastAPI dependency: one session per request."""
    with get_sessionmaker()() as session:
        yield session
