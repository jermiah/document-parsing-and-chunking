"""Delete one stopped run and its batches, preserving the document and other runs."""

import shutil
import threading

from sqlalchemy import delete, select, text

from ingestion.storage import confined
from ingestion.tables import (
    AnnotationRow,
    AssetRow,
    ChunkAssetRow,
    ChunkElementRow,
    ChunkRow,
    ContextRow,
    DocumentRow,
    ElementRow,
    RunRow,
)

BUSY_STATUSES = {"uploading", "queued", "processing"}
_cleanup_lock = threading.Lock()
CLEANUP_LOCK_ID = 62810502


class RunBusyError(ValueError):
    pass


def delete_run(settings, sessions, cloud, run_id):
    # A dedicated lock makes deletion retryable after a crash without allowing two
    # requests to remove the same directory concurrently, including across API instances.
    if not _cleanup_lock.acquire(blocking=False):
        raise RunBusyError("Another cleanup is in progress. Retry shortly.")
    try:
        with sessions() as lock_session:
            postgres = lock_session.get_bind().dialect.name == "postgresql"
            if postgres and not lock_session.scalar(
                text("SELECT pg_try_advisory_xact_lock(:lock_id)"), {"lock_id": CLEANUP_LOCK_ID}
            ):
                raise RunBusyError("Another cleanup is in progress. Retry shortly.")
            return _delete_run(settings, sessions, cloud, run_id)
    finally:
        _cleanup_lock.release()


def _delete_run(settings, sessions, cloud, run_id):
    """Tombstone first so resume cannot race cleanup; failed cleanup can be retried."""
    with sessions.begin() as session:
        initial = session.get(RunRow, run_id)
        if initial is None or initial.metrics.get("parent_job"):
            raise LookupError("Run not found")
        document_id = initial.document_id
        session.execute(select(DocumentRow).where(DocumentRow.id == document_id).with_for_update())
        run = session.scalar(
            select(RunRow)
            .where(RunRow.id == run_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if run is None:
            raise LookupError("Run not found")
        if run.status in BUSY_STATUSES:
            raise RunBusyError(
                "This run is busy. Wait until it finishes or is paused before deleting it."
            )
        relatives = session.scalars(select(RunRow).where(RunRow.document_id == document_id)).all()
        owned = [r for r in relatives if r.id == run_id or r.metrics.get("parent_job") == run_id]
        # Include uncheckpointed and failed batches by ownership, never by a supplied ID list.
        paths = []
        for item in owned:
            expected = f"runs/{document_id}/{item.id}"
            if item.artifact_key != expected or item.parser not in {
                "docling",
                "topology",
                "unlimited_ocr",
            }:
                raise ValueError("Run storage ownership could not be verified")
            paths.extend(
                [
                    (settings.artifact_dir, expected),
                    (settings.report_dir, f"{item.parser}/{item.id}"),
                ]
            )
        paths.append((settings.data_dir, f"jobs/{run_id}"))
        for root, key in paths:
            target = confined(root, key)
            # Resolving a link into another run under the same root is also unsafe.
            unresolved = root.resolve() / key
            if target != unresolved or unresolved.is_symlink():
                raise ValueError("Run directory contains an unsafe link")
            if target == root.resolve():
                raise ValueError("Cannot delete a storage root")
            if target.exists():
                for child in target.rglob("*"):
                    if child.is_symlink() or not child.resolve().is_relative_to(target):
                        raise ValueError("Run directory contains an unsafe link")
        ids = [r.id for r in owned]
        artifact_keys = [r.artifact_key for r in owned]
        run.status, run.active = "deleting", False
        run.metrics = {**run.metrics, "deletion_requested": True, "stage": "deleting"}

    try:
        for key in artifact_keys:
            cloud.remove_artifact_tree(key)
        for root, key in paths:
            target = confined(root, key)
            if target.exists():
                shutil.rmtree(target)
        with sessions.begin() as session:
            session.execute(
                select(DocumentRow).where(DocumentRow.id == document_id).with_for_update()
            )
            # Respect composite foreign keys: associations first, then evidence, then runs.
            for table in (
                ChunkAssetRow,
                ChunkElementRow,
                AnnotationRow,
                ChunkRow,
                AssetRow,
                ElementRow,
                ContextRow,
            ):
                session.execute(delete(table).where(table.run_id.in_(ids)))
            session.execute(delete(RunRow).where(RunRow.id.in_(ids)))
            remaining = session.scalars(
                select(RunRow).where(RunRow.document_id == document_id, RunRow.status == "complete")
            ).all()
            parents = [r for r in remaining if not r.metrics.get("parent_job")]
            if parents and not any(r.active for r in parents):
                max(parents, key=lambda r: r.metrics.get("queued_at", 0)).active = True
        return {"status": "deleted", "ingestion_run_id": run_id, "deleted_runs": len(ids)}
    except Exception:
        with sessions.begin() as session:
            run = session.get(RunRow, run_id)
            if run:
                run.status = "delete_failed"
                run.metrics = {**run.metrics, "stage": "delete_failed"}
                run.errors = {
                    "message": "Cleanup did not finish. Retry Delete run to finish removing its files and records."
                }
        raise
