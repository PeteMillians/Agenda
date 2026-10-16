from alembic import context
from sqlalchemy import create_engine

from app.db import metadata

config = context.config


def run_migrations() -> None:
    engine = create_engine(config.get_main_option("sqlalchemy.url"))
    with engine.connect() as connection:
        # batch mode works around SQLite's limited ALTER TABLE
        context.configure(connection=connection, target_metadata=metadata, render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


run_migrations()
