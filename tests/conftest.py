"""Small deterministic software fixtures; these are not corpus quality labels."""

from pathlib import Path

import pymupdf
import pytest
import yaml
from PIL import Image, ImageDraw
from tokenizers import Tokenizer, models, pre_tokenizers, trainers

from ingestion.config import ProcessingConfig, Settings
from ingestion.schemas import Asset, CanonicalDocument, Element
from ingestion.storage import file_hash


@pytest.fixture
def setup(tmp_path):
    tokenizer = Tokenizer(models.BPE(unk_token="[UNK]"))
    tokenizer.pre_tokenizer = pre_tokenizers.ByteLevel(add_prefix_space=False)
    trainer = trainers.BpeTrainer(
        vocab_size=300,
        special_tokens=["[UNK]"],
        initial_alphabet=pre_tokenizers.ByteLevel.alphabet(),
    )
    tokenizer.train_from_iterator(
        ["Revenue in 2025 grew to 120 million euros. Table results. chart"], trainer
    )
    path = tmp_path / "tokenizer.json"
    tokenizer.save(str(path))
    config = ProcessingConfig(
        tokenizer_path=path, model_dir=tmp_path, target_tokens=100, max_tokens=160
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config.model_dump(mode="json")))
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        data_dir=tmp_path / "data",
        artifact_dir=tmp_path / "artifacts",
        report_dir=tmp_path / "reports",
        config_path=config_path,
    )
    pdf = tmp_path / "test.pdf"
    with pymupdf.open() as document:
        page = document.new_page()
        page.insert_text((50, 50), "Revenue in 2025 grew to 120 million euros.")
        document.save(pdf)
    return settings, config, pdf


class FixtureParser:
    def parse(self, pdf_path: Path, options, output, year):
        image = Image.new("RGB", (200, 150), "white")
        draw = ImageDraw.Draw(image)
        draw.rectangle((20, 20, 80, 130), fill="blue")
        draw.rectangle((90, 50, 150, 130), fill="red")
        crop = output / "assets" / "chart.png"
        crop.parent.mkdir(parents=True, exist_ok=True)
        image.save(crop)
        return CanonicalDocument(
            filename=pdf_path.name,
            checksum=file_hash(pdf_path),
            year=year,
            page_count=1,
            pages=[1],
            parser=options.parser,
            parser_version="test-fixture",
            config_hash=options.fingerprint(),
            elements=[
                Element(
                    id="e1",
                    kind="paragraph",
                    page=1,
                    order=0,
                    text="Revenue in 2025 grew to 120 million euros.",
                    section=["Results"],
                    bbox=(0.1, 0.1, 0.8, 0.2),
                ),
                Element(
                    id="e2",
                    kind="picture",
                    page=1,
                    order=1,
                    caption="Revenue comparison",
                    asset_id="a1",
                    section=["Results"],
                    bbox=(0.1, 0.3, 0.6, 0.7),
                ),
            ],
            assets=[
                Asset(
                    id="a1",
                    page=1,
                    bbox=(0.1, 0.3, 0.6, 0.7),
                    key="assets/chart.png",
                    checksum=file_hash(crop),
                    width=200,
                    height=150,
                    caption="Revenue comparison",
                )
            ],
        )
