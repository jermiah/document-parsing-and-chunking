"""Sequential subprocess batches, with atomic 50-page checkpoint publication."""

import html
import json
import logging
import os
import subprocess
import sys
import threading
import uuid

from sqlalchemy import select, text, update

from ingestion.checkpoints import CheckpointCompatibilityError, resume_block_reason, saved_config
from ingestion.ingestion import IngestionService
from ingestion.storage import atomic_write
from ingestion.tables import DocumentRow, RunRow

logger = logging.getLogger(__name__)

# A page batch is a memory boundary; a checkpoint is a recovery boundary.
PAGE_BATCH_SIZE = 10
CHECKPOINT_PAGE_INTERVAL = 50
COORDINATOR_LOCK_ID = 62810501


def restore_comparison_inputs(settings, cloud, artifact_keys):
    """Download saved extraction inputs if a resumed worker has an empty local cache."""
    paths = []
    for key in artifact_keys:
        canonical = cloud.get(settings.artifact_dir, key + "/canonical.json", "artifacts")
        cloud.get(settings.artifact_dir, key + "/manifest.json", "artifacts")
        document = json.loads(canonical.read_text(encoding="utf-8"))
        for asset in document["assets"]:
            cloud.get(settings.artifact_dir, key + "/" + asset["key"], "artifacts")
        if cloud.enabled:
            cloud.get(settings.artifact_dir, key + "/raw/docling_document.json", "artifacts")
        paths.append(canonical.parent)
    return paths


def checkpoint(sessions, cloud, settings, job_id, batch_ids, processed, section):
    """Files first, then one database transaction publishes the checkpoint pointer."""
    with sessions() as session:
        job = session.get(RunRow, job_id)
        metadata = dict(job.metrics)
        all_ids = metadata["batches"] + batch_ids
        batches = [session.get(RunRow, bid) for bid in all_ids]
        if any(b.status != "complete" for b in batches):
            raise RuntimeError("Cannot checkpoint an incomplete batch")
        count = sum(b.metrics.get("chunk_count", 0) for b in batches)
        annotations = (
            "disabled"
            if not metadata["config"]["picture_annotations_enabled"]
            else "partial"
            if any(b.annotation_status != "complete" for b in batches)
            else "complete"
            if processed == len(metadata["pages"])
            else "pending"
        )
        key = job.artifact_key + f"/checkpoint-{processed}.html"
        links = "".join(
            f'<li><a href="/artifacts/{html.escape(b.artifact_key)}/index.html">'
            f"Batch {i + 1}: {b.metrics.get('chunk_count', 0)} chunks</a></li>"
            for i, b in enumerate(batches)
        )
        page = (
            '<!doctype html><html lang="en"><meta charset="utf-8"><title>Saved checkpoint</title>'
            f"<h1>{processed} pages saved</h1><p>{count} chunks. "
            "Batch inspectors retain original page numbers. Tables crossing a batch boundary "
            "are flagged for review.</p><ul>" + links + "</ul></html>"
        )
        path = settings.artifact_dir / key
        atomic_write(path, page.encode())
        cloud.put("artifacts/" + key, path)
    with sessions.begin() as session:
        job = session.get(RunRow, job_id)
        job.metrics = {
            **metadata,
            "batches": all_ids,
            "completed_pages": processed,
            "processed_pages": processed,
            "section": section,
            "chunk_count": count,
            "inspector": key,
            "cloud_html": cloud.enabled,
            "stage": "checkpoint_saved",
        }
        job.annotation_status = annotations
        if processed == len(metadata["pages"]) and not metadata["config"].get("compare_workflows"):
            session.execute(
                update(RunRow)
                .where(RunRow.document_id == job.document_id, RunRow.parser == job.parser)
                .values(active=False)
            )
            job.status, job.active = "complete", True
            job.indexing_status = (
                "complete" if all(b.indexing_status == "complete" for b in batches) else "partial"
            )
            job.metrics = {**job.metrics, "stage": "complete"}


class JobRunner:
    """One coordinator thread; each model batch executes in a disposable process."""

    def __init__(self, settings, engine, sessions):
        self.settings, self.engine, self.sessions = settings, engine, sessions
        self.service = IngestionService(settings, sessions)
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.loop, name="ingestion-jobs", daemon=True)
        self.process = None

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.process and self.process.poll() is None:
            self.process.terminate()
        self.thread.join(timeout=20)

    def resume(self, job_id):
        with self.sessions.begin() as session:
            job = session.scalar(select(RunRow).where(RunRow.id == job_id).with_for_update())
            if job is None:
                raise ValueError("Run not found")
            if job.metrics.get("deletion_requested"):
                raise ValueError("This run is being deleted and cannot be resumed")
            if job.metrics.get("job_version") != 1:
                raise ValueError("This older run has no checkpoints. Use Reprocess instead.")
            if job.status in {"failed", "paused"}:
                reason = resume_block_reason(job)
                if reason:
                    raise CheckpointCompatibilityError(reason)
                job.status, job.errors = "queued", {}
                job.metrics = {
                    **job.metrics,
                    "stage": "queued",
                    "processed_pages": job.metrics["completed_pages"],
                }
            return self.service.response(session.get(DocumentRow, job.document_id), job)

    def loop(self):
        while not self.stop_event.is_set():
            try:
                # Session advisory lock permits only one coordinator across API instances.
                with self.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as lock:
                    postgres = self.engine.dialect.name == "postgresql"
                    acquired = not postgres or lock.scalar(
                        text("SELECT pg_try_advisory_lock(:lock_id)"),
                        {"lock_id": COORDINATOR_LOCK_ID},
                    )
                    if acquired:
                        try:
                            self.recover()
                            while not self.stop_event.is_set():
                                with self.sessions() as session:
                                    jobs = session.scalars(
                                        select(RunRow).where(RunRow.status == "queued")
                                    ).all()
                                    jobs.sort(key=lambda j: j.metrics.get("queued_at", 0))
                                    job = next(
                                        (j for j in jobs if j.metrics.get("job_version") == 1), None
                                    )
                                if job is None:
                                    break
                                self.run_job(job.id)
                        finally:
                            if postgres:
                                lock.execute(
                                    text("SELECT pg_advisory_unlock(:lock_id)"),
                                    {"lock_id": COORDINATOR_LOCK_ID},
                                )
            except Exception:
                logger.exception("job_coordinator_failure")
            self.stop_event.wait(2)

    def recover(self):
        # Called only while holding the global coordinator lock: no other owner is active.
        with self.sessions.begin() as session:
            for job in session.scalars(select(RunRow).where(RunRow.status == "processing")):
                if job.metrics.get("job_version") == 1:
                    job.status = "paused"
                    job.metrics = {
                        **job.metrics,
                        "stage": "interrupted",
                        "processed_pages": job.metrics["completed_pages"],
                    }
                    job.errors = {
                        "message": "Worker interrupted. Resume from the saved checkpoint."
                    }

    def run_job(self, job_id):
        pending = []
        try:
            with self.sessions() as session:
                initial = session.get(RunRow, job_id)
                if initial.metrics["config"].get("pipeline_comparison"):
                    comparison = True
                else:
                    comparison = False
            if comparison:
                self.run_selected_pipelines(job_id)
                return
            with self.sessions.begin() as session:
                job = session.get(RunRow, job_id)
                metadata = dict(job.metrics)
                config = saved_config(job)
                job.status, job.errors = "processing", {}
                start = metadata["completed_pages"]
                section = metadata["section"]
            for offset in range(start, len(metadata["pages"]), PAGE_BATCH_SIZE):
                if self.stop_event.is_set():
                    raise RuntimeError("Worker stopped; resume from the last checkpoint")
                with self.sessions.begin() as session:
                    job = session.get(RunRow, job_id)
                    job.metrics = {
                        **job.metrics,
                        "stage": "processing_batch",
                        "batch_start_page": metadata["pages"][offset],
                    }
                batch_id = self.execute_batch(job_id, offset, section)
                with self.sessions() as session:
                    batch = session.get(RunRow, batch_id)
                    if batch.status != "complete":
                        raise RuntimeError("Batch conversion incomplete; checkpoint unchanged")
                    section = batch.metrics.get("last_section", section)
                pending.append(batch_id)
                processed = min(offset + PAGE_BATCH_SIZE, len(metadata["pages"]))
                with self.sessions.begin() as session:
                    job = session.get(RunRow, job_id)
                    job.metrics = {**job.metrics, "processed_pages": processed}
                if processed % CHECKPOINT_PAGE_INTERVAL == 0 or processed == len(metadata["pages"]):
                    checkpoint(
                        self.sessions,
                        self.service.cloud,
                        self.settings,
                        job_id,
                        pending,
                        processed,
                        section,
                    )
                    pending = []
            if config.compare_workflows:
                self.finish_comparison(job_id, config)
        except Exception as exc:
            logger.exception("job_failed %s", job_id)
            with self.sessions.begin() as session:
                job = session.get(RunRow, job_id)
                job.status = "paused" if self.stop_event.is_set() else "failed"
                job.metrics = {
                    **job.metrics,
                    "stage": job.status,
                    "processed_pages": job.metrics["completed_pages"],
                }
                job.errors = {"message": str(exc), "type": type(exc).__name__}
                if isinstance(exc, CheckpointCompatibilityError):
                    job.errors = {**job.errors, "code": "checkpoint_incompatible"}
                    job.metrics = {**job.metrics, "stage": "reprocess_required"}

    def run_selected_pipelines(self, job_id):
        """Finish every page batch of one pipeline before starting the next."""
        from ingestion.pipelines.report import render_report

        with self.sessions.begin() as session:
            job = session.get(RunRow, job_id)
            metadata = dict(job.metrics)
            config = saved_config(job)
            job.status, job.errors = "processing", {}
        order = config.execution_order()
        progress = dict(metadata.get("pipeline_progress", {}))
        for name in order:
            state = progress.get(name, {"completed_pages": 0, "batches": [], "section": []})
            for offset in range(state["completed_pages"], len(metadata["pages"]), PAGE_BATCH_SIZE):
                if self.stop_event.is_set():
                    raise RuntimeError("Worker stopped; resume from the saved pipeline batch")
                with self.sessions.begin() as session:
                    job = session.get(RunRow, job_id)
                    job.metrics = {
                        **job.metrics,
                        "current_pipeline": name,
                        "stage": name + "_batch",
                        "batch_start_page": metadata["pages"][offset],
                    }
                batch_id = self.execute_batch(job_id, offset, state["section"])
                with self.sessions() as session:
                    batch = session.get(RunRow, batch_id)
                    if batch.status != "complete":
                        raise RuntimeError(f"{name}: batch incomplete; saved results retained")
                    section = batch.metrics.get("last_section", [])
                state = {
                    "completed_pages": min(offset + PAGE_BATCH_SIZE, len(metadata["pages"])),
                    "batches": state["batches"] + [batch_id],
                    "section": section,
                }
                progress[name] = state
                with self.sessions.begin() as session:
                    job = session.get(RunRow, job_id)
                    completed = sum(s["completed_pages"] for s in progress.values())
                    job.metrics = {
                        **job.metrics,
                        "pipeline_progress": dict(progress),
                        "batches": [bid for s in progress.values() for bid in s["batches"]],
                        "completed_pages": completed,
                        "processed_pages": completed,
                        "chunk_count": job.metrics.get("chunk_count", 0)
                        + batch.metrics.get("chunk_count", 0),
                        "total_work_pages": len(metadata["pages"]) * len(order),
                    }
        records = {}
        with self.sessions() as session:
            job = session.get(RunRow, job_id)
            output = self.settings.artifact_dir / job.artifact_key / "pipeline-comparison"
            for name in order:
                records[name] = []
                for bid in progress[name]["batches"]:
                    batch = session.get(RunRow, bid)
                    # Export links point to complete batch inspectors, backed by cloud storage.
                    path = self.service.cloud.get(
                        self.settings.artifact_dir,
                        batch.artifact_key + "/canonical.json",
                        "artifacts",
                    ).parent
                    for filename in ("chunks.jsonl", "index.html"):
                        self.service.cloud.get(
                            self.settings.artifact_dir,
                            batch.artifact_key + "/" + filename,
                            "artifacts",
                        )
                    records[name].append((path, batch.metrics.get("elapsed_seconds", 0)))
        metrics = render_report(records, output)
        for filename in ("metrics.json", "index.html"):
            path = output / filename
            self.service.cloud.put(
                "artifacts/" + path.relative_to(self.settings.artifact_dir).as_posix(), path
            )
        with self.sessions.begin() as session:
            job = session.get(RunRow, job_id)
            all_batches = [session.get(RunRow, bid) for bid in job.metrics["batches"]]
            job.annotation_status = (
                "disabled"
                if not config.picture_annotations_enabled
                else "complete"
                if all(b.annotation_status == "complete" for b in all_batches)
                else "partial"
            )
            job.indexing_status = (
                "complete"
                if all(b.indexing_status == "complete" for b in all_batches)
                else "partial"
            )
            job.status, job.active = "complete", True
            job.metrics = {
                **job.metrics,
                "stage": "complete",
                "workflow_metrics": metrics,
                "inspector": (output / "index.html")
                .relative_to(self.settings.artifact_dir)
                .as_posix(),
            }

    def finish_comparison(self, job_id, config):
        """Finish both workflows before publishing a complete document result."""
        from ingestion.chunk_comparison import compare_bundles
        from ingestion.vision import annotation_client

        with self.sessions() as session:
            job = session.get(RunRow, job_id)
            batch_ids = job.metrics["batches"]
            batches = [session.get(RunRow, bid) for bid in batch_ids]
            artifact_keys = [b.artifact_key for b in batches]
            output = (
                self.settings.artifact_dir
                / job.artifact_key
                / ("comparison-" + uuid.uuid4().hex[:12])
            )

        paths = restore_comparison_inputs(self.settings, self.service.cloud, artifact_keys)

        def progress(workflow):
            if self.stop_event.is_set():
                raise RuntimeError("Comparison interrupted; resume the saved document job")
            with self.sessions.begin() as session:
                current = session.get(RunRow, job_id)
                current.metrics = {**current.metrics, "stage": workflow + "_workflow"}

        report = compare_bundles(
            paths,
            config,
            output,
            progress=progress,
            context_client=annotation_client(self.settings, config)
            if config.contextual_enrichment_enabled
            else None,
            context_cache=self.settings.data_dir / "context_cache",
        )
        files = sorted(path for path in output.rglob("*") if path.is_file())
        # Publish the entry page after every supporting comparison artifact.
        files.sort(key=lambda path: path.name == "index.html")
        for path in files:
            self.service.cloud.put(
                "artifacts/" + path.relative_to(self.settings.artifact_dir).as_posix(), path
            )
        with self.sessions.begin() as session:
            job = session.get(RunRow, job_id)
            session.execute(
                update(RunRow)
                .where(RunRow.document_id == job.document_id, RunRow.parser == job.parser)
                .values(active=False)
            )
            job.status, job.active = "complete", True
            job.indexing_status = (
                "complete" if all(b.indexing_status == "complete" for b in batches) else "partial"
            )
            job.metrics = {
                **job.metrics,
                "stage": "complete",
                "workflow_metrics": report["metrics"],
                "inspector": (output / "index.html")
                .relative_to(self.settings.artifact_dir)
                .as_posix(),
            }

    def execute_batch(self, job_id, offset, section):
        env = os.environ.copy()
        for name in type(self.settings).model_fields:
            value = getattr(self.settings, name)
            if hasattr(value, "get_secret_value"):
                value = value.get_secret_value()
            env[name.upper()] = str(value)
        env["BATCH_SECTION"] = json.dumps(section)
        env["OMP_NUM_THREADS"] = "4"
        result = self.settings.data_dir / "jobs" / job_id / f"batch-{offset}.json"
        result.parent.mkdir(parents=True, exist_ok=True)
        result.unlink(missing_ok=True)
        self.process = subprocess.Popen(
            [sys.executable, "-m", "ingestion.worker", job_id, str(offset), str(result)],
            env=env,
        )
        while self.process.poll() is None:
            if self.stop_event.wait(1):
                self.process.terminate()
                try:
                    self.process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
                raise RuntimeError("Batch interrupted")
        if self.process.returncode != 0:
            raise RuntimeError(
                f"Batch process exited {self.process.returncode}; saved checkpoint retained"
            )
        return json.loads(result.read_text())["ingestion_run_id"]
