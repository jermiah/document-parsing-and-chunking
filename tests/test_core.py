import json

import pytest
from conftest import FixtureParser
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ingestion.api import create_app
from ingestion.chunking import create_chunks
from ingestion.export import safe_table
from ingestion.ingestion import IngestionService
from ingestion.schemas import CanonicalDocument, Element
from ingestion.storage import confined, database
from ingestion.tables import Base, ChunkAssetRow, ChunkRow, RunRow
from ingestion.tokenizer import TokenBudget


def test_api_ingestion_reuse_force_and_debug(setup):
    settings, config, pdf = setup
    app = create_app(settings, FixtureParser())
    Base.metadata.create_all(app.state.engine)
    with TestClient(app) as client:

        def ingest(**data):
            return client.post(
                "/v1/documents/ingest",
                files={"file": ("report.pdf", pdf.read_bytes(), "application/pdf")},
                data={"year": "2025", "debug": "true", **data},
            )

        response = ingest()
        assert response.status_code == 200, response.text
        first = response.json()
        assert first["chunk_count"] >= 2
        assert ingest().json()["ingestion_run_id"] == first["ingestion_run_id"]
        second = ingest(force="true").json()
        assert second["ingestion_run_id"] != first["ingestion_run_id"]
        chunks = client.get(f"/v1/documents/{first['document_id']}/chunks?limit=1").json()
        assert chunks["total"] >= 2 and len(chunks["chunks"]) == 1
        html = client.get(first["debug_url"])
        assert html.status_code == 200 and "Source page 1" in html.text
        assert "sandbox" in html.headers["content-security-policy"]
        assert "/v1/retrieve" not in client.get("/openapi.json").json()["paths"]
        assert "/v1/answer" not in client.get("/openapi.json").json()["paths"]
        assert (
            client.post(
                "/v1/documents/ingest", files={"file": ("x.pdf", b"bad")}, data={"year": 2025}
            ).status_code
            == 422
        )
    with app.state.sessions() as session:
        runs = session.scalars(select(RunRow)).all()
        assert len(runs) == 2 and sum(r.active for r in runs) == 1
        assert all(r.indexing_status == "complete" for r in runs)


def test_cross_run_foreign_key_rejected(setup):
    settings, config, pdf = setup
    engine, sessions = database(settings.database_url)
    Base.metadata.create_all(engine)
    service = IngestionService(settings, sessions, FixtureParser())
    a = service.ingest(pdf, 2025, config)
    b = service.ingest(pdf, 2025, config, force=True)
    with sessions() as session:
        chunk = session.scalar(select(ChunkRow).where(ChunkRow.run_id == a["ingestion_run_id"]))
        # Deliberately use a nonexistent occurrence in the other run; FK must reject it.
        session.add(
            ChunkAssetRow(
                run_id=b["ingestion_run_id"], chunk_id=chunk.id, asset_id="foreign", position=0
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


def test_long_text_retained_and_budgeted(setup):
    settings, config, pdf = setup
    text = "Meaningful financial sentence with a 12.5% increase. " * 100
    doc = CanonicalDocument(
        filename="test.pdf",
        checksum="x",
        year=2025,
        page_count=1,
        pages=[1],
        parser="docling",
        parser_version="test",
        config_hash="x",
        elements=[Element(id="long", kind="paragraph", page=1, order=0, text=text)],
    )
    budget = TokenBudget(config.tokenizer_path, config.max_tokens)
    chunks = create_chunks(doc, config, budget)
    assert "".join(c.text for c in chunks) == text
    assert all(budget.count(c.retrieval_text) <= config.max_tokens for c in chunks)


def test_safe_html_and_storage():
    table = {
        "table_cells": [
            {
                "text": "<script>alert(1)</script>",
                "start_row_offset_idx": 0,
                "start_col_offset_idx": 0,
                "row_span": 2,
                "col_span": 3,
            }
        ]
    }
    rendered = safe_table(table)
    assert "<script>" not in rendered and 'rowspan="2"' in rendered and 'colspan="3"' in rendered
    with pytest.raises(ValueError):
        confined(__import__("pathlib").Path("artifacts"), "../../secret")


def test_failure_record_and_no_published_chunks(setup):
    settings, config, pdf = setup

    class FailedParser:
        def parse(self, *args):
            raise RuntimeError("intentional fixture failure")

    engine, sessions = database(settings.database_url)
    Base.metadata.create_all(engine)
    with pytest.raises(RuntimeError):
        IngestionService(settings, sessions, FailedParser()).ingest(pdf, 2025, config)
    with sessions() as session:
        assert session.scalar(select(RunRow)).status == "failed"
        assert session.scalar(select(ChunkRow)) is None


def test_config_fingerprint_changes(setup):
    settings, config, pdf = setup
    assert (
        config.fingerprint()
        != config.model_copy(update={"ocr_engine": "tesseract_cli"}).fingerprint()
    )
    assert (
        config.fingerprint()
        != config.model_copy(update={"picture_annotations_enabled": True}).fingerprint()
    )
    before = config.fingerprint()
    content = json.loads(config.tokenizer_path.read_text(encoding="utf-8"))
    content["version"] = "changed"
    config.tokenizer_path.write_text(json.dumps(content), encoding="utf-8")
    assert config.fingerprint() != before
