"""Explicit model provisioning, separate from offline ingestion and notebook execution."""

import argparse
import hashlib
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("models"))
    args = parser.parse_args()
    from docling.utils.model_downloader import download_models
    from huggingface_hub import hf_hub_download

    root = args.root
    download_models(
        output_dir=root / "docling",
        with_code_formula=False,
        with_picture_classifier=False,
        with_rapidocr=True,
        rapidocr_models=["onnxruntime:en"],
    )
    hf_hub_download("BAAI/bge-m3", "tokenizer.json", local_dir=root / "bge-m3")
    inventory = {}
    for path in sorted((root / "docling").rglob("*")):
        if path.is_file() and path.name != "inventory.json" and ".cache" not in path.parts:
            with path.open("rb") as stream:
                inventory[str(path.relative_to(root / "docling"))] = hashlib.file_digest(
                    stream, "sha256"
                ).hexdigest()
    (root / "docling" / "inventory.json").write_text(json.dumps(inventory, indent=2))


if __name__ == "__main__":
    main()
