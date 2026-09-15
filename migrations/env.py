"""Migrations read the same secret DATABASE_URL as the API; URLs are never logged."""

from alembic import context

from ingestion.config import Settings
from ingestion.storage import database
from ingestion.tables import Base

target_metadata = Base.metadata
settings = Settings()
if context.is_offline_mode():
    context.configure(
        url=settings.database_url, target_metadata=target_metadata, literal_binds=True
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    engine, _ = database(settings.database_url)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
