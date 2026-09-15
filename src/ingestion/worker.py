"""A fresh model process for at most ten new pages and one context page."""

import json
import os
import sys
from pathlib import Path

from ingestion.config import ProcessingConfig, Settings
from ingestion.ingestion import IngestionService
from ingestion.parser import DoclingParser
from ingestion.storage import database, write_json
from ingestion.tables import DocumentRow, RunRow


class BatchParser:
    def __init__(self, pages, section, parser=None):
        self.pages, self.section = pages, section
        self.parser = parser or DoclingParser()

    def parse(self, path, config, output, year):
        doc = self.parser.parse(path, config, output, year)
        wanted = set(self.pages)
        doc.pages = self.pages
        doc.elements = [e for e in doc.elements if e.page in wanted]
        doc.assets = [a for a in doc.assets if a.page in wanted]
        ids = {e.id for e in doc.elements}
        # Do not retain narrative text from the context-only page.
        doc.native_chunks = [c for c in doc.native_chunks if set(c["element_ids"]) <= ids]
        for element in doc.elements:
            if not element.section:
                element.section = list(self.section)
            if element.kind == "table" and element.page in {self.pages[0], self.pages[-1]}:
                doc.warnings.append(
                    f"Batch-boundary table {element.id}: review continuation on adjacent pages"
                )
        from ingestion.images import filter_assets
        from ingestion.ocr import recognize_figures

        filter_assets(doc, output)
        if config.parser in {"docling", "topology"}:
            recognize_figures(doc, output, config)
        return doc


def main():
    job_id, offset_text, destination = sys.argv[1:]
    offset = int(offset_text)
    settings = Settings()
    engine, sessions = database(settings.database_url)
    service = IngestionService(settings, sessions)
    try:
        with sessions() as session:
            job = session.get(RunRow, job_id)
            doc = session.get(DocumentRow, job.document_id)
            config = ProcessingConfig.model_validate(job.metrics["config"])
            config = config.model_copy(
                update={
                    "parser": job.metrics.get("current_pipeline", config.parser),
                    "pipeline_comparison": False,
                    "selected_pipelines": None,
                    "compare_workflows": False,
                }
            )
            pages = job.metrics["pages"][offset : offset + 10]
            context = [pages[0] - 1] if pages[0] > 1 else []
            config = config.model_copy(update={"pages": context + pages})
            filename, year, key = doc.filename, doc.year, doc.original_key
        from ingestion.pipelines.registry import pipeline

        parser = BatchParser(
            pages,
            json.loads(os.environ.get("BATCH_SECTION", "[]")),
            pipeline(config.parser).parser(settings, config),
        )
        service.parser = parser
        path = service.cloud.get(settings.data_dir, key, "data")
        result = service.ingest(path, year, config, True, True, filename, parent_job=job_id)
        write_json(Path(destination), result)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
