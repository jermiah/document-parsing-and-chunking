"""Canonical records use one-based pages and top-left normalized bounding boxes."""

import hashlib
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field, field_validator

from ingestion.config import ProcessingConfig


def stable_id(*parts: object) -> str:
    return hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:32]


class Element(BaseModel):
    id: str
    kind: str
    page: int = Field(ge=1)
    bbox: tuple[float, float, float, float] | None = None
    order: int
    text: str = ""
    caption: str = ""
    section: list[str] = Field(default_factory=list)
    table: dict[str, Any] | None = None
    asset_id: str | None = None
    provenance: dict[str, Any] = Field(default_factory=dict)

    @field_validator("bbox")
    @classmethod
    def valid_box(cls, v):
        if v is not None and not (0 <= v[0] <= v[2] <= 1 and 0 <= v[1] <= v[3] <= 1):
            raise ValueError("bbox must be ordered, normalized, top-left coordinates")
        return v


class Asset(BaseModel):
    id: str
    page: int
    bbox: tuple[float, float, float, float]
    key: str
    checksum: str
    width: int
    height: int
    caption: str = ""
    ocr_text: str = ""
    decision: Literal["keep", "exclude_from_retrieval", "review_required"] = "review_required"
    signals: dict[str, Any] = Field(default_factory=dict)
    reason: str = "Not yet reviewed"


class Annotation(BaseModel):
    asset_id: str
    description: str = Field(max_length=3000)
    picture_type: str = Field(max_length=100)
    content_role: Literal["substantive", "decorative", "mixed", "uncertain"] = "uncertain"
    contains_substantive_information: bool | None = None
    recommended_action: Literal["keep", "exclude_from_retrieval", "review_required"] = (
        "review_required"
    )
    reason: str = "Legacy annotation has no retention classification"
    labels: list[str] = Field(default_factory=list, max_length=100)
    axes: list[str] = Field(default_factory=list, max_length=20)
    units: list[str] = Field(default_factory=list, max_length=20)
    relationships: list[str] = Field(default_factory=list, max_length=30)
    suggested_links: dict[str, str] = Field(default_factory=dict)
    accepted_links: list[str] = Field(default_factory=list)
    uncertainty: list[str] = Field(default_factory=list)
    provenance: dict[str, Any] = Field(default_factory=dict)
    status: str = "generated_unverified"


class Chunk(BaseModel):
    pipeline: str = "docling"
    id: str
    ordinal: int
    text: str
    retrieval_text: str
    contextual_text: str = ""
    contextual_provenance: dict[str, Any] = Field(default_factory=dict)
    topology_provenance: dict[str, Any] = Field(default_factory=dict)
    start_page: int
    end_page: int
    source_pages: list[int] = Field(default_factory=list)
    element_ids: list[str]
    asset_ids: list[str] = Field(default_factory=list)
    section: list[str] = Field(default_factory=list)
    token_count: int
    tokenizer: str
    parent_id: str | None = None
    source_spans: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class CanonicalDocument(BaseModel):
    filename: str
    checksum: str
    year: int
    page_count: int
    parser: str
    parser_version: str
    config_hash: str
    pages: list[int]
    elements: list[Element] = Field(default_factory=list)
    assets: list[Asset] = Field(default_factory=list)
    annotations: list[Annotation] = Field(default_factory=list)
    failures: dict[int, str] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    confidence: dict[str, Any] = Field(default_factory=dict)
    native_chunks: list[dict[str, Any]] = Field(default_factory=list)


class DocumentParser(Protocol):
    def parse(
        self, pdf_path: Path, options: ProcessingConfig, output: Path, year: int
    ) -> CanonicalDocument:
        """Write native output and return independently scoped canonical extraction."""
        raise NotImplementedError
