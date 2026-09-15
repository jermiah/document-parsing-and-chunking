"""Typed, fingerprinted processing configuration shared by the API and CLI."""

import hashlib
import json
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ProcessingConfig(BaseModel):
    parser: Literal["docling", "topology"] = "docling"
    pipeline_comparison: bool = False
    selected_pipelines: list[Literal["docling", "topology"]] | None = None
    topology_model: str = "gpt-5.6-terra"
    topology_max_agent_steps: int = Field(4, ge=1, le=8)
    compare_workflows: bool = False
    ocr_engine: Literal["rapidocr", "tesseract_cli"] = "rapidocr"
    ocr_fallback_engine: Literal["tesseract_cli"] | None = "tesseract_cli"
    language: Literal["en"] = "en"
    force_full_page_ocr: bool = False
    formula_enrichment: bool = False
    model_dir: Path = Path("models/docling")
    tokenizer_path: Path = Path("models/bge-m3/tokenizer.json")
    tokenizer_revision: str = "local-sha256"
    target_tokens: int = Field(450, ge=32)
    max_tokens: int = Field(700, ge=64)
    overlap_tokens: int = Field(0, ge=0, le=100)
    max_images: int = Field(2, ge=1, le=2)
    chunk_strategy: Literal["hybrid", "fixed"] = "hybrid"
    pages: list[int] | None = None
    render_dpi: int = Field(144, ge=72, le=300)
    picture_annotations_enabled: bool = False
    contextual_enrichment_enabled: bool = False
    contextual_model: str = "gpt-5.6-terra"
    contextual_reserve_tokens: int = Field(128, ge=32, le=512)
    contextual_max_document_chars: int = Field(60000, ge=1000, le=200000)
    annotation_provider: Literal["openai"] = "openai"
    annotation_model: str = "gpt-5.6-terra"
    annotation_revision: str = "local"
    prompt_version: str = "picture-v1"
    request_timeout: float = Field(120, gt=0)
    retries: int = Field(2, ge=0, le=4)

    @model_validator(mode="after")
    def budgets(self):
        if self.selected_pipelines is not None:
            if not self.selected_pipelines or len(set(self.selected_pipelines)) != len(
                self.selected_pipelines
            ):
                raise ValueError("Select at least one pipeline, without duplicates")
            self.pipeline_comparison = True
            self.compare_workflows = False
        if (
            self.contextual_enrichment_enabled
            and self.max_tokens - self.contextual_reserve_tokens < 64
        ):
            raise ValueError("Context reserve must leave at least 64 tokens for source content")
        if self.target_tokens > self.max_tokens:
            raise ValueError("target_tokens must not exceed max_tokens")
        if self.pages is not None and (not self.pages or min(self.pages) < 1):
            raise ValueError("pages must be a nonempty list of one-based page numbers")
        if self.pages:
            self.pages = sorted(set(self.pages))
        return self

    def execution_order(self) -> list[str]:
        order = ["docling", "topology"]
        return [
            name
            for name in order
            if self.selected_pipelines is None or name in self.selected_pipelines
        ]

    def fingerprint(self) -> str:
        values = self.model_dump(mode="json")
        # Version 6 retires the external OCR workflow and renames comparison mode.
        values["application_policy_version"] = "6"
        for name in ("tokenizer_path",):
            path = Path(values[name])
            values[name + "_checksum"] = (
                hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "missing"
            )
        # Model inventory is produced by the explicit download command, never by ingestion.
        inventory = self.model_dir / "inventory.json"
        values["model_inventory"] = inventory.read_text() if inventory.exists() else "unavailable"
        return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    storage_backend: Literal["local", "supabase"] = "local"
    supabase_url: str = ""
    supabase_secret_key: SecretStr = SecretStr("")
    supabase_bucket: str = "document-ingestion"
    database_url: str = "postgresql+psycopg://rag:rag@localhost:5432/rag"
    data_dir: Path = Path("data")
    artifact_dir: Path = Path("artifacts")
    report_dir: Path = Path("reports")
    config_path: Path = Path("configs/default.yaml")
    max_upload_mb: int = 150
    max_pages: int = 1000
    openai_api_key: SecretStr = SecretStr("")


def load_config(path: Path | str) -> ProcessingConfig:
    return ProcessingConfig.model_validate(yaml.safe_load(Path(path).read_text()) or {})
