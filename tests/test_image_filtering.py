"""Excluded visual content never reaches any pipeline's chunk input."""

import json

import pytest
from conftest import FixtureParser

from ingestion.images import apply_image_decision
from ingestion.pipelines.registry import pipeline
from ingestion.schemas import Annotation
from ingestion.tokenizer import TokenBudget
from ingestion.topology import enrich_retrieval


@pytest.mark.parametrize("name", ["docling", "topology"])
def test_deterministic_exclusion_before_each_chunker(setup, tmp_path, name):
    _, config, pdf = setup
    doc = FixtureParser().parse(pdf, config, tmp_path, 2025)
    asset = doc.assets[0]
    asset.caption = "ExcludedCaption"
    asset.ocr_text = "ExcludedOCR"
    doc.elements[1].text = "ExcludedSource"
    doc.elements[1].caption = "ExcludedCaption"
    annotation = Annotation(
        asset_id=asset.id,
        description="ExcludedDescription",
        picture_type="logo",
        content_role="decorative",
        contains_substantive_information=False,
        recommended_action="exclude_from_retrieval",
        reason="Standalone branding",
        accepted_links=["e1"],
    )
    doc.annotations = [annotation]
    apply_image_decision(doc, asset, annotation)
    doc.native_chunks = [{"text": "Revenue ExcludedSource", "element_ids": ["e1", "e2"]}]
    original = doc.model_dump()
    projected = enrich_retrieval(doc)
    assert not projected.assets and not projected.annotations and not projected.native_chunks
    assert [e.id for e in projected.elements] == ["e1"]
    assert doc.model_dump() == original

    class Refiner:
        def complete(self, model, messages, max_tokens, extra):
            assert "Excluded" not in json.dumps(messages)
            return json.dumps(
                {
                    "boundary_ids": [0],
                    "title": "",
                    "context": "",
                    "evidence_ids": [],
                    "request_ids": [],
                }
            ), {}

    chunks = pipeline(name).chunk(
        projected,
        config,
        TokenBudget(config.tokenizer_path, config.max_tokens),
        Refiner(),
        tmp_path,
    )
    assert chunks
    assert all(not c.asset_ids and "Excluded" not in c.retrieval_text for c in chunks)
    assert all(c.element_ids == ["e1"] for c in chunks)
