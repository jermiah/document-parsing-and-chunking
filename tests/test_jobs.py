"""Checkpoint durability, resume and HTTP queue contracts without model downloads."""

import pymupdf
import pytest
from fastapi.testclient import TestClient

from ingestion.api import create_app
from ingestion.ingestion import IngestionService
from ingestion.jobs import JobRunner
from ingestion.schemas import CanonicalDocument, Element
from ingestion.storage import database, file_hash
from ingestion.tables import Base, RunRow


class PagesParser:
    def parse(self, path, config, output, year):
        pages = config.pages
        return CanonicalDocument(
            filename=path.name,
            checksum=file_hash(path),
            year=year,
            page_count=61,
            parser="docling",
            parser_version="fixture",
            config_hash=config.fingerprint(),
            pages=pages,
            elements=[
                Element(
                    id=f"p{p}",
                    kind="paragraph",
                    page=p,
                    order=p,
                    text=f"Report results on page {p}.",
                    section=["Results"],
                )
                for p in pages
            ],
        )


def prepare(setup):
    settings, config, pdf = setup
    with pymupdf.open() as doc:
        for _ in range(61):
            doc.new_page()
        doc.save(pdf.with_name("long.pdf"))
    pdf = pdf.with_name("long.pdf")
    engine, sessions = database(settings.database_url)
    Base.metadata.create_all(engine)
    service = IngestionService(settings, sessions)
    response = service.ingest(pdf, 2025, config, queued=True)
    runner = JobRunner(settings, engine, sessions)
    return settings, config, pdf, engine, sessions, service, response, runner


def test_checkpoint_failure_resume_and_visible_chunks(setup):
    settings, config, pdf, engine, sessions, service, response, runner = prepare(setup)
    job_id = response["ingestion_run_id"]
    offsets = []
    fail = True

    def batch(job, offset, section):
        offsets.append(offset)
        if offset == 60 and fail:
            raise RuntimeError("Simulated worker death")
        selected = list(range(offset + 1, min(offset + 10, 61) + 1))
        result = IngestionService(settings, sessions, PagesParser()).ingest(
            pdf,
            2025,
            config.model_copy(update={"pages": selected}),
            debug=True,
            force=True,
            parent_job=job,
        )
        return result["ingestion_run_id"]

    runner.execute_batch = batch
    runner.run_job(job_id)
    with sessions() as session:
        job = session.get(RunRow, job_id)
        assert job.status == "failed"
        assert job.metrics["completed_pages"] == 50
        assert job.metrics["processed_pages"] == 50
        saved_ids = list(job.metrics["batches"])
        assert len(saved_ids) == 5
        assert job.metrics["inspector"].endswith("checkpoint-50.html")
    app = create_app(settings, PagesParser())
    with TestClient(app) as client:
        listed = client.get("/v1/documents").json()["documents"]
        assert len(listed) == 1 and len(listed[0]["runs"]) == 1
        chunks = client.get(
            f"/v1/documents/{response['document_id']}/chunks?run_id={job_id}&limit=200"
        ).json()
        assert chunks["total"] > 0
        assert max(c["end_page"] for c in chunks["chunks"]) == 50
        assert client.get(client.get(f"/v1/runs/{job_id}").json()["debug_url"]).status_code == 200
    fail = False
    assert runner.resume(job_id)["status"] == "queued"
    runner.run_job(job_id)
    assert offsets == [0, 10, 20, 30, 40, 50, 60, 50, 60]
    with sessions() as session:
        job = session.get(RunRow, job_id)
        assert job.status == "complete" and job.active
        assert job.metrics["completed_pages"] == 61
        assert job.metrics["batches"][:5] == saved_ids
        assert len(job.metrics["batches"]) == 7
    engine.dispose()


def test_upload_returns_accepted_and_deduplicates_active_jobs(setup, monkeypatch):
    settings, config, pdf = setup
    monkeypatch.setattr(JobRunner, "start", lambda self: None)
    monkeypatch.setattr(JobRunner, "stop", lambda self: None)
    app = create_app(settings)
    Base.metadata.create_all(app.state.engine)
    with TestClient(app) as client:

        def upload():
            return client.post(
                "/v1/documents/ingest",
                files={"file": ("report.pdf", pdf.read_bytes())},
                data={"year": 2025, "debug": True},
            )

        first = upload()
        assert first.status_code == 202
        result = first.json()
        assert result["status"] == "queued" and result["job"]
        assert upload().json()["ingestion_run_id"] == result["ingestion_run_id"]
        status = client.get(f"/v1/runs/{result['ingestion_run_id']}")
        assert status.json()["progress"]["completed_pages"] == 0


def test_recovery_and_config_mismatch_preserve_checkpoint(setup):
    settings, config, pdf, engine, sessions, service, response, runner = prepare(setup)
    job_id = response["ingestion_run_id"]
    with sessions.begin() as session:
        job = session.get(RunRow, job_id)
        job.status = "processing"
        job.metrics = {**job.metrics, "processed_pages": 30}
    runner.recover()
    with sessions() as session:
        job = session.get(RunRow, job_id)
        assert job.status == "paused" and job.metrics["processed_pages"] == 0
    runner.resume(job_id)
    with sessions.begin() as session:
        job = session.get(RunRow, job_id)
        job.config_hash = "changed"
    runner.run_job(job_id)
    with sessions() as session:
        job = session.get(RunRow, job_id)
        assert job.status == "failed"
        assert job.errors["code"] == "checkpoint_incompatible"
        assert job.metrics["stage"] == "reprocess_required"
        assert "Use Reprocess" in job.errors["message"]
        assert job.metrics["completed_pages"] == 0
    engine.dispose()


def test_cloud_publication_failure_does_not_advance_checkpoint(setup):
    from ingestion.jobs import checkpoint

    settings, config, pdf, engine, sessions, service, response, runner = prepare(setup)

    class BrokenCloud:
        enabled = True

        def put(self, *args):
            raise RuntimeError("Upload failed")

    with pytest.raises(RuntimeError, match="Upload failed"):
        checkpoint(sessions, BrokenCloud(), settings, response["ingestion_run_id"], [], 0, [])
    with sessions() as session:
        job = session.get(RunRow, response["ingestion_run_id"])
        assert job.metrics["completed_pages"] == 0
        assert "inspector" not in job.metrics
    engine.dispose()
