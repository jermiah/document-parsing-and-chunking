"""Thin synchronous API routes run parsing outside FastAPI's event loop."""

import json
import logging
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from sqlalchemy import func, select, text

from ingestion.config import ProcessingConfig, Settings, load_config
from ingestion.ingestion import IngestionService
from ingestion.jobs import JobRunner
from ingestion.run_cleanup import RunBusyError, delete_run
from ingestion.storage import database
from ingestion.tables import ChunkRow, DocumentRow, RunRow

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None, parser=None) -> FastAPI:
    settings = settings or Settings()
    engine, sessions = database(settings.database_url)
    service = IngestionService(settings, sessions, parser)
    runner = JobRunner(settings, engine, sessions) if parser is None else None

    @asynccontextmanager
    async def lifespan(app):
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        service.cloud.ensure_bucket()
        if runner:
            runner.start()
        try:
            yield
        finally:
            if runner:
                runner.stop()
            engine.dispose()

    app = FastAPI(title="Document Ingestion", version="0.1.0", lifespan=lifespan)
    app.state.engine, app.state.sessions = engine, sessions

    @app.middleware("http")
    async def same_origin_writes(request: Request, call_next):
        # Prevent an unrelated website from submitting local uploads/reprocess requests.
        origin = request.headers.get("origin")
        if request.method not in {"GET", "HEAD", "OPTIONS"} and (
            request.headers.get("sec-fetch-site") == "cross-site"
            or (origin and origin != str(request.base_url).rstrip("/"))
        ):
            return JSONResponse({"detail": "Cross-site write rejected"}, status_code=403)
        return await call_next(request)

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def home():
        return HTMLResponse(
            Path(__file__).with_name("dashboard.html").read_text(encoding="utf-8"),
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/v1/documents")
    def documents(offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)):
        with sessions() as session:
            total = session.scalar(select(func.count()).select_from(DocumentRow))
            docs = session.scalars(
                select(DocumentRow)
                .order_by(DocumentRow.created_at.desc())
                .offset(offset)
                .limit(limit)
            ).all()
            ids = [d.id for d in docs]
            runs = session.scalars(select(RunRow).where(RunRow.document_id.in_(ids))).all()
            runs.sort(key=lambda r: r.metrics.get("queued_at", 0))
            return {
                "total": total,
                "storage": settings.storage_backend,
                "documents": [
                    {
                        "document_id": d.id,
                        "filename": d.filename,
                        "year": d.year,
                        "total_pages": d.page_count,
                        "original_url": f"/v1/documents/{d.id}/original",
                        "runs": [
                            {**service.response(d, r), "active": r.active}
                            for r in runs
                            if r.document_id == d.id and not r.metrics.get("parent_job")
                        ],
                    }
                    for d in docs
                ],
            }

    @app.get("/v1/documents/{document_id}/original")
    def original(document_id: str):
        with sessions() as session:
            doc = session.get(DocumentRow, document_id)
            if doc is None:
                raise HTTPException(404, "Document not found")
            try:
                path = service.cloud.get(settings.data_dir, doc.original_key, "data")
            except FileNotFoundError:
                raise HTTPException(404, "Original PDF unavailable") from None
            return FileResponse(path, media_type="application/pdf", filename=doc.filename)

    @app.post(
        "/v1/documents/{document_id}/reprocess", responses={202: {"description": "Job queued"}}
    )
    def reprocess(document_id: str, pipelines: Annotated[str | None, Form()] = None):
        with sessions() as session:
            doc = session.get(DocumentRow, document_id)
            if doc is None:
                raise HTTPException(404, "Document not found")
            filename, year, key = doc.filename, doc.year, doc.original_key
        try:
            path = service.cloud.get(settings.data_dir, key, "data")
            result = service.ingest(
                path,
                year,
                selected_config(pipelines),
                True,
                True,
                filename,
                queued=runner is not None,
            )
            return JSONResponse(
                result, status_code=202 if result["status"] in {"queued", "processing"} else 200
            )
        except FileNotFoundError:
            raise HTTPException(404, "Original PDF unavailable; upload it again") from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except RuntimeError:
            logger.exception("reprocess_failure")
            raise HTTPException(503, "Processing failed; check API logs") from None

    @app.get("/v1/runs/{run_id}")
    def run_status(run_id: str):
        with sessions() as session:
            run = session.get(RunRow, run_id)
            if run is None or run.metrics.get("parent_job"):
                raise HTTPException(404, "Run not found")
            return service.response(session.get(DocumentRow, run.document_id), run)

    @app.post("/v1/runs/{run_id}/resume")
    def resume(run_id: str):
        if runner is None:
            raise HTTPException(409, "Job worker is unavailable")
        try:
            return JSONResponse(runner.resume(run_id), status_code=202)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.delete("/v1/runs/{run_id}")
    def remove_run(run_id: str):
        try:
            return delete_run(settings, sessions, service.cloud, run_id)
        except LookupError:
            raise HTTPException(404, "Run not found") from None
        except (RunBusyError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc
        except Exception:
            logger.exception("run_cleanup_failed %s", run_id)
            raise HTTPException(
                503, "Cleanup did not finish. Retry Delete run; see API logs."
            ) from None

    @app.get("/health/live")
    def live():
        return {"status": "ok"}

    @app.get("/health/ready")
    def ready():
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
                connection.execute(select(DocumentRow.id).limit(1))
            config = load_config(settings.config_path)
            if not config.tokenizer_path.is_file():
                raise RuntimeError("Tokenizer missing")
            if config.parser == "docling" and not config.model_dir.is_dir():
                raise RuntimeError("Docling models missing")
            return {"status": "ready", "parser": config.parser}
        except Exception:
            raise HTTPException(503, "Database, migrations, or parser assets unavailable") from None

    def selected_config(pipelines):
        config = load_config(settings.config_path)
        if pipelines is not None:
            try:
                selection = json.loads(pipelines)
                config = ProcessingConfig.model_validate(
                    {**config.model_dump(), "selected_pipelines": selection}
                )
                if selection is None:
                    raise ValueError("Select at least one pipeline")
            except (ValueError, TypeError) as exc:
                raise HTTPException(
                    422, "Select one or more valid pipelines without duplicates"
                ) from exc
        return config

    @app.post(
        "/v1/documents/ingest", responses={202: {"description": "Job queued; poll the run ID"}}
    )
    def ingest(
        file: Annotated[UploadFile, File()],
        year: Annotated[int, Form(ge=1900, le=2100)],
        debug: Annotated[bool, Form()] = False,
        force: Annotated[bool, Form()] = False,
        pipelines: Annotated[str | None, Form()] = None,
    ):
        config = selected_config(pipelines)
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                suffix=".pdf", dir=settings.data_dir, delete=False
            ) as out:
                temporary = Path(out.name)
                size = 0
                while block := file.file.read(1024 * 1024):
                    size += len(block)
                    if size > settings.max_upload_mb * 1024 * 1024:
                        raise HTTPException(413, "PDF exceeds upload size limit")
                    out.write(block)
            result = service.ingest(
                temporary, year, config, debug, force, file.filename, queued=runner is not None
            )
            return JSONResponse(
                result, status_code=202 if result["status"] in {"queued", "processing"} else 200
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except RuntimeError as exc:
            logger.exception("ingestion_dependency_failure")
            raise HTTPException(
                503, "Parser dependency unavailable; see server log and preflight"
            ) from exc
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)
            file.file.close()

    @app.post("/v1/documents/batch", status_code=202)
    def ingest_batch(
        files: Annotated[list[UploadFile], File()],
        years: Annotated[str, Form()],
        pipelines: Annotated[str | None, Form()] = None,
    ):
        """Queue PDFs independently; years is a JSON array matching upload order."""
        try:
            selected_config(pipelines)
            values = json.loads(years)
            if (
                not isinstance(values, list)
                or len(values) != len(files)
                or not 1 <= len(files) <= 50
                or any(type(y) is not int or not 1900 <= y <= 2100 for y in values)
            ):
                raise ValueError("Supply 1–50 PDFs and one integer year (1900–2100) per PDF")
        except (ValueError, TypeError) as exc:
            for file in files:
                file.file.close()
            raise HTTPException(422, str(exc)) from exc
        results = []
        try:
            for index, (file, year) in enumerate(zip(files, values)):
                try:
                    response = ingest(file, year, debug=True, force=False, pipelines=pipelines)
                    results.append(
                        {
                            "index": index,
                            "filename": file.filename,
                            "result": json.loads(response.body),
                        }
                    )
                except HTTPException as exc:
                    results.append(
                        {
                            "index": index,
                            "filename": file.filename,
                            "error": exc.detail,
                            "status_code": exc.status_code,
                        }
                    )
        finally:
            for file in files:
                file.file.close()
        return {
            "documents": results,
            "accepted": sum("result" in r for r in results),
            "rejected": sum("error" in r for r in results),
        }

    @app.get("/v1/documents/{document_id}/chunks")
    def chunks(
        document_id: str,
        run_id: str | None = None,
        page: int | None = Query(None, ge=1),
        offset: int = Query(0, ge=0),
        limit: int = Query(50, ge=1, le=200),
    ):
        with sessions() as session:
            if session.get(DocumentRow, document_id) is None:
                raise HTTPException(404, "Document not found")
            selected = (
                session.get(RunRow, run_id)
                if run_id
                else session.scalar(
                    select(RunRow).where(RunRow.document_id == document_id, RunRow.active.is_(True))
                )
            )
            if run_id and (selected is None or selected.document_id != document_id):
                raise HTTPException(404, "Run not found for this document")
            if selected and selected.metrics.get("deletion_requested"):
                raise HTTPException(409, "This run is being deleted")
            batch_ids = (
                selected.metrics.get("batches", [])
                if selected and selected.metrics.get("job_version") == 1
                else [selected.id]
                if selected
                else []
            )
            query = select(ChunkRow).where(
                ChunkRow.document_id == document_id, ChunkRow.run_id.in_(batch_ids)
            )
            if page:
                query = query.where(ChunkRow.start_page <= page, ChunkRow.end_page >= page)
            total = session.scalar(select(func.count()).select_from(query.subquery()))
            rows = session.scalars(
                query.order_by(ChunkRow.start_page, ChunkRow.run_id, ChunkRow.ordinal)
                .offset(offset)
                .limit(limit)
            )
            return {
                "total": total,
                "offset": offset,
                "limit": limit,
                "chunks": [
                    {**c.payload, "document_id": c.document_id, "run_id": c.run_id} for c in rows
                ],
            }

    @app.get("/artifacts/{key:path}")
    def artifact(key: str):
        parts = key.split("/")
        if len(parts) >= 3 and parts[0] == "runs":
            with sessions() as session:
                owner = session.get(RunRow, parts[2])
                parent = (
                    session.get(RunRow, owner.metrics.get("parent_job"))
                    if owner and owner.metrics.get("parent_job")
                    else None
                )
                if (
                    owner is None
                    or owner.document_id != parts[1]
                    or owner.metrics.get("deletion_requested")
                    or (parent and parent.metrics.get("deletion_requested"))
                ):
                    raise HTTPException(404, "Artifact not found")
        try:
            path = service.cloud.get(settings.artifact_dir, key, "artifacts")
        except (ValueError, FileNotFoundError):
            raise HTTPException(404, "Artifact not found") from None
        return FileResponse(
            path,
            headers={
                "Content-Security-Policy": "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; sandbox",
                "X-Content-Type-Options": "nosniff",
            },
        )

    return app
