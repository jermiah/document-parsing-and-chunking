"""Safe static document and chunk views, plus canonical reload and link validation."""

import html
import json
from pathlib import Path

import pymupdf
from bs4 import BeautifulSoup

from ingestion.schemas import CanonicalDocument, Chunk
from ingestion.storage import atomic_write, file_hash, write_json

STYLE = "body{font:16px system-ui;max-width:1150px;margin:32px auto;padding:0 24px;color:#17202a;background:#fafafa}article,details{background:white;border:1px solid #ccd4db;padding:20px;margin:18px 0}pre{white-space:pre-wrap;overflow-wrap:anywhere}img{max-width:100%;max-height:600px}table{border-collapse:collapse}td,th{border:1px solid #aaa;padding:8px}small{color:#526171}.page{position:relative;display:inline-block}.box{position:absolute;border:1px solid #d33;box-sizing:border-box}"


def safe_table(table: dict) -> str:
    """Construct markup from cell values; source-provided HTML is never executed."""
    cells = table.get("table_cells", [])
    rows: dict[int, list[dict]] = {}
    for cell in cells:
        rows.setdefault(cell["start_row_offset_idx"], []).append(cell)
    parts = ["<table>"]
    for _, row in sorted(rows.items()):
        parts.append("<tr>")
        for cell in sorted(row, key=lambda c: c["start_col_offset_idx"]):
            tag = "th" if cell.get("column_header") else "td"
            rs = max(1, min(100, int(cell.get("row_span", 1))))
            cs = max(1, min(100, int(cell.get("col_span", 1))))
            parts.append(
                f'<{tag} rowspan="{rs}" colspan="{cs}">{html.escape(cell["text"])}</{tag}>'
            )
        parts.append("</tr>")
    return "".join(parts) + "</table>"


def shell(title: str, body: str) -> str:
    return f'<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>{html.escape(title)}</title><style>{STYLE}</style><body><h1>{html.escape(title)}</h1>{body}</body></html>'


def export_bundle(
    doc: CanonicalDocument,
    chunks: list[Chunk],
    output: Path,
    pdf_path: Path,
    manifest: dict,
    debug: bool,
    scorecard: dict,
) -> dict:
    write_json(output / "canonical.json", doc.model_dump(mode="json"))
    write_json(output / "confidence.json", doc.confidence)
    atomic_write(
        output / "chunks.jsonl", ("\n".join(c.model_dump_json() for c in chunks) + "\n").encode()
    )
    asset_map = {a.id: a for a in doc.assets}
    body, markdown = [], []
    for e in doc.elements:
        body.append(
            f'<section id="e-{html.escape(e.id, quote=True)}"><small>Page {e.page} | {html.escape(e.kind)}</small>'
        )
        body.append(safe_table(e.table) if e.table else "<pre>" + html.escape(e.text) + "</pre>")
        if e.asset_id in asset_map:
            a = asset_map[e.asset_id]
            body.append(f'<img src="{a.key}" alt="{html.escape(a.caption, quote=True)}">')
        body.append("</section>")
        markdown.append(e.text)
    atomic_write(output / "document.md", "\n\n".join(markdown).encode())
    atomic_write(output / "document.html", shell(doc.filename, "".join(body)).encode())
    if debug:
        views = [
            '<p><a href="document.html">Document view</a> · <a href="manifest.json">Manifest</a></p>',
            "<details><summary>Scorecard and warnings</summary><pre>"
            + html.escape(json.dumps(scorecard, indent=2) + "\n" + "\n".join(doc.warnings))
            + "</pre></details>",
        ]
        for chunk in chunks:
            views.append(
                f'<article id="chunk-{chunk.id}"><h2>Chunk {chunk.ordinal}</h2><small>ID {chunk.id} | pages {chunk.start_page}–{chunk.end_page} | {chunk.token_count} tokens</small><pre>{html.escape(chunk.text)}</pre>'
            )
            if chunk.contextual_provenance:
                views.append(
                    "<details open><summary>Generated retrieval context — unverified</summary><pre>"
                    + html.escape(chunk.contextual_text or "Context generation failed")
                    + "</pre><pre>"
                    + html.escape(json.dumps(chunk.contextual_provenance, indent=2))
                    + "</pre></details>"
                )
            for eid in chunk.element_ids:
                e = next(e for e in doc.elements if e.id == eid)
                if e.table:
                    views.append(safe_table(e.table))
            for aid in chunk.asset_ids:
                a = asset_map[aid]
                views.append(
                    f'<figure><img src="{a.key}" alt="{html.escape(a.caption, quote=True)}"><figcaption>{html.escape(a.caption)} · {a.decision}</figcaption></figure>'
                )
                for annotation in doc.annotations:
                    if annotation.asset_id == aid:
                        views.append(
                            "<details open><summary>AI image annotation — unverified</summary><pre>"
                            + html.escape(json.dumps(annotation.model_dump(), indent=2))
                            + "</pre></details>"
                        )
            views.append("</article>")
        views.append("<details><summary>All image decisions (including exclusions)</summary>")
        for a in doc.assets:
            views.append(
                f'<p>{a.id}: {a.decision} — {html.escape(a.reason)}</p><img loading="lazy" src="{a.key}" alt="Image candidate">'
            )
        views.append(
            "</details><details><summary>Generated annotations — unverified</summary><pre>"
            + html.escape(json.dumps([a.model_dump() for a in doc.annotations], indent=2))
            + "</pre></details>"
        )
        with pymupdf.open(pdf_path) as pdf:
            for page_number in doc.pages:
                page_path = output / "pages" / f"{page_number}.png"
                atomic_write(page_path, pdf[page_number - 1].get_pixmap(dpi=72).tobytes("png"))
                views.append(
                    f'<details><summary>Source page {page_number} and detected regions</summary><div class="page"><img loading="lazy" src="pages/{page_number}.png" alt="Source page {page_number}">'
                )
                for e in doc.elements:
                    if e.page == page_number and e.bbox:
                        x0, y0, x1, y1 = e.bbox
                        views.append(
                            f'<span class="box" title="{html.escape(e.id, quote=True)}" style="left:{x0 * 100}%;top:{y0 * 100}%;width:{(x1 - x0) * 100}%;height:{(y1 - y0) * 100}%"></span>'
                        )
                views.append("</div></details>")
        atomic_write(
            output / "index.html",
            shell("Chunk inspection: " + doc.filename, "".join(views)).encode(),
        )
    restored = CanonicalDocument.model_validate_json(
        (output / "canonical.json").read_text(encoding="utf-8")
    )
    broken = []
    for path in output.glob("*.html"):
        soup = BeautifulSoup(path.read_text(encoding="utf-8"), "html.parser")
        for image in soup.find_all("img"):
            if not (output / str(image["src"])).is_file():
                broken.append(str(image["src"]))
    checks = {
        "canonical_roundtrip": restored == doc,
        "broken_images": broken,
        "chunk_export_count": len(
            (output / "chunks.jsonl").read_text(encoding="utf-8").splitlines()
        ),
        "html_browser_render": "not_run",
    }
    manifest.update(
        {
            "serialization": checks,
            "artifacts": {
                str(p.relative_to(output)): file_hash(p)
                for p in output.rglob("*")
                if p.is_file() and p.name != "manifest.json"
            },
        }
    )
    write_json(output / "manifest.json", manifest)
    return checks
