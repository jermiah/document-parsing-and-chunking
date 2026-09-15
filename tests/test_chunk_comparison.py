"""Regression tests for preservation, scoping, budgeting and honest evaluation."""

import json

import pytest
from bs4 import BeautifulSoup
from conftest import FixtureParser

from ingestion.chunk_comparison import compare_bundles, retrieval_evaluation
from ingestion.document_bundle import save_bundle
from ingestion.schemas import Annotation
from ingestion.storage import write_json
from ingestion.tokenizer import TokenBudget
from ingestion.topology import enrich_retrieval, topology_chunks


def test_annotation_projection_keeps_source_and_bounds_chunks(setup, tmp_path):
    _, config, pdf = setup
    doc = FixtureParser().parse(pdf, config, tmp_path / "bundle", 2025)
    doc.annotations = [
        Annotation(
            asset_id="a1",
            description="Visible red and blue bars.",
            picture_type="chart",
            accepted_links=["e1"],
        )
    ]
    original = doc.model_dump_json()
    projected = enrich_retrieval(doc)
    assert "Visible red and blue bars." in projected.elements[1].text
    assert "generated, unverified" in projected.elements[1].text
    assert doc.model_dump_json() == original
    chunks, graph = topology_chunks(projected, config, TokenBudget(config.tokenizer_path, 160))
    assert {e for c in chunks for e in c.element_ids} == {"e1", "e2"}
    assert all(c.token_count <= 160 for c in chunks)
    assert any(e["kind"] == "annotation_link_unverified" for e in graph["edges"])
    assert all(c.parent_id in {p["id"] for p in graph["parents"]} for c in chunks)


def test_repeated_section_names_have_distinct_parents(setup, tmp_path):
    _, config, pdf = setup
    doc = FixtureParser().parse(pdf, config, tmp_path, 2025)
    first = doc.elements[0]
    doc.elements = [
        first,
        first.model_copy(update={"id": "other", "order": 1, "section": ["Other"]}),
        first.model_copy(update={"id": "return", "order": 2}),
    ]
    chunks, graph = topology_chunks(doc, config, TokenBudget(config.tokenizer_path, 160))
    assert len(graph["parents"]) == 3
    assert len({c.parent_id for c in chunks}) == 3


def test_report_no_false_accuracy_and_escaped_content(setup, tmp_path):
    _, config, pdf = setup
    path = tmp_path / "saved"
    doc = FixtureParser().parse(pdf, config, path, 2025)
    doc.filename = "<script>alert(1)</script>.pdf"
    doc.native_chunks = [{"text": doc.elements[0].text, "element_ids": ["e1"]}]
    write_json(path / "canonical.json", doc.model_dump(mode="json"))
    write_json(path / "manifest.json", {"config": config.model_dump(mode="json")})
    before = (path / "canonical.json").read_bytes()
    report = compare_bundles([path], config, tmp_path / "report")
    assert report["accuracy_winner"] is None
    assert all(m["retrieval"]["status"] == "not_run" for m in report["metrics"].values())
    soup = BeautifulSoup((tmp_path / "report/index.html").read_text("utf-8"), "html.parser")
    assert not soup.find_all("script")
    assert len(soup.select(".variants > div")) == 2
    assert set(report["metrics"]) == {"enriched_hybrid", "topology"}
    assert "Existing hybrid" not in soup.get_text()
    assert "Reusable document route" not in soup.get_text()
    assert not (tmp_path / "report/existing_hybrid.jsonl").exists()
    assert (path / "canonical.json").read_bytes() == before
    assert (tmp_path / "report/documents/saved/source.html").is_file()
    for strategy in report["metrics"]:
        lines = (tmp_path / "report" / (strategy + ".jsonl")).read_text().splitlines()
        assert len(lines) == report["metrics"][strategy]["chunks"]
        assert all(json.loads(line)["bundle"] == "saved" for line in lines)


def test_prepared_bundle_preserves_annotations_without_chunks(setup, tmp_path):
    _, config, pdf = setup
    doc = FixtureParser().parse(pdf, config, tmp_path / "prepared", 2025)
    doc.native_chunks = [{"text": "previous chunks"}]
    doc.annotations = [Annotation(asset_id="a1", description="A chart.", picture_type="chart")]
    save_bundle(doc, tmp_path / "prepared")
    saved = json.loads((tmp_path / "prepared/canonical.json").read_text())
    sidecar = json.loads((tmp_path / "prepared/enrichment.json").read_text())
    assert not saved["native_chunks"]
    assert saved["annotations"][0]["description"] == "A chart."
    assert sidecar["pictures"][0]["annotations"][0]["status"] == "generated_unverified"
    assert doc.native_chunks


def test_evaluation_requires_verified_labels_and_exact_evidence(setup, tmp_path):
    _, config, pdf = setup
    doc = FixtureParser().parse(pdf, config, tmp_path, 2025)
    chunks, _ = topology_chunks(doc, config, TokenBudget(config.tokenizer_path, 160))
    rows = [{"bundle": "sample", "chunk": c} for c in chunks]
    with pytest.raises(ValueError):
        retrieval_evaluation(rows, [{"question": "Revenue?"}], 160)
    gold = [
        {
            "verified": True,
            "question": "Revenue 2025 euros",
            "evidence": [{"bundle": "sample", "element_id": "e1", "quote": "120 million euros"}],
        }
    ]
    scores = retrieval_evaluation(rows, gold, 160)
    assert scores["hit_rate"] == 1
    assert "not embedding" in scores["method"]
    gold[0]["evidence"][0]["quote"] = "not present"
    assert retrieval_evaluation(rows, gold, 160)["hit_rate"] == 0


def test_overlapping_runs_rejected(setup, tmp_path):
    _, config, pdf = setup
    paths = []
    for name in ("one", "two"):
        path = tmp_path / name
        doc = FixtureParser().parse(pdf, config, path, 2025)
        doc.native_chunks = [{"text": doc.elements[0].text, "element_ids": ["e1"]}]
        write_json(path / "canonical.json", doc.model_dump(mode="json"))
        write_json(path / "manifest.json", {"config": config.model_dump(mode="json")})
        paths.append(path)
    with pytest.raises(ValueError, match="overlap"):
        compare_bundles(paths, config, tmp_path / "report")
