"""Parse/enrich once, retain native Docling plus a lossless annotation sidecar."""

import json
from pathlib import Path

from ingestion.annotations import annotate
from ingestion.config import ProcessingConfig, Settings
from ingestion.images import filter_assets
from ingestion.ocr import recognize_figures
from ingestion.parser import DoclingParser
from ingestion.schemas import CanonicalDocument
from ingestion.storage import write_json
from ingestion.vision import annotation_client


def save_bundle(doc: CanonicalDocument, output: Path) -> None:
    """Keep generated statements separate from verbatim source and native schema."""
    clean = doc.model_copy(deep=True)
    clean.native_chunks = []
    write_json(output / "canonical.json", clean.model_dump(mode="json"))
    write_json(
        output / "enrichment.json",
        {
            "schema_version": 1,
            "native_document": "raw/docling_document.json",
            "canonical_document": "canonical.json",
            "description": "Application enrichment sidecar; not a native Docling JSON schema.",
            "pictures": [
                {
                    "asset": asset.model_dump(mode="json"),
                    "native_refs": [
                        e.provenance.get("native_ref", e.id)
                        for e in doc.elements
                        if e.asset_id == asset.id
                    ],
                    "annotations": [
                        a.model_dump(mode="json") for a in doc.annotations if a.asset_id == asset.id
                    ],
                }
                for asset in doc.assets
            ],
        },
    )


def prepare_document(
    pdf: Path, year: int, config: ProcessingConfig, settings: Settings, output: Path
) -> CanonicalDocument:
    if output.exists() and any(output.iterdir()):
        raise ValueError("Use an empty output directory for a new document bundle")
    output.mkdir(parents=True, exist_ok=True)
    doc = DoclingParser().convert(pdf, config, output, year)
    filter_assets(doc, output)
    recognize_figures(doc, output, config)
    if config.picture_annotations_enabled:
        annotate(
            doc,
            output,
            config,
            annotation_client(settings, config),
            settings.data_dir / "annotation_cache",
        )
    save_bundle(doc, output)
    write_json(
        output / "manifest.json",
        {
            "kind": "prepared_document",
            "config": config.model_dump(mode="json"),
            "status": "partial" if doc.failures else "complete",
            "scope": {"pages": doc.pages},
        },
    )
    return doc


def load_bundle(path: Path, config: ProcessingConfig) -> CanonicalDocument:
    doc = CanonicalDocument.model_validate_json((path / "canonical.json").read_text("utf-8"))
    if not doc.native_chunks:
        from docling_core.types.doc import DoclingDocument

        from ingestion.parser import native_hybrid_chunks

        native = DoclingDocument.model_validate(
            json.loads((path / "raw/docling_document.json").read_text("utf-8"))
        )
        doc.native_chunks = native_hybrid_chunks(native, config)
    return doc
