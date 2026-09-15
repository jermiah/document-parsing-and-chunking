import json

import pytest
import yaml
from conftest import FixtureParser
from fastapi.testclient import TestClient
from sqlalchemy import select

from ingestion.api import create_app
from ingestion.config import load_config
from ingestion.ingestion import IngestionService
from ingestion.jobs import JobRunner
from ingestion.tables import Base, RunRow


@pytest.mark.parametrize(
    "selection", ["[]", '["unknown"]', '["unlimited_ocr"]', '["docling","docling"]', "null"]
)
def test_invalid_pipeline_selection_rejected_before_queue(setup, monkeypatch, selection):
    settings, _, pdf = setup
    monkeypatch.setattr(JobRunner, "start", lambda self: None)
    monkeypatch.setattr(JobRunner, "stop", lambda self: None)
    app = create_app(settings)
    Base.metadata.create_all(app.state.engine)
    with TestClient(app) as client:
        response = client.post(
            "/v1/documents/batch",
            files=[("files", ("a.pdf", pdf.read_bytes()))],
            data={"years": "[2025]", "pipelines": selection},
        )
        assert response.status_code == 422
        assert client.get("/v1/documents").json()["total"] == 0


def test_selected_pipelines_saved_on_upload_and_reprocess(setup, monkeypatch):
    settings, _, pdf = setup
    monkeypatch.setattr(JobRunner, "start", lambda self: None)
    monkeypatch.setattr(JobRunner, "stop", lambda self: None)
    app = create_app(settings)
    Base.metadata.create_all(app.state.engine)
    with TestClient(app) as client:
        response = client.post(
            "/v1/documents/batch",
            files=[("files", ("a.pdf", pdf.read_bytes()))],
            data={"years": "[2025]", "pipelines": '["topology","docling"]'},
        )
        result = response.json()["documents"][0]["result"]
        with app.state.sessions() as session:
            run = session.get(RunRow, result["ingestion_run_id"])
            assert run.metrics["pipeline_order"] == ["docling", "topology"]
            assert run.metrics["config"]["pipeline_comparison"]
        response = client.post(
            "/v1/documents/" + result["document_id"] + "/reprocess",
            data={"pipelines": '["docling"]'},
        )
        assert response.status_code == 202
        with app.state.sessions() as session:
            run = session.get(RunRow, response.json()["ingestion_run_id"])
            assert run.metrics["pipeline_order"] == ["docling"]


def test_multiple_uploads_validate_years_and_preserve_independent_failures(setup, monkeypatch):
    settings, _, pdf = setup
    monkeypatch.setattr(JobRunner, "start", lambda self: None)
    monkeypatch.setattr(JobRunner, "stop", lambda self: None)
    app = create_app(settings)
    Base.metadata.create_all(app.state.engine)
    with TestClient(app) as client:
        invalid = client.post(
            "/v1/documents/batch",
            files=[("files", ("a.pdf", pdf.read_bytes()))],
            data={"years": "[]"},
        )
        assert invalid.status_code == 422
        assert client.get("/v1/documents").json()["total"] == 0
        response = client.post(
            "/v1/documents/batch",
            files=[
                ("files", ("arbitrary-a.pdf", pdf.read_bytes())),
                ("files", ("broken.pdf", b"not a PDF")),
                ("files", ("arbitrary-b.pdf", pdf.read_bytes())),
            ],
            data={"years": "[2020,2021,2022]"},
        )
        assert response.status_code == 202
        data = response.json()
        assert data["accepted"] == 2 and data["rejected"] == 1
        assert data["documents"][1]["status_code"] == 422
        docs = client.get("/v1/documents").json()["documents"]
        assert {d["year"] for d in docs} == {2020, 2022}
        assert all(d["runs"][0]["status"] == "queued" for d in docs)
        html = client.get("/").text
        assert "multiple required" in html and 'id="comparison"' in html


def test_job_publishes_two_workflows_and_metrics_below_chunks(setup, monkeypatch):
    settings, config, pdf = setup
    config = config.model_copy(update={"compare_workflows": True})
    settings.config_path.write_text(yaml.safe_dump(config.model_dump(mode="json")))
    monkeypatch.setattr(JobRunner, "start", lambda self: None)
    monkeypatch.setattr(JobRunner, "stop", lambda self: None)
    app = create_app(settings)
    Base.metadata.create_all(app.state.engine)

    class SavedParser(FixtureParser):
        def parse(self, *args):
            doc = super().parse(*args)
            doc.native_chunks = [{"text": doc.elements[0].text, "element_ids": ["e1"]}]
            return doc

    with TestClient(app) as client:
        response = client.post(
            "/v1/documents/batch",
            files=[("files", ("custom.pdf", pdf.read_bytes()))],
            data={"years": "[2025]"},
        ).json()
        run_id = response["documents"][0]["result"]["ingestion_run_id"]
        runner = JobRunner(settings, app.state.engine, app.state.sessions)
        published = []
        monkeypatch.setattr(runner.service.cloud, "put", lambda key, path: published.append(key))

        def execute(job_id, offset, section):
            return IngestionService(settings, app.state.sessions, SavedParser()).ingest(
                pdf,
                2025,
                load_config(settings.config_path),
                True,
                True,
                "custom.pdf",
                parent_job=job_id,
            )["ingestion_run_id"]

        runner.execute_batch = execute
        runner.run_job(run_id)
        status = client.get(f"/v1/runs/{run_id}").json()
        assert status["status"] == "complete", status
        assert "/comparison-" in status["debug_url"]
        assert published[-1].endswith("/index.html")
        html = client.get(status["debug_url"]).text
        assert html.index("<section id='inspect'>") < html.index("<section id='results'>")
        assert "Hybrid + image context" in html and "Topology-aware + image context" in html
        with app.state.sessions() as session:
            job = session.get(RunRow, run_id)
            assert set(job.metrics["workflow_metrics"]) == {"enriched_hybrid", "topology"}
            metrics = settings.artifact_dir / job.metrics["inspector"]
            assert (
                json.loads(metrics.with_name("metrics.json").read_text())["documents"][0]["year"]
                == 2025
            )
            assert session.scalar(select(RunRow).where(RunRow.id == run_id)).active
