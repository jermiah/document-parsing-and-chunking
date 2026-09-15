from types import SimpleNamespace

import pytest

from ingestion.config import ProcessingConfig
from ingestion.ocr import recognize_figures
from ingestion.ocr_options import run_with_ocr_fallback
from ingestion.schemas import Asset, CanonicalDocument


@pytest.mark.parametrize("text", ["recognized", ""])
def test_success_including_empty_does_not_retry(text):
    attempts = []
    assert run_with_ocr_fallback(ProcessingConfig(), lambda _: text, attempts) == (text, "rapidocr")
    assert len(attempts) == 1


def test_failure_recovers_and_records_engine():
    def operation(config):
        if config.ocr_engine == "rapidocr":
            raise RuntimeError("missing models")
        return "recovered"

    attempts = []
    assert run_with_ocr_fallback(ProcessingConfig(), operation, attempts) == (
        "recovered",
        "tesseract_cli",
    )
    assert [a["status"] for a in attempts] == ["failed", "complete"]


@pytest.mark.parametrize(
    "config",
    [
        ProcessingConfig(ocr_fallback_engine=None),
        ProcessingConfig(ocr_engine="tesseract_cli"),
    ],
)
def test_no_cycles_or_disabled_retry(config):
    def operation(_):
        raise RuntimeError("unavailable")

    attempts = []
    with pytest.raises(RuntimeError, match="OCR attempts failed"):
        run_with_ocr_fallback(config, operation, attempts)
    assert len(attempts) == 1


@pytest.mark.parametrize("retry", ["success", "partial", "exception"])
def test_partial_conversion_recovery_or_preservation(retry):
    def operation(config):
        if config.ocr_engine == "rapidocr":
            return "partial"
        if retry == "exception":
            raise RuntimeError("fallback unavailable")
        return retry

    result, engine = run_with_ocr_fallback(
        ProcessingConfig(), operation, [], is_success=lambda r: r == "success"
    )
    assert result == ("success" if retry == "success" else "partial")
    assert engine == ("tesseract_cli" if retry == "success" else "rapidocr")


def test_crop_fallback_and_total_failure_preserve_asset(tmp_path, monkeypatch):
    import sys

    import ingestion.ocr as module

    # Force RapidOCR initialization failure without requiring the optional package.
    monkeypatch.setitem(sys.modules, "rapidocr", None)
    asset = Asset(
        id="a", page=1, bbox=(0, 0, 1, 1), key="crop.png", checksum="hash", width=100, height=100
    )
    doc = CanonicalDocument(
        filename="x.pdf",
        checksum="hash",
        year=2025,
        page_count=1,
        parser="docling",
        parser_version="test",
        config_hash="config",
        pages=[1],
        assets=[asset],
    )
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="Revenue"))
    recognize_figures(doc, tmp_path, ProcessingConfig())
    assert asset.ocr_text == "Revenue"
    assert asset.signals["label_source"] == "tesseract_cli_crop"
    assert len(asset.signals["ocr_attempts"]) == 2

    def fail(*args, **kwargs):
        raise FileNotFoundError("Tesseract unavailable")

    monkeypatch.setattr(module.subprocess, "run", fail)
    recognize_figures(doc, tmp_path, ProcessingConfig())
    assert asset.signals["ocr_status"] == "failed"
    assert doc.assets == [asset]
    assert doc.warnings
