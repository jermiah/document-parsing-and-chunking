"""Short transactions reserve and publish runs; inference and export occur outside locks."""

import importlib.metadata
import platform
import re
import time
import uuid
from pathlib import Path

import pymupdf
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from ingestion.annotations import annotate
from ingestion.checkpoints import resume_block_reason
from ingestion.checks import scorecard
from ingestion.cloud import CloudFiles
from ingestion.config import ProcessingConfig, Settings
from ingestion.contextual import enrich_chunks, source_budget
from ingestion.export import export_bundle
from ingestion.images import filter_assets
from ingestion.nodes import prepare_nodes
from ingestion.ocr import recognize_figures
from ingestion.reports import write_report
from ingestion.schemas import DocumentParser
from ingestion.storage import atomic_write, file_hash
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
from ingestion.topology import enrich_retrieval
from ingestion.vision import annotation_client


class IngestionService:
    def __init__(self, settings: Settings, sessions, parser: DocumentParser | None = None):
        self.settings, self.sessions, self.parser = settings, sessions, parser
        self.cloud = CloudFiles(settings)

    def ingest(
        self,
        path: Path,
        year: int,
        config: ProcessingConfig,
        debug: bool = False,
        force: bool = False,
        filename: str | None = None,
        queued: bool = False,
        parent_job: str | None = None,
    ) -> dict:
        if config.pipeline_comparison and not queued and not parent_job:
            # The coordinator owns ordering, isolation and recovery for the selected workflows.
            queued = True
        started = time.perf_counter()
        queued_at = time.time()
        vision = (
            annotation_client(self.settings, config)
            if (
                config.picture_annotations_enabled
                or config.contextual_enrichment_enabled
                or config.parser == "topology"
            )
            else None
        )
        if not 1900 <= year <= 2100:
            raise ValueError("Year must be between 1900 and 2100")
        if path.stat().st_size > self.settings.max_upload_mb * 1024 * 1024:
            raise ValueError("PDF exceeds upload size limit")
        with path.open("rb") as stream:
            if not stream.read(5) == b"%PDF-":
                raise ValueError("Expected PDF magic bytes")
        with pymupdf.open(path) as pdf:
            if pdf.is_encrypted:
                raise ValueError("Encrypted PDFs are not supported")
            count = len(pdf)
        if count == 0 or count > self.settings.max_pages:
            raise ValueError("PDF page count exceeds configured limits")
        name = (
            re.sub(r"[^\w. -]", "_", (filename or path.name).replace("\\", "/").split("/")[-1])[
                :200
            ]
            or "document.pdf"
        )
        checksum, config_hash = file_hash(path), config.fingerprint()
        original_key = f"originals/{checksum}.pdf"
        original = self.settings.data_dir / original_key
        if not original.exists():
            atomic_write(original, path.read_bytes())
        with self.sessions() as session:
            document = session.scalar(
                select(DocumentRow).where(
                    DocumentRow.checksum == checksum, DocumentRow.year == year
                )
            )
            if document is None:
                document = DocumentRow(
                    id=str(uuid.uuid4()),
                    filename=name,
                    checksum=checksum,
                    year=year,
                    page_count=count,
                    original_key=original_key,
                )
                session.add(document)
                try:
                    session.commit()
                except IntegrityError:
                    session.rollback()
                    document = session.scalar(
                        select(DocumentRow).where(
                            DocumentRow.checksum == checksum, DocumentRow.year == year
                        )
                    )
            doc_id = document.id
            session.execute(select(DocumentRow).where(DocumentRow.id == doc_id).with_for_update())
            if queued:
                existing = session.scalars(
                    select(RunRow).where(
                        RunRow.document_id == doc_id,
                        RunRow.config_hash == config_hash,
                        RunRow.status.in_(["queued", "processing"]),
                    )
                ).all()
                for candidate in existing:
                    if candidate.metrics.get("job_version") == 1:
                        return self.response(document, candidate)
            reusable = session.scalar(
                select(RunRow).where(
                    RunRow.document_id == doc_id,
                    RunRow.config_hash == config_hash,
                    RunRow.status == "complete",
                )
            )
            if (
                reusable
                and not force
                and (
                    not debug
                    or (self.settings.artifact_dir / reusable.artifact_key / "index.html").exists()
                    or reusable.metrics.get("inspector")
                    or (self.cloud.enabled and reusable.metrics.get("cloud_html", False))
                )
            ):
                return self.response(document, reusable)
            run_id = str(uuid.uuid4())
            key = f"runs/{doc_id}/{run_id}"
            run = RunRow(
                id=run_id,
                document_id=doc_id,
                parser=config.parser,
                config_hash=config_hash,
                artifact_key=key,
                annotation_status="pending" if config.picture_annotations_enabled else "disabled",
                status="uploading" if queued else "processing",
                metrics={
                    "queued_at": queued_at,
                    **({"parent_job": parent_job} if parent_job else {}),
                },
            )
            session.add(run)
            session.commit()
        if queued:
            try:
                self.cloud.put("data/" + original_key, original)
                pages = config.pages or list(range(1, count + 1))
                if max(pages) > count:
                    raise ValueError("Requested page exceeds PDF page count")
                with self.sessions.begin() as session:
                    run = session.get(RunRow, run_id)
                    run.metrics = {
                        "job_version": 1,
                        "queued_at": time.time(),
                        "config": config.model_dump(mode="json"),
                        "pages": pages,
                        "debug": debug,
                        "batch_size": 10,
                        "checkpoint_interval": 50,
                        "completed_pages": 0,
                        "processed_pages": 0,
                        "batches": [],
                        "chunk_count": 0,
                        "section": [],
                        "stage": "queued",
                        "pipeline_order": config.execution_order()
                        if config.pipeline_comparison
                        else [config.parser],
                    }
                    run.status = "queued"
                with self.sessions() as session:
                    return self.response(
                        session.get(DocumentRow, doc_id), session.get(RunRow, run_id)
                    )
            except Exception:
                with self.sessions.begin() as session:
                    run = session.get(RunRow, run_id)
                    run.status = "failed"
                    run.errors = {"message": "Could not queue document; upload it again"}
                raise
        output = self.settings.artifact_dir / key
        output.mkdir(parents=True, exist_ok=True)
        try:
            budget = source_budget(config)
            parser = self.parser
            if parser is None:
                from ingestion.pipelines.registry import pipeline

                parser = pipeline(config.parser).parser(self.settings, config)
            canonical = parser.parse(path, config, output, year)
            canonical.filename = name
            filter_assets(canonical, output)
            if self.parser is None and config.parser in {"docling", "topology"}:
                recognize_figures(canonical, output, config)
            if config.picture_annotations_enabled:
                assert vision is not None
                annotate(
                    canonical,
                    output,
                    config,
                    vision,
                    self.settings.data_dir / "annotation_cache",
                )
            # Classification above; deterministic exclusion before any chunker
            # or chunk-context prompt receives the retrieval copy.
            projected = enrich_retrieval(canonical)
            from ingestion.pipelines.registry import pipeline

            chunks = pipeline(config.parser).chunk(projected, config, budget, vision, output)
            for chunk in chunks:
                chunk.pipeline = config.parser
            enrich_chunks(
                projected, chunks, config, vision, self.settings.data_dir / "context_cache"
            )
            scores = scorecard(canonical, chunks, config.max_tokens)
            status = "partial" if canonical.failures else "complete"
            annotation_status = (
                "disabled"
                if not config.picture_annotations_enabled
                else (
                    "complete" if len(canonical.annotations) == len(canonical.assets) else "partial"
                )
            )
            manifest = {
                "schema_version": 1,
                "document_id": doc_id,
                "run_id": run_id,
                "checksum": checksum,
                "original_key": original_key,
                "parser": canonical.parser,
                "parser_version": canonical.parser_version,
                "config": config.model_dump(mode="json"),
                "config_hash": config_hash,
                "scope": {"pages": canonical.pages, "full_report": config.pages is None},
                "status": status,
                "inference": "real" if self.parser is None or parent_job else "injected_parser",
                "failures": canonical.failures,
                "warnings": canonical.warnings,
            }
            serialization = export_bundle(canonical, chunks, output, path, manifest, debug, scores)
            if (
                not serialization["canonical_roundtrip"]
                or serialization["broken_images"]
                or scores["invalid_chunks"]
            ):
                raise ValueError("Serialization or provenance validation failed")
            indexing_status = "complete"
            try:
                prepare_nodes(chunks, doc_id, run_id, output / "llamaindex")
            except Exception as exc:
                indexing_status = "failed"
                canonical.warnings.append(f"Node indexing failed: {type(exc).__name__}")
            metrics = {
                "queued_at": queued_at,
                "parent_job": parent_job,
                "last_section": canonical.elements[-1].section if canonical.elements else [],
                "chunk_count": len(chunks),
                "pages": {
                    "attempted": len(canonical.pages),
                    "successful": len(canonical.pages) - len(canonical.failures),
                    "failed": len(canonical.failures),
                },
                "inventory": {
                    "elements": len(canonical.elements),
                    "assets": len(canonical.assets),
                    "annotations": len(canonical.annotations),
                },
                "elapsed_seconds": time.perf_counter() - started,
                "scorecard": scores,
                "serialization": serialization,
            }
            if parent_job:
                for artifact in sorted(output.rglob("*")):
                    if artifact.is_file():
                        self.cloud.put(
                            "artifacts/" + key + "/" + artifact.relative_to(output).as_posix(),
                            artifact,
                        )
            else:
                self.cloud.publish(original, original_key, output, key)
            metrics["cloud_html"] = self.cloud.enabled and (output / "index.html").is_file()
            with self.sessions.begin() as session:
                parents = {c.parent_id for c in chunks if c.parent_id}
                session.add_all(
                    [
                        ContextRow(
                            run_id=run_id,
                            id=p,
                            payload={
                                "element_ids": list(
                                    dict.fromkeys(
                                        eid
                                        for c in chunks
                                        if c.parent_id == p
                                        for eid in c.element_ids
                                    )
                                )
                            },
                        )
                        for p in parents
                    ]
                )
                session.add_all(
                    [
                        ElementRow(
                            run_id=run_id, id=e.id, page=e.page, payload=e.model_dump(mode="json")
                        )
                        for e in canonical.elements
                    ]
                )
                session.add_all(
                    [
                        AssetRow(
                            run_id=run_id, id=a.id, page=a.page, payload=a.model_dump(mode="json")
                        )
                        for a in canonical.assets
                    ]
                )
                session.flush()
                for c in chunks:
                    session.add(
                        ChunkRow(
                            run_id=run_id,
                            id=c.id,
                            document_id=doc_id,
                            ordinal=c.ordinal,
                            text=c.text,
                            retrieval_text=c.retrieval_text,
                            start_page=c.start_page,
                            end_page=c.end_page,
                            parent_id=c.parent_id,
                            payload=c.model_dump(mode="json"),
                        )
                    )
                session.flush()
                for c in chunks:
                    session.add_all(
                        [
                            ChunkElementRow(
                                run_id=run_id, chunk_id=c.id, element_id=eid, position=i
                            )
                            for i, eid in enumerate(c.element_ids)
                        ]
                    )
                    session.add_all(
                        [
                            ChunkAssetRow(run_id=run_id, chunk_id=c.id, asset_id=aid, position=i)
                            for i, aid in enumerate(c.asset_ids)
                        ]
                    )
                session.add_all(
                    [
                        AnnotationRow(
                            run_id=run_id,
                            asset_id=a.asset_id,
                            version=a.provenance["input_hash"],
                            payload=a.model_dump(mode="json"),
                        )
                        for a in canonical.annotations
                    ]
                )
                if status == "complete" and not parent_job:
                    # Runs stay independent; only the latest successful run for this parser is active.
                    session.execute(
                        update(RunRow)
                        .where(RunRow.document_id == doc_id, RunRow.parser == config.parser)
                        .values(active=False)
                    )
                current = session.get(RunRow, run_id)
                current.status, current.active = status, status == "complete" and not parent_job
                current.parser_version, current.metrics = canonical.parser_version, metrics
                current.annotation_status, current.indexing_status = (
                    annotation_status,
                    indexing_status,
                )
                current.errors = {"pages": canonical.failures, "warnings": canonical.warnings}
            report = {
                "title": f"{config.parser} ingestion {run_id}",
                "manifest": manifest,
                "execution": metrics,
                "annotation_status": annotation_status,
                "indexing_status": indexing_status,
                "environment": {
                    "python": platform.python_version(),
                    "platform": platform.platform(),
                    "packages": {
                        p: importlib.metadata.version(p)
                        for p in ["document-ingestion", "llama-index-core", "sqlalchemy"]
                    },
                },
                "reproduce": {
                    "command": f'doc-ingest ingest "{path.name}" --year {year}'
                    + (" --pages " + " ".join(map(str, config.pages)) if config.pages else "")
                    + (" --debug" if debug else ""),
                    "processing_config": config.model_dump(mode="json"),
                },
            }
            report_key = config.parser
            write_report(self.settings.report_dir / report_key / run_id, report)
            with self.sessions() as session:
                return self.response(session.get(DocumentRow, doc_id), session.get(RunRow, run_id))
        except Exception as exc:
            with self.sessions.begin() as session:
                current = session.get(RunRow, run_id)
                if current.status == "processing":
                    current.status = "failed"
                    current.errors = {"type": type(exc).__name__, "message": str(exc)}
            raise

    def response(self, document: DocumentRow, run: RunRow) -> dict:
        blocked = resume_block_reason(run)
        inspector = run.metrics.get("inspector")
        debug_url = (
            "/artifacts/" + inspector
            if inspector
            else (
                f"/artifacts/{run.artifact_key}/index.html"
                if (
                    (self.settings.artifact_dir / run.artifact_key / "index.html").exists()
                    or (self.cloud.enabled and run.metrics.get("cloud_html", False))
                )
                else None
            )
        )
        return {
            "can_resume": blocked is None,
            "resume_block_reason": blocked,
            "queued_at": run.metrics.get("queued_at"),
            "deletion_requested": bool(run.metrics.get("deletion_requested")),
            "pipeline_order": run.metrics.get("pipeline_order", [run.parser]),
            "document_id": document.id,
            "ingestion_run_id": run.id,
            "filename": document.filename,
            "year": document.year,
            "total_pages": document.page_count,
            "chunk_count": run.metrics.get("chunk_count", 0),
            "parser": run.parser,
            "status": run.status,
            "annotation_status": run.annotation_status,
            "progress": {
                k: run.metrics[k]
                for k in (
                    "completed_pages",
                    "processed_pages",
                    "batch_size",
                    "checkpoint_interval",
                    "stage",
                    "batch_start_page",
                    "current_pipeline",
                    "pipeline_progress",
                    "total_work_pages",
                )
                if k in run.metrics
            },
            "job": run.metrics.get("job_version") == 1,
            "errors": run.errors,
            "debug_url": debug_url,
        }
