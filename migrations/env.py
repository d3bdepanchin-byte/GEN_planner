from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine

from planerka.db.models import Base
from planerka.settings import load_env

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)


def _url() -> str:
    # Приоритет: config.attributes['url'], затем -x url=..., затем DATABASE_URL.
    url = config.attributes.get("url") or context.get_x_argument(as_dictionary=True).get("url")
    return url or load_env().database_url


def run_migrations_online() -> None:
    engine = create_engine(_url())
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=Base.metadata)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    raise SystemExit("offline-режим не поддерживается")
run_migrations_online()
