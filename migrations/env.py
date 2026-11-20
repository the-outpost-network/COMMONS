from __future__ import annotations

from logging.config import fileConfig

from alembic import context

from app.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    raise RuntimeError(
        "Offline SQL generation is unsupported: migration 0001 must inspect the live "
        "PostgreSQL schema to safely adopt existing identity tables."
    )


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()
        return

    from app.config import load_settings, read_secret
    from app.db import make_engine

    settings = load_settings()
    engine = make_engine(settings.database_url, lambda: read_secret("database_password"))
    try:
        with engine.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                compare_type=True,
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
