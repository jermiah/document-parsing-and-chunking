"""Reusable Docling conversion, with optional native HybridChunker preparation."""

import importlib.metadata
import json
from pathlib import Path

import pymupdf

from ingestion.config import ProcessingConfig
from ingestion.normalize import normalize
from ingestion.ocr_options import make_ocr_options, run_with_ocr_fallback
from ingestion.schemas import CanonicalDocument
from ingestion.storage import file_hash, write_json


class DoclingParser:
    def parse(
        self, pdf_path: Path, options: ProcessingConfig, output: Path, year: int
    ) -> CanonicalDocument:
        return self.convert(pdf_path, options, output, year, chunk=True)

    def convert(
        self,
        pdf_path: Path,
        options: ProcessingConfig,
        output: Path,
        year: int,
        *,
        chunk: bool = False,
    ) -> CanonicalDocument:
        """Save a native document; conversion alone never needs a chunk tokenizer."""
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        from docling.document_converter import DocumentConverter, PdfFormatOption

        if not options.model_dir.is_dir():
            raise RuntimeError(
                "Docling weights missing; run doc-ingest download-models before ingestion"
            )
        raw = output / "raw"
        raw.mkdir(parents=True, exist_ok=True)
        with pymupdf.open(pdf_path) as source:
            page_count = len(source)
            pages = options.pages or list(range(1, page_count + 1))
            if max(pages) > page_count:
                raise ValueError("Requested page exceeds PDF page count")
            conversion_path = pdf_path
            if options.pages:
                conversion_path = raw / "selected_pages.pdf"
                with pymupdf.open() as subset:
                    for number in pages:
                        subset.insert_pdf(source, from_page=number - 1, to_page=number - 1)
                    subset.save(conversion_path)

        def convert_with_engine(selected):
            ocr_options = make_ocr_options(selected)
            pipeline = PdfPipelineOptions(
                artifacts_path=options.model_dir.resolve(),
                do_ocr=True,
                ocr_options=ocr_options,
                do_table_structure=True,
                generate_page_images=True,
                generate_picture_images=True,
                images_scale=options.render_dpi / 72,
                allow_external_plugins=False,
                enable_remote_services=False,
                do_formula_enrichment=options.formula_enrichment,
            )
            converter = DocumentConverter(
                format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline)}
            )
            return converter.convert(conversion_path, raises_on_error=False)

        attempts = []
        try:
            result, actual_engine = run_with_ocr_fallback(
                options,
                convert_with_engine,
                attempts,
                is_success=lambda result: result.status.value == "success",
            )
        finally:
            write_json(raw / "ocr_attempts.json", attempts)
        native = result.document
        native.save_as_json(raw / "docling_document.json")
        restored = type(native).model_validate_json(
            (raw / "docling_document.json").read_text(encoding="utf-8")
        )
        if len(restored.texts) != len(native.texts) or [t.data for t in restored.tables] != [
            t.data for t in native.tables
        ]:
            raise ValueError("Native Docling round-trip lost text or table structure")
        doc = CanonicalDocument(
            filename=pdf_path.name,
            checksum=file_hash(pdf_path),
            year=year,
            page_count=page_count,
            parser="docling",
            parser_version=importlib.metadata.version("docling"),
            config_hash=options.fingerprint(),
            pages=pages,
        )
        if len(attempts) > 1:
            doc.warnings.append("OCR fallback attempted: " + json.dumps(attempts))
        page_map = dict(enumerate(pages, 1))
        normalize(native, doc, output, page_map)
        missing_pages = set(page_map) - set(native.pages)
        for number in missing_pages:
            doc.failures[page_map[number]] = "Page missing from native conversion"
        if str(result.status.value) != "success":
            # A partial conversion is never published as a successful complete run.
            errors = [str(e) for e in result.errors]
            doc.warnings.extend(errors)
            doc.failures = {p: "Conversion status: " + result.status.value for p in pages}
        confidence = json.loads(result.confidence.model_dump_json())
        doc.confidence = {
            "native": confidence,
            "ocr_engine": actual_engine,
            "ocr_attempts": attempts,
            "table_score": None,
            "table_score_reason": "Docling table_score is not a measured table-quality metric",
        }
        if chunk:
            doc.native_chunks = native_hybrid_chunks(native, options)
            write_json(raw / "native_chunks.json", doc.native_chunks)
        return doc


def native_hybrid_chunks(native, options: ProcessingConfig) -> list[dict]:
    """Run chunking independently on an already converted DoclingDocument."""
    from docling_core.transforms.chunker.hybrid_chunker import HybridChunker
    from docling_core.transforms.chunker.tokenizer.huggingface import HuggingFaceTokenizer
    from transformers import PreTrainedTokenizerFast

    tokenizer = PreTrainedTokenizerFast(tokenizer_file=str(options.tokenizer_path))
    chunker = HybridChunker(
        tokenizer=HuggingFaceTokenizer(tokenizer=tokenizer, max_tokens=options.target_tokens),
        repeat_table_header=True,
        omit_header_on_overflow=False,
    )
    chunks = []
    for chunk in chunker.chunk(dl_doc=native):
        chunks.append(
            {
                "text": chunk.text,
                "contextualized": chunker.contextualize(chunk=chunk),
                "element_ids": [item.self_ref for item in getattr(chunk.meta, "doc_items", [])],
                "headings": getattr(chunk.meta, "headings", None) or [],
            }
        )
    return chunks
