"""Small command-line interface for setup and PDF ingestion."""

import argparse
import json
import subprocess
import sys
from pathlib import Path

from ingestion.config import Settings, load_config
from ingestion.ingestion import IngestionService
from ingestion.storage import database
from ingestion.vision import annotation_client


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    ingest = commands.add_parser("ingest")
    ingest.add_argument("pdf", type=Path)
    ingest.add_argument("--year", type=int, required=True)
    ingest.add_argument("--pages", type=int, nargs="+")
    ingest.add_argument("--debug", action="store_true")
    ingest.add_argument("--force", action="store_true")
    ingest.add_argument("--config", type=Path)
    prepare = commands.add_parser("prepare-document", help="Convert and enrich without chunking")
    prepare.add_argument("pdf", type=Path)
    prepare.add_argument("--year", type=int, required=True)
    prepare.add_argument("--pages", type=int, nargs="+")
    prepare.add_argument("--config", type=Path)
    prepare.add_argument("--output", type=Path, required=True)
    compare = commands.add_parser(
        "compare-chunks", help="Compare saved bundles with optional OpenAI chunk context"
    )
    source = compare.add_mutually_exclusive_group(required=True)
    source.add_argument("--bundles", type=Path, nargs="+")
    source.add_argument("--source-root", type=Path)
    compare.add_argument("--filename", help="Filter a source root by original document filename")
    compare.add_argument("--config", type=Path)
    compare.add_argument("--output", type=Path, required=True)
    compare.add_argument(
        "--gold", type=Path, help="Verified question/evidence JSON for BM25 evaluation"
    )
    commands.add_parser("download-models")
    commands.add_parser("migrate")
    commands.add_parser("check")
    args = parser.parse_args()
    settings = Settings()
    if args.command == "download-models":
        subprocess.run([sys.executable, "scripts/download_models.py"], check=True)
        return
    if args.command == "migrate":
        from alembic import command
        from alembic.config import Config

        command.upgrade(Config("alembic.ini"), "head")
        return
    config = load_config(getattr(args, "config", None) or settings.config_path)
    if args.command == "prepare-document":
        from ingestion.document_bundle import prepare_document

        if args.pages:
            config = config.model_copy(update={"pages": args.pages})
        result = prepare_document(args.pdf, args.year, config, settings, args.output)
        print(
            json.dumps(
                {
                    "output": str(args.output),
                    "pages": result.pages,
                    "annotations": len(result.annotations),
                    "failures": result.failures,
                }
            )
        )
        return
    if args.command == "compare-chunks":
        from ingestion.chunk_comparison import compare_bundles

        paths = args.bundles or [p.parent for p in args.source_root.rglob("canonical.json")]
        if args.filename:
            paths = [
                p
                for p in paths
                if json.loads((p / "canonical.json").read_text("utf-8"))["filename"]
                == args.filename
            ]
        paths = sorted(
            paths, key=lambda p: min(json.loads((p / "canonical.json").read_text("utf-8"))["pages"])
        )
        gold = json.loads(args.gold.read_text("utf-8")) if args.gold else None
        result = compare_bundles(
            paths,
            config,
            args.output,
            gold,
            context_client=annotation_client(settings, config)
            if config.contextual_enrichment_enabled
            else None,
            context_cache=settings.data_dir / "context_cache",
        )
        print(
            json.dumps(
                {"html": str(args.output / "index.html"), "metrics": result["metrics"]}, indent=2
            )
        )
        return
    if args.command == "check":
        if (
            not config.tokenizer_path.is_file()
            or not (config.model_dir / "inventory.json").is_file()
        ):
            raise RuntimeError("Run doc-ingest download-models first")
        if config.picture_annotations_enabled or config.contextual_enrichment_enabled:
            models = annotation_client(settings, config).preflight()
            required = set()
            if config.picture_annotations_enabled:
                required.add(config.annotation_model)
            if config.contextual_enrichment_enabled:
                required.add(config.contextual_model)
            if not required <= {m["id"] for m in models.get("data", [])}:
                raise RuntimeError("A configured OpenAI model is unavailable to this API key")
        print("Parser assets and configured annotation service are available.")
        return
    if args.pages:
        config = config.model_copy(update={"pages": args.pages})
    engine, sessions = database(settings.database_url)
    try:
        result = IngestionService(settings, sessions).ingest(
            args.pdf, args.year, config, args.debug, args.force
        )
        print(json.dumps(result, indent=2))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
