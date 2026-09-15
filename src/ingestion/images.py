"""Collect visual signals; only validated contextual classification can exclude assets."""

from collections import Counter
from pathlib import Path

from PIL import Image

from ingestion.schemas import CanonicalDocument


def filter_assets(
    doc: CanonicalDocument,
    output: Path,
) -> None:
    frequencies = Counter(a.checksum for a in doc.assets)
    for asset in doc.assets:
        x0, y0, x1, y1 = asset.bbox
        area, margin = (x1 - x0) * (y1 - y0), y0 > 0.88 or y1 < 0.12
        context = bool(asset.caption.strip() or asset.ocr_text.strip())
        with Image.open(output / asset.key) as image:
            entropy = image.convert("L").entropy()
            small = image.convert("L").resize((8, 8))
            import numpy as np

            values = np.asarray(small).ravel().tolist()
            average = sum(values) / len(values)
            perceptual = hex(sum(int(v >= average) << i for i, v in enumerate(values)))
        protected = context or any(
            e.kind in {"chart", "table"}
            and e.page == asset.page
            and e.bbox
            and e.bbox[0] <= x0
            and e.bbox[1] <= y0
            and e.bbox[2] >= x1
            and e.bbox[3] >= y1
            for e in doc.elements
        )
        asset.signals.update(
            {
                "rule_version": "image-vision-v2",
                "area": area,
                "margin": margin,
                "repetitions": frequencies[asset.checksum],
                "entropy": entropy,
                "perceptual_hash": perceptual,
                "protected_context": protected,
            }
        )
        asset.decision = "review_required"
        asset.reason = "Awaiting vision classification; geometry alone does not exclude images"


def apply_image_decision(doc, asset, annotation):
    """Classify the whole region, retaining contradictory or uncertain evidence."""
    chart_or_table = any(
        e.asset_id == asset.id and e.kind in {"chart", "table"} for e in doc.elements
    )
    if (
        annotation.contains_substantive_information is True
        or annotation.content_role in {"substantive", "mixed"}
        or chart_or_table
    ):
        asset.decision = "keep"
        asset.reason = "Substantive or mixed content is protected. " + annotation.reason
    elif (
        annotation.content_role == "decorative"
        and annotation.contains_substantive_information is False
        and annotation.recommended_action == "exclude_from_retrieval"
        and not annotation.uncertainty
    ):
        asset.decision = "exclude_from_retrieval"
        asset.reason = annotation.reason
    else:
        asset.decision = "review_required"
        asset.reason = (
            "Uncertain or inconsistent classification; source retained. " + annotation.reason
        )
    asset.signals["vision_classification"] = {
        "content_role": annotation.content_role,
        "contains_substantive_information": annotation.contains_substantive_information,
        "recommended_action": annotation.recommended_action,
        "uncertainty": annotation.uncertainty,
        "provenance": annotation.provenance,
    }


def filter_image_content(doc: CanonicalDocument) -> CanonicalDocument:
    """Remove excluded image content from chunk input using recorded decisions only.

    No model call is made here. Originals and classification records stay in the
    canonical document for inspection; only the retrieval copy is filtered.
    """
    projected = doc.model_copy(deep=True)
    excluded = {a.id for a in projected.assets if a.decision == "exclude_from_retrieval"}
    removed_elements = {e.id for e in projected.elements if e.asset_id in excluded}
    projected.elements = [e for e in projected.elements if e.id not in removed_elements]
    projected.assets = [a for a in projected.assets if a.id not in excluded]
    projected.annotations = [a for a in projected.annotations if a.asset_id not in excluded]
    for annotation in projected.annotations:
        annotation.accepted_links = [
            eid for eid in annotation.accepted_links if eid not in removed_elements
        ]
    # A native group can contain preassembled image text. Discard that grouping;
    # the chunker will rebuild its remaining narrative from the retained elements.
    projected.native_chunks = [
        group
        for group in projected.native_chunks
        if not removed_elements.intersection(group["element_ids"])
    ]
    projected.warnings.extend(
        f"Retrieval policy excluded: {eid}" for eid in sorted(removed_elements)
    )
    return projected
