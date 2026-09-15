"""Local English OCR configuration and observable, one-way engine fallback."""

import shutil
import subprocess

from ingestion.config import ProcessingConfig


def run_with_ocr_fallback(config, operation, attempts, is_success=lambda result: True):
    """Retry once with Tesseract; preserve a partial result if retry cannot recover it."""
    engines = [config.ocr_engine]
    if config.ocr_engine == "rapidocr" and config.ocr_fallback_engine:
        engines.append(config.ocr_fallback_engine)
    partial = None
    last_error = None
    for engine in engines:
        selected = config.model_copy(update={"ocr_engine": engine})
        try:
            result = operation(selected)
            complete = is_success(result)
            attempts.append({"engine": engine, "status": "complete" if complete else "partial"})
            if complete:
                return result, engine
            if partial is None:
                partial = (result, engine)
        except Exception as exc:
            last_error = exc
            attempts.append({"engine": engine, "status": "failed", "error": type(exc).__name__})
    if partial is not None:
        return partial
    raise RuntimeError(f"OCR attempts failed: {attempts}") from last_error


def make_ocr_options(config: ProcessingConfig):
    from docling.datamodel.pipeline_options import RapidOcrOptions, TesseractCliOcrOptions

    if config.ocr_engine == "rapidocr":
        import onnxruntime

        if "CPUExecutionProvider" not in onnxruntime.get_available_providers():
            raise RuntimeError("RapidOCR needs ONNX Runtime CPUExecutionProvider")
        return RapidOcrOptions(
            lang=["en"], backend="onnxruntime", force_full_page_ocr=config.force_full_page_ocr
        )
    executable = shutil.which("tesseract")
    if not executable:
        raise RuntimeError(
            "Install Tesseract with English traineddata or rebuild the application Docker image"
        )
    languages = subprocess.run(
        [executable, "--list-langs"],
        capture_output=True,
        text=True,
        check=True,
        timeout=config.request_timeout,
    )
    if "eng" not in languages.stdout.split():
        raise RuntimeError("Tesseract English traineddata (eng) is missing")
    return TesseractCliOcrOptions(lang=["eng"], force_full_page_ocr=config.force_full_page_ocr)
