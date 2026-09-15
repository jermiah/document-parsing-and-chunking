from conftest import FixtureParser
from fastapi.testclient import TestClient

from ingestion.api import create_app
from ingestion.tables import Base


def test_library_original_reprocess_and_origin(setup):
    settings, config, pdf = setup
    app = create_app(settings, FixtureParser())
    Base.metadata.create_all(app.state.engine)
    with TestClient(app) as client:
        assert "Your document library" in client.get("/").text
        result = client.post(
            "/v1/documents/ingest",
            files={"file": ("report.pdf", pdf.read_bytes())},
            data={"year": 2025, "debug": True},
        ).json()
        rows = client.get("/v1/documents").json()
        assert rows["total"] == 1
        doc = rows["documents"][0]
        assert doc["runs"][0]["debug_url"] == result["debug_url"]
        assert client.get(doc["original_url"]).content == pdf.read_bytes()
        url = "/v1/documents/" + doc["document_id"] + "/reprocess"
        assert client.post(url, headers={"Origin": "https://unrelated.example"}).status_code == 403
        rerun = client.post(url)
        assert rerun.status_code == 200
        assert rerun.json()["ingestion_run_id"] != result["ingestion_run_id"]
        assert len(client.get("/v1/documents").json()["documents"][0]["runs"]) == 2


def test_cloud_cache_download_and_upload(setup, monkeypatch, tmp_path):
    import httpx

    from ingestion.cloud import CloudFiles

    settings, config, pdf = setup
    settings = settings.model_copy(
        update={
            "storage_backend": "supabase",
            "supabase_url": "https://example.supabase.co",
            "supabase_secret_key": __import__("pydantic").SecretStr("sb_secret_test"),
        }
    )
    objects = {}

    def request(method, url, headers, timeout, **kwargs):
        assert headers["apikey"] == "sb_secret_test"
        assert "Authorization" not in headers
        key = url.split("/object/")[-1]
        if method == "POST":
            objects[key] = kwargs["content"].read()
            return httpx.Response(200, json={})
        return httpx.Response(200, content=objects[key]) if key in objects else httpx.Response(404)

    monkeypatch.setattr(httpx, "request", request)
    cloud = CloudFiles(settings)
    cloud.put("data/original.pdf", pdf)
    restored = cloud.get(tmp_path / "fresh", "original.pdf", "data")
    assert restored.read_bytes() == pdf.read_bytes()
    import pytest

    with pytest.raises(ValueError):
        cloud.get(tmp_path / "fresh", "../escape", "data")
    with pytest.raises(FileNotFoundError):
        cloud.get(tmp_path / "fresh", "missing.pdf", "data")


def test_shared_inspector_reuse_on_fresh_installation(setup, monkeypatch, tmp_path):
    import httpx
    from pydantic import SecretStr

    settings, config, pdf = setup
    settings = settings.model_copy(
        update={
            "storage_backend": "supabase",
            "supabase_url": "https://example.supabase.co",
            "supabase_secret_key": SecretStr("sb_secret_test"),
        }
    )
    objects = {}

    def request(method, url, **kwargs):
        if "/bucket/" in url:
            return httpx.Response(200, json={"public": False})
        key = url.split("/object/")[-1]
        if method == "POST":
            objects[key] = kwargs["content"].read()
            return httpx.Response(200, json={})
        return httpx.Response(200, content=objects[key]) if key in objects else httpx.Response(404)

    monkeypatch.setattr(httpx, "request", request)
    app = create_app(settings, FixtureParser())
    Base.metadata.create_all(app.state.engine)
    with TestClient(app) as client:
        first = client.post(
            "/v1/documents/ingest",
            files={"file": ("report.pdf", pdf.read_bytes())},
            data={"year": 2025, "debug": True},
        ).json()

    class NoParse:
        def parse(self, *args):
            raise AssertionError("Saved cloud run must be reused")

    fresh = settings.model_copy(
        update={
            "data_dir": tmp_path / "fresh-data",
            "artifact_dir": tmp_path / "fresh-artifacts",
            "report_dir": tmp_path / "fresh-reports",
        }
    )
    with TestClient(create_app(fresh, NoParse())) as client:
        listed = client.get("/v1/documents").json()["documents"][0]
        assert listed["runs"][0]["debug_url"] == first["debug_url"]
        html = client.get(first["debug_url"])
        assert html.status_code == 200 and "Source page 1" in html.text
        assert client.get(listed["original_url"]).content == pdf.read_bytes()
        reused = client.post(
            "/v1/documents/ingest",
            files={"file": ("report.pdf", pdf.read_bytes())},
            data={"year": 2025, "debug": True},
        ).json()
        assert reused["ingestion_run_id"] == first["ingestion_run_id"]
