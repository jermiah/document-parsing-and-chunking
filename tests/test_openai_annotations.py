import json

import httpx
import pytest
from conftest import FixtureParser
from pydantic import SecretStr

from ingestion.annotations import annotate
from ingestion.config import Settings
from ingestion.vision import annotation_client


def test_openai_key_required_and_masked(setup):
    settings, config, _ = setup
    config = config.model_copy(update={"annotation_provider": "openai"})
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        annotation_client(Settings(_env_file=None, openai_api_key=""), config)
    settings = Settings(_env_file=None, openai_api_key="test-only-secret")
    assert "test-only-secret" not in repr(settings)
    assert "test-only-secret" not in settings.model_dump_json()
    client = annotation_client(settings, config)
    assert client.url == "https://api.openai.com/v1"


def test_openai_annotation_request_cache_and_source_preservation(setup, tmp_path, monkeypatch):
    settings, config, pdf = setup
    settings = Settings(_env_file=None, openai_api_key="test-only-secret")
    config = config.model_copy(
        update={"annotation_provider": "openai", "annotation_model": "gpt-4.1-mini-2025-04-14"}
    )
    output = tmp_path / "output"
    doc = FixtureParser().parse(pdf, config, output, 2025)
    original_text = [e.text for e in doc.elements]
    requests = []

    def handler(request):
        requests.append(request)
        payload = json.loads(request.content)
        assert request.url == "https://api.openai.com/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer test-only-secret"
        assert payload["response_format"]["type"] == "json_schema"
        assert payload["response_format"]["json_schema"]["strict"] is True
        assert payload["messages"][0]["content"][1]["image_url"]["url"].startswith(
            "data:image/png;base64,"
        )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(
                                {
                                    "description": "Two bars",
                                    "picture_type": "bar chart",
                                    "content_role": "substantive",
                                    "contains_substantive_information": True,
                                    "recommended_action": "keep",
                                    "reason": "Chart data",
                                    "labels": [],
                                    "axes": [],
                                    "units": [],
                                    "relationships": [],
                                    "uncertainty": [],
                                    "suggested_links": [
                                        {"element_id": "e1", "reason": "related text"},
                                        {"element_id": "unknown", "reason": "invalid"},
                                    ],
                                }
                            )
                        },
                    }
                ]
            },
        )

    real_client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    client = annotation_client(settings, config)
    annotate(doc, output, config, client, tmp_path / "cache")
    assert doc.annotations[0].accepted_links == ["e1"]
    assert doc.annotations[0].provenance["provider"] == "openai"
    assert [e.text for e in doc.elements] == original_text
    assert doc.assets[0].caption == "Revenue comparison"
    doc.annotations.clear()
    annotate(doc, output, config, client, tmp_path / "cache")
    assert len(requests) == 1


def test_openai_refusal_preserves_image(setup, tmp_path, monkeypatch):
    _, config, pdf = setup
    config = config.model_copy(update={"annotation_provider": "openai"})
    doc = FixtureParser().parse(pdf, config, tmp_path, 2025)
    real_client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: real_client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json={
                        "choices": [
                            {
                                "finish_reason": "stop",
                                "message": {"refusal": "declined", "content": None},
                            }
                        ]
                    },
                )
            ),
            **kwargs,
        ),
    )
    client = annotation_client(Settings(_env_file=None, openai_api_key="test-only-secret"), config)
    annotate(doc, tmp_path, config, client, tmp_path / "cache")
    assert not doc.annotations and doc.assets
    assert any("Annotation failed" in w for w in doc.warnings)


@pytest.mark.parametrize("enabled,expected", [(True, "pending"), (False, "disabled")])
def test_annotation_status_while_parsing(setup, enabled, expected):
    from sqlalchemy import select

    from ingestion.ingestion import IngestionService
    from ingestion.storage import database
    from ingestion.tables import Base, RunRow

    settings, config, pdf = setup
    settings = settings.model_copy(update={"openai_api_key": SecretStr("test-only")})
    config = config.model_copy(update={"picture_annotations_enabled": enabled})
    engine, sessions = database(settings.database_url)
    Base.metadata.create_all(engine)

    class InspectParser:
        def parse(self, *args):
            with sessions() as session:
                run = session.scalar(select(RunRow))
                assert run.status == "processing"
                assert run.annotation_status == expected
            raise RuntimeError("Stop before inference")

    try:
        with pytest.raises(RuntimeError, match="Stop before inference"):
            IngestionService(settings, sessions, InspectParser()).ingest(pdf, 2025, config)
    finally:
        engine.dispose()
