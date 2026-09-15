"""Mechanical invariants are distinct from manually measured extraction quality."""

import numpy as np

from ingestion.chunking import eligible_elements
from ingestion.schemas import CanonicalDocument, Chunk


def fraction(numerator: int, denominator: int) -> dict:
    return {
        "numerator": numerator,
        "denominator": denominator,
        "value": numerator / denominator if denominator else None,
        "status": "measured" if denominator else "not_applicable",
    }


def scorecard(doc: CanonicalDocument, chunks: list[Chunk], cap: int) -> dict:
    eligible, excluded = eligible_elements(doc)
    source = {e.id for e in eligible if e.text or e.caption or e.asset_id}
    represented = {eid for c in chunks for eid in c.element_ids}
    assets = {a.id for a in doc.assets if a.decision != "exclude_from_retrieval"}
    attached = {aid for c in chunks for aid in c.asset_ids}
    tokens = [c.token_count for c in chunks]
    invalid = [
        c.id
        for c in chunks
        if not set(c.element_ids).issubset({e.id for e in doc.elements})
        or not set(c.asset_ids).issubset(assets)
        or not (1 <= c.start_page <= c.end_page <= doc.page_count)
    ]
    return {
        "version": "1",
        "contextual_enrichment": {
            "successful": sum(bool(c.contextual_text) for c in chunks),
            "failed": sum(c.contextual_provenance.get("status") == "failed" for c in chunks),
            "not_requested": sum(not c.contextual_provenance for c in chunks),
        },
        "token_budget_compliance": fraction(sum(t <= cap for t in tokens), len(tokens)),
        "token_distribution": dict(
            zip(["p50", "p95", "max"], map(float, np.percentile(tokens, [50, 95, 100])))
        )
        if tokens
        else None,
        "parsed_element_retention": fraction(len(source & represented), len(source)),
        "parsed_asset_retention": fraction(len(assets & attached), len(assets)),
        "provenance_validity": fraction(len(chunks) - len(invalid), len(chunks)),
        "missing_geometry": [e.id for e in doc.elements if e.bbox is None],
        "invalid_chunks": invalid,
        "unrepresented_elements": sorted(source - represented),
        "policy_exclusions": excluded,
        "overflows": [c.id for c in chunks if c.token_count > cap],
        "gold_evidence_coverage": {
            "status": "not_run",
            "reason": "Requires verified source labels",
        },
        "structural_quality": {
            "status": "not_run",
            "reason": "Requires verified table/figure labels",
        },
    }
