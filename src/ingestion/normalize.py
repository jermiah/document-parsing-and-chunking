"""Map native Docling references, tables and source geometry without flattening them."""

import hashlib
import io
from pathlib import Path

from ingestion.schemas import Asset, CanonicalDocument, Element, stable_id
from ingestion.storage import atomic_write


def normalize(native, doc: CanonicalDocument, output: Path, page_map: dict[int, int]) -> None:
    from docling_core.types.doc import CoordOrigin, PictureItem, TableItem

    section: list[str] = []
    for order, (item, depth) in enumerate(native.iterate_items()):
        if not getattr(item, "prov", None):
            continue
        for occurrence, prov in enumerate(item.prov):
            page = native.pages[prov.page_no]
            box = prov.bbox
            if box.coord_origin == CoordOrigin.BOTTOMLEFT:
                box = box.to_top_left_origin(page_height=page.size.height)
            bbox = tuple(
                max(0.0, min(1.0, v))
                for v in (
                    box.l / page.size.width,
                    box.t / page.size.height,
                    box.r / page.size.width,
                    box.b / page.size.height,
                )
            )
            kind = str(item.label.value)
            text = getattr(item, "text", "")
            if kind in {"section_header", "title"}:
                level = max(1, getattr(item, "level", 1))
                section = section[: level - 1] + [text]
                kind = "heading"
            table = None
            caption = item.caption_text(native) if hasattr(item, "caption_text") else ""
            if isinstance(item, TableItem):
                table = item.data.model_dump(mode="json")
                table["html"] = item.export_to_html(doc=native)
                table["markdown"] = item.export_to_markdown(doc=native)
                text = table["markdown"]
                kind = "table"
            element_id = item.self_ref if occurrence == 0 else f"{item.self_ref}@{occurrence}"
            element = Element(
                id=element_id,
                kind=kind,
                page=page_map[prov.page_no],
                bbox=bbox,
                order=order * 100 + occurrence,
                text=text,
                caption=caption,
                table=table,
                section=list(section),
                provenance={
                    "native_ref": item.self_ref,
                    "native_provenance": prov.model_dump(mode="json"),
                    "page_size": page.size.model_dump(mode="json"),
                    "depth": depth,
                },
            )
            if isinstance(item, PictureItem):
                crop = item.get_image(native)
                if crop is not None:
                    buffer = io.BytesIO()
                    crop.convert("RGB").save(buffer, format="PNG")
                    checksum = hashlib.sha256(buffer.getvalue()).hexdigest()
                    key = f"assets/{checksum}.png"
                    atomic_write(output / key, buffer.getvalue())
                    asset_id = stable_id(element_id, element.page)
                    doc.assets.append(
                        Asset(
                            id=asset_id,
                            page=element.page,
                            bbox=bbox,
                            key=key,
                            checksum=checksum,
                            width=crop.width,
                            height=crop.height,
                            caption=caption,
                        )
                    )
                    element.asset_id = asset_id
                else:
                    doc.warnings.append(f"Missing picture crop: {element_id}")
            doc.elements.append(element)
