"""Pipeline ordering and source invariants; no live model quality claims."""

import json
import uuid

import pytest

from ingestion.pipelines.topological.pipeline import chunk
from ingestion.schemas import CanonicalDocument, Element
from ingestion.storage import write_json
from ingestion.tables import RunRow
from ingestion.tokenizer import TokenBudget


class RefinerClient:
    def __init__(self, invalid=False):
        self.invalid = invalid

    def complete(self, model, messages, max_tokens, extra):
        return json.dumps(
            {
                "boundary_ids": [999] if self.invalid else [0],
                "title": "Revenue analysis",
                "context": "",
                "evidence_ids": [],
                "request_ids": [],
            }
        ), {"usage": {"total_tokens": 10}}


@pytest.mark.parametrize("invalid", [False, True])
def test_refiner_preserves_source_and_atomic_table(setup, tmp_path, invalid):
    _, config, _ = setup
    text = "Revenue increased. " * 50
    table = "| Year | Amount |\n" + "| 2025 | 123 |\n" * 100
    doc = CanonicalDocument(
        filename="report.pdf",
        checksum="x",
        year=2025,
        page_count=1,
        parser="topology",
        parser_version="test",
        config_hash="test",
        pages=[1],
        elements=[
            Element(
                id="p", kind="paragraph", page=1, order=1, text=text, section=["Results", "Revenue"]
            ),
            Element(
                id="t", kind="table", page=1, order=2, text=table, section=["Results", "Revenue"]
            ),
        ],
    )
    chunks = chunk(
        doc,
        config,
        TokenBudget(config.tokenizer_path, config.max_tokens),
        RefinerClient(invalid),
        tmp_path,
    )
    assert "".join(c.text for c in chunks if c.element_ids == ["p"]) == text
    tables = [c for c in chunks if "t" in c.element_ids]
    assert len(tables) == 1 and tables[0].text == table
    assert any("atomic_overflow" in warning for warning in tables[0].warnings)
    assert all(c.token_count <= config.max_tokens for c in chunks if "t" not in c.element_ids)
    sir = json.loads((tmp_path / "topology_sir.json").read_text("utf-8"))
    assert next(n for n in sir["nodes"] if n["id"] == "p")["lineage"] == [
        "report.pdf",
        "Results",
        "Revenue",
    ]
    assert bool(chunks[0].topology_provenance.get("status") == "failed") == invalid


@pytest.mark.parametrize("selection", [None, ["docling", "topology"]])
def test_pipeline_order_and_resume(setup, selection):
    from test_jobs import prepare

    settings, config, pdf, engine, sessions, service, response, runner = prepare(setup)
    job_id = response["ingestion_run_id"]
    config = config.model_copy(update={"pipeline_comparison": True, "selected_pipelines": selection})
    order = config.execution_order()
    interrupted = order[1]
    with sessions.begin() as session:
        job = session.get(RunRow, job_id)
        job.config_hash = config.fingerprint()
        job.metrics = {**job.metrics, "config": config.model_dump(mode="json")}
    calls, fail = [], True

    def execute(job_id, offset, section):
        with sessions() as session:
            job = session.get(RunRow, job_id)
            name = job.metrics["current_pipeline"]
            document_id = job.document_id
        calls.append((name, offset))
        if name == interrupted and offset == 10 and fail:
            raise RuntimeError("Simulated OCR server interruption")
        bid = str(uuid.uuid4())
        key = "test-batches/" + bid
        path = settings.artifact_dir / key
        write_json(
            path / "canonical.json",
            {
                "filename": "report.pdf",
                "pages": list(range(offset + 1, min(offset + 10, 61) + 1)),
                "elements": [],
                "assets": [],
                "failures": {},
            },
        )
        (path / "chunks.jsonl").write_text("", encoding="utf-8")
        (path / "index.html").write_text("<h1>Fixture</h1>", encoding="utf-8")
        with sessions.begin() as session:
            session.add(
                RunRow(
                    id=bid,
                    document_id=document_id,
                    parser=name,
                    config_hash="fixture",
                    status="complete",
                    artifact_key=key,
                    metrics={"elapsed_seconds": 1, "chunk_count": 0},
                )
            )
        return bid

    runner.execute_batch = execute
    runner.run_job(job_id)
    assert calls == [("docling", n) for n in range(0, 61, 10)] + [
        (interrupted, 0),
        (interrupted, 10),
    ]
    with sessions() as session:
        job = session.get(RunRow, job_id)
        assert job.status == "failed"
        assert job.metrics["pipeline_progress"][interrupted]["completed_pages"] == 10
    fail = False
    runner.resume(job_id)
    runner.run_job(job_id)
    assert calls[9:] == [(interrupted, n) for n in range(10, 61, 10)] + [
        (name, n) for name in order[2:] for n in range(0, 61, 10)
    ]
    with sessions() as session:
        job = session.get(RunRow, job_id)
        assert job.status == "complete"
        assert job.metrics["completed_pages"] == 61 * len(order)
        assert set(job.metrics["workflow_metrics"]) == set(order)
        assert all(v["pages"] == 61 for v in job.metrics["workflow_metrics"].values())
        assert (settings.artifact_dir / job.metrics["inspector"]).is_file()
    engine.dispose()
