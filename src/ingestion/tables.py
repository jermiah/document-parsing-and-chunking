"""Relational evidence graph. Composite foreign keys prohibit cross-run associations."""

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class DocumentRow(Base):
    __tablename__ = "documents"
    __table_args__ = (UniqueConstraint("checksum", "year"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    filename: Mapped[str] = mapped_column(Text)
    checksum: Mapped[str] = mapped_column(String(64))
    year: Mapped[int] = mapped_column(Integer, index=True)
    page_count: Mapped[int] = mapped_column(Integer)
    original_key: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class RunRow(Base):
    __tablename__ = "ingestion_runs"
    __table_args__ = (Index("ix_run_reuse", "document_id", "config_hash", "status"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), index=True)
    parser: Mapped[str] = mapped_column(String(30))
    parser_version: Mapped[str] = mapped_column(String(80), default="pending")
    config_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(30), default="processing")
    active: Mapped[bool] = mapped_column(Boolean, default=False)
    annotation_status: Mapped[str] = mapped_column(String(30), default="disabled")
    indexing_status: Mapped[str] = mapped_column(String(30), default="pending")
    artifact_key: Mapped[str] = mapped_column(Text)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    errors: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ElementRow(Base):
    __tablename__ = "elements"
    run_id: Mapped[str] = mapped_column(ForeignKey("ingestion_runs.id"), primary_key=True)
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    page: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class ContextRow(Base):
    __tablename__ = "context_groups"
    run_id: Mapped[str] = mapped_column(ForeignKey("ingestion_runs.id"), primary_key=True)
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class ChunkRow(Base):
    __tablename__ = "chunks"
    __table_args__ = (
        UniqueConstraint("run_id", "ordinal"),
        ForeignKeyConstraint(
            ["run_id", "parent_id"], ["context_groups.run_id", "context_groups.id"]
        ),
        Index("ix_chunk_parent", "run_id", "parent_id"),
        Index("ix_chunk_pages", "run_id", "start_page", "end_page"),
    )
    run_id: Mapped[str] = mapped_column(ForeignKey("ingestion_runs.id"), primary_key=True)
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    retrieval_text: Mapped[str] = mapped_column(Text)
    start_page: Mapped[int] = mapped_column(Integer)
    end_page: Mapped[int] = mapped_column(Integer)
    parent_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class AssetRow(Base):
    __tablename__ = "assets"
    run_id: Mapped[str] = mapped_column(ForeignKey("ingestion_runs.id"), primary_key=True)
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    page: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class ChunkElementRow(Base):
    __tablename__ = "chunk_elements"
    __table_args__ = (
        ForeignKeyConstraint(["run_id", "chunk_id"], ["chunks.run_id", "chunks.id"]),
        ForeignKeyConstraint(["run_id", "element_id"], ["elements.run_id", "elements.id"]),
        Index("ix_chunk_element_source", "run_id", "element_id"),
    )
    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    chunk_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    element_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    position: Mapped[int] = mapped_column(Integer)


class ChunkAssetRow(Base):
    __tablename__ = "chunk_assets"
    __table_args__ = (
        ForeignKeyConstraint(["run_id", "chunk_id"], ["chunks.run_id", "chunks.id"]),
        ForeignKeyConstraint(["run_id", "asset_id"], ["assets.run_id", "assets.id"]),
        Index("ix_chunk_asset_source", "run_id", "asset_id"),
    )
    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    chunk_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    asset_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    position: Mapped[int] = mapped_column(Integer)


class AnnotationRow(Base):
    __tablename__ = "asset_annotations"
    __table_args__ = (ForeignKeyConstraint(["run_id", "asset_id"], ["assets.run_id", "assets.id"]),)
    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    asset_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    version: Mapped[str] = mapped_column(String(64), primary_key=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class EvaluationRow(Base):
    __tablename__ = "evaluation_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    dataset_version: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
