import json

import pytest
from conftest import FixtureParser

from ingestion.annotations import annotate
from ingestion.chunk_comparison import compare_bundles
from ingestion.chunking import create_chunks
from ingestion.config import ProcessingConfig
from ingestion.contextual import enrich_chunks, source_budget
from ingestion.images import apply_image_decision, filter_assets
from ingestion.schemas import Annotation
from ingestion.storage import write_json
from ingestion.tokenizer import TokenBudget
from ingestion.topology import enrich_retrieval, topology_chunks


@pytest.mark.parametrize(
    "role,substantive,uncertainty,kind,expected",
    [
        ("decorative", False, [], "picture", "exclude_from_retrieval"),
        ("mixed", True, [], "picture", "keep"),
        ("substantive", True, [], "picture", "keep"),
        ("decorative", False, [], "chart", "keep"),
        ("decorative", False, ["unclear crop"], "picture", "review_required"),
        ("uncertain", False, [], "picture", "review_required"),
    ],
)
def test_contextual_image_policy(setup, tmp_path, role, substantive, uncertainty, kind, expected):
    _, config, pdf = setup
    doc = FixtureParser().parse(pdf, config, tmp_path, 2025)
    asset = doc.assets[0]
    asset.bbox = (0, 0, 1, 1)  # Large central logos are not protected by size.
    doc.elements[1].kind = kind
    annotation = Annotation(
        asset_id=asset.id,
        description="Region",
        picture_type="logo",
        content_role=role,
        contains_substantive_information=substantive,
        recommended_action="exclude_from_retrieval",
        reason="Purpose",
        uncertainty=uncertainty,
    )
    apply_image_decision(doc, asset, annotation)
    assert asset.decision == expected
    assert (tmp_path / asset.key).exists()


def test_preexcluded_images_reach_vision_and_are_removed_from_both_workflows(setup, tmp_path):
    _, config, pdf = setup
    doc = FixtureParser().parse(pdf, config, tmp_path, 2025)
    doc.assets[0].decision = "exclude_from_retrieval"
    filter_assets(doc, tmp_path)
    assert doc.assets[0].decision == "review_required"
    doc.assets[0].decision = "exclude_from_retrieval"

    class Client:
        def complete(self, *args):
            return json.dumps(
                dict(
                    description="BrandOnly",
                    picture_type="logo",
                    content_role="decorative",
                    contains_substantive_information=False,
                    recommended_action="exclude_from_retrieval",
                    reason="Standalone branding",
                    labels=[],
                    axes=[],
                    units=[],
                    relationships=[],
                    uncertainty=[],
                    suggested_links=[],
                )
            ), {}

    annotate(doc, tmp_path, config, Client(), tmp_path / "cache")
    assert len(doc.annotations) == 1
    doc.elements[1].text = "BrandOnly"
    doc.native_chunks = [{"text": "Revenue BrandOnly", "element_ids": ["e1", "e2"]}]
    projected = enrich_retrieval(doc)
    budget = TokenBudget(config.tokenizer_path, config.max_tokens)
    variants = [
        create_chunks(projected, config, budget),
        topology_chunks(projected, config, budget)[0],
    ]
    for chunks in variants:
        assert chunks
        assert all("BrandOnly" not in c.retrieval_text and not c.asset_ids for c in chunks)
        assert all("e2" not in c.element_ids for c in chunks)
    assert doc.elements[1].text == "BrandOnly"


class ContextClient:
    def __init__(self, response=None):
        self.calls = 0
        self.response = response or {"context": "Revenue in 2025.", "source_ids": ["e1"]}

    def complete(self, model, messages, max_tokens, extra):
        self.calls += 1
        assert extra["response_format"]["json_schema"]["strict"]
        return json.dumps(self.response), {"usage": {"total_tokens": 42}}


def test_chunk_context_preserves_sources_and_caches_both_workflows(setup, tmp_path):
    _, original, pdf = setup
    config = ProcessingConfig(
        tokenizer_path=original.tokenizer_path,
        model_dir=tmp_path,
        target_tokens=100,
        max_tokens=400,
        contextual_enrichment_enabled=True,
    )
    doc = FixtureParser().parse(pdf, config, tmp_path, 2025)
    for factory in [
        lambda: create_chunks(doc, config, source_budget(config)),
        lambda: topology_chunks(doc, config, source_budget(config))[0],
    ]:
        chunks = factory()
        before = [(c.text, c.retrieval_text, c.element_ids[:]) for c in chunks]
        client = ContextClient()
        enrich_chunks(doc, chunks, config, client, tmp_path / "context-cache")
        for chunk, (text, retrieval, ids) in zip(chunks, before):
            assert chunk.contextual_provenance["status"] == "generated_unverified"
            assert chunk.text == text and chunk.element_ids == ids
            assert chunk.retrieval_text.endswith(retrieval)
            assert chunk.token_count <= config.max_tokens
        calls = client.calls
        enrich_chunks(doc, factory(), config, client, tmp_path / "context-cache")
        assert client.calls == calls


@pytest.mark.parametrize(
    "response",
    [
        {"context": "unsupported", "source_ids": ["invented"]},
        {"context": "word " * 1000, "source_ids": ["e1"]},
    ],
)
def test_invalid_context_preserves_retrieval_and_marks_failure(setup, tmp_path, response):
    _, original, pdf = setup
    config = original.model_copy(update={"max_tokens": 400, "contextual_enrichment_enabled": True})
    doc = FixtureParser().parse(pdf, config, tmp_path, 2025)
    chunks = create_chunks(doc, config, source_budget(config))
    before = [c.retrieval_text for c in chunks]
    enrich_chunks(doc, chunks, config, ContextClient(response), tmp_path / "cache")
    assert [c.retrieval_text for c in chunks] == before
    assert all(c.contextual_provenance["status"] == "failed" for c in chunks)


def test_contextual_comparison_reports_both_workflows(setup, tmp_path):
    _, original, pdf = setup
    config = original.model_copy(update={"max_tokens": 400, "contextual_enrichment_enabled": True})
    bundle = tmp_path / "bundle"
    doc = FixtureParser().parse(pdf, config, bundle, 2025)
    doc.native_chunks = [{"text": doc.elements[0].text, "element_ids": ["e1"]}]
    write_json(bundle / "canonical.json", doc.model_dump(mode="json"))
    write_json(bundle / "manifest.json", {"config": config.model_dump(mode="json")})
    report = compare_bundles([bundle], config, tmp_path / "report", context_client=ContextClient())
    assert set(report["metrics"]) == {"enriched_hybrid", "topology"}
    for metrics in report["metrics"].values():
        assert metrics["contextual_enrichment"]["successful"] == metrics["chunks"]
        assert metrics["contextual_enrichment"]["failed"] == 0
    assert "OpenAI chunk context" in (tmp_path / "report" / "index.html").read_text("utf-8")
