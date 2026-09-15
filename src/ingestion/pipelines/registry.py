"""The explicit execution order and lazy pipeline dispatch."""

ORDER = ("docling", "topology")
LABELS = {
    "docling": "Docling + RapidOCR / Hybrid",
    "topology": "Topological / Inspector + SIR + Refiner",
}


def pipeline(name):
    if name == "docling":
        from ingestion.pipelines.docling_hybrid import pipeline as implementation
    elif name == "topology":
        from ingestion.pipelines.topological import pipeline as implementation
    else:
        raise ValueError(f"Unknown pipeline: {name}")
    return implementation
