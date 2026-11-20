"""PostgreSQL engine and Alembic migrations."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

from alembic import command
from alembic.config import Config
from fastapi import Request
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker


def make_engine(database_url: str, password: Callable[[], str] | None = None) -> Engine:
    """Engine for ``database_url``.

    ``password`` is called on each new connection, so the secret file is only
    read when the database is actually used (not on import).
    """
    url = make_url(database_url)
    if url.get_backend_name() != "postgresql":
        raise ValueError("COMMONS_DATABASE_URL must be a PostgreSQL URL")
    # pool_pre_ping: survive the database restarting underneath the app.
    engine = create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=5)
    if password is not None:

        @event.listens_for(engine, "do_connect")
        def _inject_password(dialect, conn_rec, cargs, cparams):
            cparams["password"] = password()

    return engine


def upgrade_database(engine: Engine) -> None:
    """Apply committed Alembic revisions using this configured PostgreSQL engine."""
    project_root = Path(__file__).resolve().parent.parent
    config = Config(str(project_root / "alembic.ini"))
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")


def make_sessionmaker(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)


def get_db(request: Request) -> Iterator[Session]:
    """FastAPI dependency: one DB session per request."""
    with request.app.state.sessionmaker() as db:
        yield db
