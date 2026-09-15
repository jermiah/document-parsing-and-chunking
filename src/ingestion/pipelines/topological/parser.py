"""Extract locally with Docling and OCR; never upload page renders to OpenAI."""

from ingestion.pipelines.docling_hybrid.parser import DoclingParser


class TopologicalParser:
    def __init__(self, extractor=None):
        self.extractor = extractor or DoclingParser()

    def parse(self, path, config, output, year):
        doc = self.extractor.convert(path, config, output, year, chunk=False)
        version = doc.parser_version
        doc.parser = "topology"
        doc.parser_version = "docling-topology-v2"
        doc.confidence.update(
            {
                "extraction_backend": "docling",
                "extraction_version": version,
                "full_page_openai_parsing": False,
                "topology_scope": "current_page_batch",
            }
        )
        for element in doc.elements:
            element.provenance["extraction_backend"] = "docling"
        return doc
