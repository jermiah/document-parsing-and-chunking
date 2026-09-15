"""Database integration test; use a dedicated migrated test database."""

import os

import pytest
from conftest import FixtureParser
from sqlalchemy import select, text

from ingestion.ingestion import IngestionService
from ingestion.storage import database
from ingestion.tables import ChunkRow


@pytest.mark.postgres
def test_postgres_ingestion_and_rls(setup):
    url = os.environ.get("TEST_POSTGRES_URL")
    if not url:
        pytest.skip("TEST_POSTGRES_URL is not configured")
    settings, config, pdf = setup
    settings = settings.model_copy(update={"database_url": url})
    engine, sessions = database(url)
    try:
        result = IngestionService(settings, sessions, FixtureParser()).ingest(
            pdf, 2025, config, force=True
        )
        with sessions() as session:
            chunks = session.scalars(
                select(ChunkRow).where(ChunkRow.run_id == result["ingestion_run_id"])
            ).all()
            assert len(chunks) == result["chunk_count"] and chunks
            assert session.execute(
                text("SELECT relrowsecurity FROM pg_class WHERE oid='public.chunks'::regclass")
            ).scalar()
    finally:
        engine.dispose()
