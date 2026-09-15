"""Structure-first chunking with source span accounting and explicit table overflows."""

import re
from collections import Counter

from ingestion.config import ProcessingConfig
from ingestion.schemas import CanonicalDocument, Chunk, Element, stable_id
from ingestion.tokenizer import TokenBudget


def eligible_elements(doc: CanonicalDocument) -> tuple[list[Element], list[str]]:
    furniture = Counter(
        re.sub(r"\d+", "#", e.text.strip().lower())
        for e in doc.elements
        if e.kind in {"page_header", "page_footer", "header", "footer"}
    )
    excluded_assets = {a.id for a in doc.assets if a.decision == "exclude_from_retrieval"}
    eligible, excluded = [], []
    for element in doc.elements:
        normalized = re.sub(r"\d+", "#", element.text.strip().lower())
        if element.asset_id in excluded_assets:
            excluded.append(element.id)
        elif (
            element.kind in {"page_header", "page_footer", "header", "footer"}
            and furniture[normalized] >= 3
        ):
            excluded.append(element.id)
        else:
            eligible.append(element)
    return eligible, excluded


def table_units(element: Element) -> list[tuple[str, dict]]:
    cells = (element.table or {}).get("table_cells", [])
    if not cells:
        return [(element.text, {"element_id": element.id, "start": 0, "end": len(element.text)})]
    headers = [c for c in cells if c.get("column_header")]
    header = " | ".join(c["text"] for c in headers)
    rows: dict[int, list[dict]] = {}
    for index, cell in enumerate(cells):
        if not cell.get("column_header"):
            rows.setdefault(cell["start_row_offset_idx"], []).append({**cell, "cell_index": index})
    if not rows:
        return [(header, {"element_id": element.id, "cells": list(range(len(cells)))})]
    units = []
    for row, row_cells in sorted(rows.items()):
        text = (
            (element.caption + "\n" if element.caption else "")
            + header
            + "\n"
            + " | ".join(c["text"] for c in row_cells)
        )
        units.append(
            (
                text,
                {
                    "element_id": element.id,
                    "row": row,
                    "cells": [c["cell_index"] for c in row_cells],
                    "header_cells": [i for i, c in enumerate(cells) if c.get("column_header")],
                },
            )
        )
    return units


def create_chunks(
    doc: CanonicalDocument,
    config: ProcessingConfig,
    budget: TokenBudget,
    *,
    merge_narrative: bool = True,
) -> list[Chunk]:
    elements, excluded = eligible_elements(doc)
    doc.warnings.extend(f"Retrieval policy excluded: {eid}" for eid in excluded)
    by_id = {e.id: e for e in elements}
    assets = {a.id: a for a in doc.assets if a.decision != "exclude_from_retrieval"}
    annotation_links: dict[str, list[str]] = {}
    for annotation in doc.annotations:
        for eid in annotation.accepted_links:
            annotation_links.setdefault(eid, []).append(annotation.asset_id)
    groups: list[tuple[str, list[Element], list[dict]]] = []
    represented: set[str] = set()
    if config.chunk_strategy == "hybrid":
        for native in doc.native_chunks:
            source = [by_id[eid] for eid in native["element_ids"] if eid in by_id]
            # Shared policy owns tables and figures; native hierarchy owns narrative grouping.
            if (
                source
                and len(source) == len(native["element_ids"])
                and all(e.kind not in {"table", "picture", "chart"} for e in source)
            ):
                groups.append(
                    (
                        native["text"],
                        source,
                        [{"element_id": e.id, "native_chunk": True} for e in source],
                    )
                )
                represented.update(e.id for e in source)
    for element in elements:
        if element.id in represented:
            continue
        if element.asset_id and element.asset_id not in assets and not element.text:
            continue
        if element.kind == "table" and config.chunk_strategy == "hybrid":
            for text, span in table_units(element):
                groups.append((text, [element], [span]))
        else:
            text = element.text or element.caption or ("[Source image]" if element.asset_id else "")
            if text:
                groups.append(
                    (text, [element], [{"element_id": element.id, "start": 0, "end": len(text)}])
                )
    chunks: list[Chunk] = []
    for text, source, spans in groups:
        section = source[0].section
        prefix = f"{doc.filename} | {doc.year}\n" + (" > ".join(section) + "\n" if section else "")
        image_ids = list(
            dict.fromkeys(
                [e.asset_id for e in source if e.asset_id in assets]
                + [a for e in source for a in annotation_links.get(e.id, []) if a in assets]
            )
        )
        table_overflow = (
            any(e.kind == "table" for e in source) and budget.count(prefix + text) > budget.cap
        )
        fragments = [(text, 0, len(text))] if table_overflow else budget.split(text, prefix)
        for fragment, start, end in fragments:
            warnings = (
                ["table_row_overflow: retained intact for review; not sent to embedding model"]
                if table_overflow
                else []
            )
            # Image siblings share only this fragment, never the whole containing section.
            image_groups = [
                image_ids[i : i + config.max_images]
                for i in range(0, len(image_ids), config.max_images)
            ] or [[]]
            for attachments in image_groups:
                ordinal = len(chunks)
                chunks.append(
                    Chunk(
                        id=stable_id(doc.config_hash, ordinal, [e.id for e in source], fragment),
                        ordinal=ordinal,
                        text=fragment,
                        retrieval_text=prefix + fragment,
                        start_page=min(e.page for e in source),
                        end_page=max(e.page for e in source),
                        source_pages=sorted({e.page for e in source}),
                        element_ids=list(dict.fromkeys(e.id for e in source)),
                        asset_ids=attachments,
                        section=section,
                        token_count=budget.count(prefix + fragment),
                        tokenizer=budget.identity,
                        parent_id=stable_id("parent", section or [source[0].id]),
                        source_spans=[
                            {
                                **span,
                                "fragment_start": start,
                                "fragment_end": end,
                                "text_basis": by_id[span["element_id"]].provenance.get(
                                    "search_projection", "source"
                                ),
                            }
                            for span in spans
                        ],
                        warnings=warnings,
                    )
                )
    # Canonical narrative merging is only needed for Pipeline B and small fallback elements.
    merged: list[Chunk] = []
    for chunk in chunks:
        previous = merged[-1] if merged else None
        kinds = {by_id[eid].kind for eid in chunk.element_ids}
        prev_kinds = {by_id[eid].kind for eid in previous.element_ids} if previous else set()
        if (
            merge_narrative
            and config.chunk_strategy == "hybrid"
            and previous
            and not chunk.warnings
            and not previous.warnings
            and chunk.section == previous.section
            and chunk.parent_id == previous.parent_id
            and not ({"table", "picture", "chart"} & (kinds | prev_kinds))
            and len(set(previous.asset_ids + chunk.asset_ids)) <= config.max_images
            and budget.count(previous.retrieval_text + "\n" + chunk.text) <= config.target_tokens
        ):
            previous.text += "\n" + chunk.text
            previous.retrieval_text += "\n" + chunk.text
            previous.token_count = budget.count(previous.retrieval_text)
            previous.element_ids = list(dict.fromkeys(previous.element_ids + chunk.element_ids))
            previous.asset_ids = list(dict.fromkeys(previous.asset_ids + chunk.asset_ids))
            previous.source_spans.extend(chunk.source_spans)
            previous.end_page = max(previous.end_page, chunk.end_page)
            previous.source_pages = sorted(set(previous.source_pages + chunk.source_pages))
        else:
            merged.append(chunk)
    for ordinal, chunk in enumerate(merged):
        chunk.ordinal = ordinal
        chunk.id = stable_id(doc.config_hash, ordinal, chunk.text, chunk.element_ids)
    if config.overlap_tokens:
        for previous, current in zip(merged, merged[1:]):
            if previous.section != current.section or any(
                by_id[e].kind == "table" for e in previous.element_ids + current.element_ids
            ):
                continue
            sentences = re.split(r"(?<=[.!?])\s+", previous.text)
            overlap = sentences[-1]
            if (
                budget.count(overlap) <= config.overlap_tokens
                and budget.count(current.retrieval_text + "\n" + overlap) <= config.max_tokens
            ):
                current.retrieval_text += "\n[Previous sentence]\n" + overlap
                current.token_count = budget.count(current.retrieval_text)
                if current.token_count > config.max_tokens:
                    current.retrieval_text = current.retrieval_text.rsplit(
                        "\n[Previous sentence]\n", 1
                    )[0]
                    current.token_count = budget.count(current.retrieval_text)
                else:
                    current.source_spans.append({"overlap_from": previous.id})
    return merged
