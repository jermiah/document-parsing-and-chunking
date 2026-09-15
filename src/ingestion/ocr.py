"""Selective region OCR of retained raster figures using the selected English engine."""

import subprocess
from pathlib import Path

from ingestion.config import ProcessingConfig
from ingestion.ocr_options import run_with_ocr_fallback
from ingestion.schemas import CanonicalDocument


def recognize_figures(doc: CanonicalDocument, output: Path, config: ProcessingConfig) -> None:
    reader = None
    for asset in doc.assets:
        if asset.decision == "exclude_from_retrieval":
            continue
        x0, y0, x1, y1 = asset.bbox
        labels = [
            e.text
            for e in doc.elements
            if e.page == asset.page
            and e.text
            and e.bbox
            and x0 <= e.bbox[0]
            and y0 <= e.bbox[1]
            and x1 >= e.bbox[2]
            and y1 >= e.bbox[3]
            and e.asset_id != asset.id
        ]
        if labels:
            asset.ocr_text = "\n".join(labels)
            asset.signals["label_source"] = "native_elements_inside_figure"
            continue
        attempts = []

        def recognize(selected):
            nonlocal reader
            if selected.ocr_engine == "tesseract_cli":
                result = subprocess.run(
                    ["tesseract", str(output / asset.key), "stdout", "-l", "eng"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=config.request_timeout,
                    check=True,
                )
                return result.stdout.strip()
            else:
                if reader is None:
                    from rapidocr import RapidOCR
                    from rapidocr.utils.typings import OCRVersion

                    root = config.model_dir / "RapidOcr"
                    paths = {
                        "Det.model_path": root / "PP-OCRv6_det_small.onnx",
                        "Cls.model_path": root / "ch_ppocr_mobile_v2.0_cls_mobile.onnx",
                        "Rec.model_path": root / "PP-OCRv6_rec_small.onnx",
                    }
                    if not all(p.is_file() for p in paths.values()):
                        raise RuntimeError("Provisioned RapidOCR figure models are missing")
                    reader = RapidOCR(
                        params={
                            **{k: str(v) for k, v in paths.items()},
                            "Rec.ocr_version": OCRVersion.PPOCRV6,
                            "Det.ocr_version": OCRVersion.PPOCRV6,
                            "Rec.lang_type": "en",
                            "EngineConfig.onnxruntime.intra_op_num_threads": 4,
                        }
                    )
                result = reader(str(output / asset.key))
                return "\n".join(result.txts or [])

        try:
            asset.ocr_text, engine = run_with_ocr_fallback(config, recognize, attempts)
            asset.signals["label_source"] = engine + "_crop"
            asset.signals["ocr_status"] = "complete"
        except Exception as exc:
            asset.signals["ocr_status"] = "failed"
            doc.warnings.append(f"Figure OCR failed for {asset.id}: {type(exc).__name__}")
        finally:
            asset.signals["ocr_attempts"] = attempts
