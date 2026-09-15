"""Group source elements by section and reading order, keeping explicit image links."""

from ingestion.chunking import create_chunks, eligible_elements
from ingestion.config import ProcessingConfig
from ingestion.images import filter_image_content
from ingestion.schemas import CanonicalDocument, Chunk, stable_id
from ingestion.tokenizer import TokenBudget


def enrich_retrieval(doc: CanonicalDocument) -> CanonicalDocument:
    """Create a search-only projection; preserve the original canonical source."""
    projected = filter_image_content(doc)
    assets = {a.id: a for a in projected.assets if a.decision != "exclude_from_retrieval"}
    annotations = {a.asset_id: a for a in projected.annotations}
    for element in projected.elements:
        asset = assets.get(element.asset_id)
        if asset is None:
            continue
        parts = [element.text, element.caption or asset.caption]
        if asset.ocr_text:
            parts.append("[Figure OCR]\n" + asset.ocr_text)
        annotation = annotations.get(asset.id)
        if annotation:
            parts.append("[AI image annotation — generated, unverified]\n" + annotation.description)
            for key in ("labels", "axes", "units", "relationships", "uncertainty"):
                values = getattr(annotation, key)
                if values:
                    parts.append(key.title() + ": " + "; ".join(values))
        element.text = "\n".join(dict.fromkeys(p for p in parts if p))
        element.provenance["search_projection"] = "source + figure OCR + unverified annotation"
    return projected


def topology_chunks(
    doc: CanonicalDocument, config: ProcessingConfig, budget: TokenBudget
) -> tuple[list[Chunk], dict]:
    """Pack consecutive evidence units within real section occurrences.

    Reading-order/section edges control packing. Figure links remain explicit for
    later expansion even when a token limit prevents co-location. No cross-reference
    inference or LLM hierarchy reconstruction is claimed.
    """
    working = doc.model_copy(deep=True)
    working.native_chunks = []
    eligible, _ = eligible_elements(working)
    ordered = sorted(eligible, key=lambda e: e.order)
    working.elements = ordered
    parents, edges, membership = [], [], {}
    previous = None
    current_parent = None
    for element in ordered:
        if previous is None or element.section != previous.section or element.kind == "heading":
            current_parent = stable_id(doc.checksum, doc.config_hash, "section", element.id)
            parents.append({"id": current_parent, "section": element.section, "element_ids": []})
        membership[element.id] = current_parent
        parents[-1]["element_ids"].append(element.id)
        edges.append({"source": current_parent, "target": element.id, "kind": "contains"})
        if previous:
            edges.append({"source": previous.id, "target": element.id, "kind": "reading_order"})
        if element.asset_id:
            edges.append({"source": element.id, "target": element.asset_id, "kind": "figure"})
        previous = element
    for annotation in doc.annotations:
        if not any(
            a.id == annotation.asset_id and a.decision != "exclude_from_retrieval"
            for a in doc.assets
        ):
            continue
        for eid in annotation.accepted_links:
            if eid in membership:
                edges.append(
                    {
                        "source": eid,
                        "target": annotation.asset_id,
                        "kind": "annotation_link_unverified",
                    }
                )
    # Reuse the existing table-row and overflow policy to keep the comparison fair.
    units = create_chunks(
        working,
        config.model_copy(
            update={
                "chunk_strategy": "hybrid",
                "target_tokens": 32,
                "overlap_tokens": 0,
            }
        ),
        budget,
        merge_narrative=False,
    )
    result: list[Chunk] = []
    for unit in units:
        unit.parent_id = membership[unit.element_ids[0]]
        last = result[-1] if result else None
        combined = last.retrieval_text + "\n" + unit.text if last else ""
        if (
            last
            and last.parent_id == unit.parent_id
            and not last.warnings
            and not unit.warnings
            and len(set(last.asset_ids + unit.asset_ids)) <= config.max_images
            and budget.count(combined) <= config.target_tokens
        ):
            last.text += "\n" + unit.text
            last.retrieval_text = combined
            last.token_count = budget.count(combined)
            last.element_ids = list(dict.fromkeys(last.element_ids + unit.element_ids))
            last.asset_ids = list(dict.fromkeys(last.asset_ids + unit.asset_ids))
            last.source_spans.extend(unit.source_spans)
            last.source_pages = sorted(set(last.source_pages + unit.source_pages))
            last.start_page, last.end_page = min(last.source_pages), max(last.source_pages)
        else:
            result.append(unit)
    for ordinal, chunk in enumerate(result):
        chunk.ordinal = ordinal
        chunk.id = stable_id(doc.config_hash, "topology-v1", ordinal, chunk.retrieval_text)
    return result, {"strategy": "deterministic-topology-v1", "parents": parents, "edges": edges}
