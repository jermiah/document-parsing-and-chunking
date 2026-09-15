"""Local extraction and evidence-grounded topology contracts, without model calls."""

import json
from unittest.mock import Mock

import pytest

from ingestion.pipelines.topological.agents import enhance
from ingestion.pipelines.topological.parser import TopologicalParser
from ingestion.pipelines.topological.pipeline import chunk
from ingestion.pipelines.topological.sir import SIRIndex
from ingestion.schemas import CanonicalDocument, Element
from ingestion.tokenizer import TokenBudget


def document():
    return CanonicalDocument(
        filename="report.pdf",
        checksum="x",
        year=2025,
        page_count=1,
        pages=[1],
        parser="docling",
        parser_version="native",
        config_hash="test",
        elements=[
            Element(
                id="p",
                kind="paragraph",
                page=1,
                order=1,
                text="Revenue increased. " * 40,
                section=["Results"],
            )
        ],
    )


def test_topological_parser_only_uses_local_docling(setup, tmp_path):
    _, config, pdf = setup
    original = document()
    extractor = Mock()
    extractor.convert.return_value = original
    result = TopologicalParser(extractor).parse(pdf, config, tmp_path, 2025)
    extractor.convert.assert_called_once_with(pdf, config, tmp_path, 2025, chunk=False)
    assert result is original
    assert result.elements[0].section == ["Results"]
    assert result.confidence["full_page_openai_parsing"] is False
    assert result.confidence["extraction_version"] == "native"
    assert result.parser == "topology"


def graph():
    nodes = [
        {"id": "root", "parent_id": None, "lineage": ["Report"]},
        {"id": "a", "parent_id": "root", "lineage": ["Report", "Definition"]},
        {"id": "b", "parent_id": "root", "lineage": ["Report", "Results"]},
    ]
    return SIRIndex(nodes, {"a": "LNG means liquefied natural gas.", "b": "Its revenue increased."})


def test_sir_traverses_and_bounds_evidence():
    sir = graph()
    assert sir.nodes["a"]["next_sibling"] == "b"
    assert sir.nodes["b"]["prev_sibling"] == "a"
    assert sir.nodes["root"]["children"] == ["a", "b"]
    result = sir.query(["b"])
    assert {item["id"] for item in result} == {"a", "b", "root"}
    assert sum(len(item["text"]) for item in sir.query(["a"], max_chars=9)) == 9
    assert sir.query(["a"], max_chars=9)[0]["truncated"]
    with pytest.raises(ValueError, match="Unknown"):
        sir.query(["invented"])


class Client:
    def __init__(self):
        self.bodies = []

    def complete(self, model, messages, max_tokens, extra):
        assert all(isinstance(message["content"], str) for message in messages)
        schema = extra["response_format"]["json_schema"]["name"]
        if schema == "Route":
            response = {"path": "SEMANTIC_FLOW", "reason": "Narrative evidence"}
        elif schema == "Refinement":
            response = {
                "boundary_ids": [0],
                "title": "",
                "context": "",
                "evidence_ids": [],
                "request_ids": [],
            }
        else:
            body = json.loads(messages[1]["content"])["chunk"]
            self.bodies.append(body)
            response = {
                "title": "Revenue growth analysis",
                "context": "",
                "evidence_ids": [],
                "request_ids": [],
            }
        return json.dumps(response), {"usage": {}}


def test_each_final_chunk_gets_own_enhancement(setup, tmp_path):
    _, config, _ = setup
    doc, client = document(), Client()
    budget = TokenBudget(config.tokenizer_path, config.max_tokens)
    chunks = chunk(doc, config, budget, client, tmp_path)
    assert len(chunks) > 1
    assert client.bodies == [c.text for c in chunks]
    assert "".join(c.text for c in chunks) == doc.elements[0].text
    assert all(c.token_count <= budget.cap for c in chunks)
    assert all(
        c.topology_provenance["enhancement"]["status"] == "generated_unverified" for c in chunks
    )


def test_enhancer_rejects_invented_evidence(setup, tmp_path):
    _, config, _ = setup
    client = Mock()
    client.complete.return_value = (
        json.dumps(
            {
                "title": "Revenue growth analysis",
                "context": "Invented fact",
                "evidence_ids": ["unknown"],
                "request_ids": [],
            }
        ),
        {},
    )
    with pytest.raises(ValueError, match="visible source"):
        enhance(client, config, "Its revenue increased.", ["b"], graph(), tmp_path, 0)
