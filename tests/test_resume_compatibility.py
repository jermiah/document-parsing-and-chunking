import pytest
from fastapi.testclient import TestClient

from ingestion.api import create_app
from ingestion.checkpoints import resume_block_reason
from ingestion.ingestion import IngestionService
from ingestion.jobs import JobRunner
from ingestion.tables import Base, RunRow


@pytest.mark.parametrize("saved_pages", [0, 150])
def test_incompatible_resume_rejected_before_queue(setup, monkeypatch, saved_pages):
    settings, config, pdf = setup
    monkeypatch.setattr(JobRunner, "start", lambda self: None)
    monkeypatch.setattr(JobRunner, "stop", lambda self: None)
    app = create_app(settings)
    Base.metadata.create_all(app.state.engine)
    with TestClient(app) as client:
        result = IngestionService(settings, app.state.sessions).ingest(
            pdf, 2025, config, queued=True
        )
        rid = result["ingestion_run_id"]
        with app.state.sessions.begin() as session:
            run = session.get(RunRow, rid)
            run.status = "failed"
            run.config_hash = "old-installation"
            run.metrics = {
                **run.metrics,
                "completed_pages": saved_pages,
                "batches": ["saved-batch"] if saved_pages else [],
            }
            original = dict(run.metrics)
        for _ in range(2):
            response = client.post(f"/v1/runs/{rid}/resume")
            assert response.status_code == 409
            assert "Use Reprocess" in response.json()["detail"]
        status = client.get(f"/v1/runs/{rid}").json()
        assert not status["can_resume"] and "current installation" in status["resume_block_reason"]
        with app.state.sessions() as session:
            run = session.get(RunRow, rid)
            assert run.status == "failed" and run.metrics == original


def test_resume_uses_saved_settings_but_rejects_changed_assets(setup, monkeypatch):
    settings, config, pdf = setup
    monkeypatch.setattr(JobRunner, "start", lambda self: None)
    monkeypatch.setattr(JobRunner, "stop", lambda self: None)
    app = create_app(settings)
    Base.metadata.create_all(app.state.engine)
    with TestClient(app) as client:
        result = IngestionService(settings, app.state.sessions).ingest(
            pdf, 2025, config, queued=True
        )
        rid = result["ingestion_run_id"]
        with app.state.sessions.begin() as session:
            session.get(RunRow, rid).status = "paused"
        # Editing the default profile alone must not invalidate the saved profile.
        settings.config_path.write_text("target_tokens: 200\n")
        assert client.get(f"/v1/runs/{rid}").json()["can_resume"]
        assert client.post(f"/v1/runs/{rid}/resume").status_code == 202
        with app.state.sessions.begin() as session:
            run = session.get(RunRow, rid)
            run.status = "paused"
        config.tokenizer_path.write_bytes(config.tokenizer_path.read_bytes() + b"\n")
        assert client.post(f"/v1/runs/{rid}/resume").status_code == 409
        with app.state.sessions() as session:
            assert resume_block_reason(session.get(RunRow, rid))
