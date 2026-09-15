import httpx
import pytest
from conftest import FixtureParser
from fastapi.testclient import TestClient
from sqlalchemy import select

from ingestion.api import create_app
from ingestion.cloud import CloudFiles
from ingestion.ingestion import IngestionService
from ingestion.jobs import JobRunner
from ingestion.tables import Base, ChunkRow, DocumentRow, RunRow


def test_delete_entire_run_preserves_document_and_other_runs(setup):
    settings, config, pdf = setup
    app = create_app(settings, FixtureParser())
    Base.metadata.create_all(app.state.engine)
    service = IngestionService(settings, app.state.sessions, FixtureParser())
    with TestClient(app) as client:
        first = service.ingest(pdf, 2025, config, True, True)
        parent = service.ingest(pdf, 2025, config, True, True, queued=True)
        parent_id = parent["ingestion_run_id"]
        child = service.ingest(pdf, 2025, config, True, True, parent_job=parent_id)
        failed = service.ingest(pdf, 2025, config, True, True, parent_job=parent_id)
        with app.state.sessions.begin() as session:
            run = session.get(RunRow, parent_id)
            run.status = "failed"
            # Failed/uncheckpointed child must be discovered through parent_job.
            run.metrics = {**run.metrics, "batches": [child["ingestion_run_id"]]}
            session.get(RunRow, failed["ingestion_run_id"]).status = "processing"
            root = settings.artifact_dir / run.artifact_key
            root.mkdir(parents=True)
            (root / "checkpoint-150.html").write_text("checkpoint")
        jobs = settings.data_dir / "jobs" / parent_id
        jobs.mkdir(parents=True)
        (jobs / "batch-140.json").write_text("{}")
        ids = [parent_id, child["ingestion_run_id"], failed["ingestion_run_id"]]
        response = client.delete(f"/v1/runs/{parent_id}")
        assert response.status_code == 200, response.text
        assert response.json()["deleted_runs"] == 3
        assert not root.exists() and not jobs.exists()
        with app.state.sessions() as session:
            assert session.get(DocumentRow, first["document_id"])
            for rid in ids:
                assert session.get(RunRow, rid) is None
                assert not (settings.report_dir / "docling" / rid).exists()
                assert not (settings.artifact_dir / "runs" / first["document_id"] / rid).exists()
            assert session.get(RunRow, first["ingestion_run_id"]).active
            assert session.scalars(select(ChunkRow)).all()
        assert client.get(first["debug_url"]).status_code == 200
        assert client.get(child["debug_url"]).status_code == 404
        assert (
            client.get(f"/v1/documents/{first['document_id']}/original").content == pdf.read_bytes()
        )
        assert client.delete(f"/v1/runs/{parent_id}").status_code == 404


@pytest.mark.parametrize("status", ["queued", "processing", "uploading"])
def test_busy_runs_cannot_be_deleted(setup, status):
    settings, config, pdf = setup
    app = create_app(settings, FixtureParser())
    Base.metadata.create_all(app.state.engine)
    with TestClient(app) as client:
        result = IngestionService(settings, app.state.sessions, FixtureParser()).ingest(
            pdf, 2025, config, queued=True
        )
        rid = result["ingestion_run_id"]
        with app.state.sessions.begin() as session:
            session.get(RunRow, rid).status = status
        assert client.delete(f"/v1/runs/{rid}").status_code == 409
        assert client.get(f"/v1/runs/{rid}").json()["status"] == status
        assert (
            client.delete(
                f"/v1/runs/{rid}", headers={"Origin": "https://outside.example"}
            ).status_code
            == 403
        )


def test_cleanup_failure_can_retry_and_cannot_resume(setup, monkeypatch):
    settings, config, pdf = setup
    app = create_app(settings, FixtureParser())
    Base.metadata.create_all(app.state.engine)
    with TestClient(app) as client:
        result = IngestionService(settings, app.state.sessions, FixtureParser()).ingest(
            pdf, 2025, config, True
        )
        rid = result["ingestion_run_id"]

        def fail(*args):
            raise RuntimeError("Storage offline")

        monkeypatch.setattr(CloudFiles, "remove_artifact_tree", fail)
        assert client.delete(f"/v1/runs/{rid}").status_code == 503
        assert client.get(f"/v1/runs/{rid}").json()["status"] == "delete_failed"
        assert client.get(result["debug_url"]).status_code == 404
        assert (
            client.get(f"/v1/documents/{result['document_id']}/chunks?run_id={rid}").status_code
            == 409
        )
        runner = JobRunner(settings, app.state.engine, app.state.sessions)
        with pytest.raises(ValueError, match="cannot be resumed"):
            runner.resume(rid)
        monkeypatch.setattr(CloudFiles, "remove_artifact_tree", lambda *args: None)
        # Simulate process termination while deleting: persisted tombstone remains retryable.
        with app.state.sessions.begin() as session:
            session.get(RunRow, rid).status = "deleting"
        assert client.delete(f"/v1/runs/{rid}").status_code == 200


def test_cloud_cleanup_paginates_and_stays_within_run_prefix(setup, monkeypatch):
    settings, _, _ = setup
    cloud = CloudFiles(settings)
    cloud.enabled = True
    prefix = "artifacts/runs/doc/run"
    objects = {f"{prefix}/file-{i:03}.json" for i in range(205)}
    objects.update(
        {prefix + "/raw/page.json", "artifacts/runs/doc/other/index.html", "data/original.pdf"}
    )
    calls = []

    def request(method, path, json):
        calls.append((method, json))
        if method == "DELETE":
            assert all(k.startswith(prefix + "/") for k in json["prefixes"])
            objects.difference_update(json["prefixes"])
            return httpx.Response(200, json=[])
        folder = json["prefix"]
        entries = {}
        for key in objects:
            if key.startswith(folder):
                suffix = key[len(folder) :]
                name = suffix.split("/")[0]
                entries[name] = {"name": name, "id": None if "/" in suffix else "object-id"}
        rows = [entries[name] for name in sorted(entries)]
        return httpx.Response(200, json=rows[json["offset"] : json["offset"] + json["limit"]])

    monkeypatch.setattr(cloud, "request", request)
    cloud.remove_artifact_tree("runs/doc/run")
    assert objects == {"artifacts/runs/doc/other/index.html", "data/original.pdf"}
    assert any(body.get("offset") == 200 for _, body in calls)
    assert len([method for method, _ in calls if method == "DELETE"]) == 3
    cloud.remove_artifact_tree("runs/doc/run")  # Retrying already removed files is harmless.
    with pytest.raises(ValueError):
        cloud.remove_artifact_tree("runs/doc/..")


def test_reject_child_deletion_and_restore_current_result(setup):
    settings, config, pdf = setup
    app = create_app(settings, FixtureParser())
    Base.metadata.create_all(app.state.engine)
    service = IngestionService(settings, app.state.sessions, FixtureParser())
    with TestClient(app) as client:
        older = service.ingest(pdf, 2025, config, True, True)
        newer = service.ingest(pdf, 2025, config, True, True)
        child = service.ingest(pdf, 2025, config, True, True, parent_job=newer["ingestion_run_id"])
        assert client.delete("/v1/runs/" + child["ingestion_run_id"]).status_code == 404
        assert client.delete("/v1/runs/" + newer["ingestion_run_id"]).status_code == 200
        with app.state.sessions() as session:
            assert session.get(RunRow, older["ingestion_run_id"]).active
