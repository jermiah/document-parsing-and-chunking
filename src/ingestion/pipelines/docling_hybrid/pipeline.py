"""Docling layout extraction, RapidOCR and native Hybrid grouping."""

from ingestion.chunking import create_chunks


def parser(settings, config):
    from ingestion.pipelines.docling_hybrid.parser import DoclingParser

    return DoclingParser()


def chunk(doc, config, budget, client, output):
    return create_chunks(doc, config, budget)
