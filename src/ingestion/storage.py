"""Confined artifact storage with atomic replacement and content-addressed image crops."""

import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker


def confined(root: Path, key: str) -> Path:
    base = root.resolve()
    candidate = (base / key).resolve()
    if not candidate.is_relative_to(base):
        raise ValueError("Path is outside configured storage")
    return candidate


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temp.write_bytes(data)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def write_json(path: Path, value: Any) -> None:
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode())


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def regenerate_crop(
    pdf_path: Path,
    bbox: tuple[float, float, float, float],
    page: int,
    destination: Path,
    dpi: int = 144,
) -> None:
    """Reconstruct a source region from the durable PDF when a cached crop is missing."""
    import pymupdf

    with pymupdf.open(pdf_path) as document:
        source = document[page - 1]
        rect = pymupdf.Rect(
            bbox[0] * source.rect.width,
            bbox[1] * source.rect.height,
            bbox[2] * source.rect.width,
            bbox[3] * source.rect.height,
        )
        atomic_write(destination, source.get_pixmap(clip=rect, dpi=dpi).tobytes("png"))


def database(url: str):
    from sqlalchemy.engine import make_url
    from sqlalchemy.pool import NullPool

    parsed = make_url(url)
    if parsed.drivername in {"postgres", "postgresql"}:
        parsed = parsed.set(drivername="postgresql+psycopg")
    options: dict[str, Any] = {"pool_pre_ping": True}
    if parsed.drivername.startswith("postgresql"):
        options.update(pool_size=5, max_overflow=2)
        connect_args: dict[str, Any] = {"connect_timeout": 15}
        if parsed.host and (
            parsed.host.endswith("supabase.co") or parsed.host.endswith("supabase.com")
        ):
            connect_args["sslmode"] = parsed.query.get("sslmode", "require")
        if parsed.port == 6543:
            options = {"pool_pre_ping": True, "poolclass": NullPool}
            connect_args["prepare_threshold"] = None
        options["connect_args"] = connect_args
    engine = create_engine(parsed, **options)
    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def enforce_fks(connection, record):
            connection.execute("PRAGMA foreign_keys=ON")

    return engine, sessionmaker(engine, expire_on_commit=False)
