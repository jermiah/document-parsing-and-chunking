"""Selected-workflow inspection followed by measured pipeline differences."""

import html
import json
import os
from statistics import median
from urllib.parse import quote

from ingestion.export import shell
from ingestion.pipelines.registry import LABELS, ORDER
from ingestion.storage import atomic_write, write_json


def render_report(batches, output):
    """Read saved results only; never reparse or rechunk for the comparison."""
    order = [name for name in ORDER if name in batches]
    if not order:
        raise ValueError("No selected pipeline results")
    metrics, columns = {}, []
    for name in order:
        records = batches.get(name, [])
        chunks, docs = [], []
        body = [f"<article><h2>{html.escape(LABELS[name])}</h2>"]
        for path, elapsed in records:
            doc = json.loads((path / "canonical.json").read_text("utf-8"))
            docs.append(doc)
            batch_chunks = [
                json.loads(line)
                for line in (path / "chunks.jsonl").read_text("utf-8").splitlines()
                if line.strip()
            ]
            chunks.extend(batch_chunks)
            link = quote(os.path.relpath(path / "index.html", output).replace("\\", "/"), safe="/.")
            body.append(
                f"<h3>{html.escape(doc['filename'])} Â· pages {doc['pages']}</h3>"
                f'<a href="{link}">Source, images and chunk inspector</a>'
            )
            for c in batch_chunks:
                body.append(
                    f"<details><summary>Chunk {c['ordinal'] + 1} Â· "
                    f"{c['token_count']} tokens Â· pages {c['source_pages']}</summary>"
                    f"<pre>{html.escape(c['retrieval_text'])}</pre>"
                    f"<pre>{html.escape(json.dumps(c.get('topology_provenance', {}), ensure_ascii=False))}</pre>"
                    "</details>"
                )
        tokens = [c["token_count"] for c in chunks]
        metrics[name] = {
            "batches": len(records),
            "pages": sum(len(d["pages"]) for d in docs),
            "failed_pages": sum(len(d["failures"]) for d in docs),
            "chunks": len(chunks),
            "median_tokens": median(tokens) if tokens else None,
            "max_tokens": max(tokens) if tokens else None,
            "elements": sum(len(d["elements"]) for d in docs),
            "represented_elements": sum(
                len(
                    {
                        eid
                        for c in chunks
                        for eid in c["element_ids"]
                        if c.get("start_page") in d["pages"]
                    }
                )
                for d in docs
            ),
            "images": sum(len(d["assets"]) for d in docs),
            "excluded_images": sum(
                a["decision"] == "exclude_from_retrieval" for d in docs for a in d["assets"]
            ),
            "refiner_failures": sum(
                c.get("topology_provenance", {}).get("status") == "failed" for c in chunks
            ),
            "context_enriched": sum(bool(c.get("contextual_text")) for c in chunks),
            "context_failures": sum(
                c.get("contextual_provenance", {}).get("status") == "failed" for c in chunks
            ),
            "chunks_with_warnings": sum(bool(c.get("warnings")) for c in chunks),
            "elapsed_seconds": round(sum(t for _, t in records), 2),
            "retrieval_accuracy": "not evaluated: verified questions required",
        }
        body.append("</article>")
        columns.append("".join(body))
    table = (
        "<h2>Measured comparison</h2><table><thead><tr><th>Metric</th>"
        + "".join(f"<th>{html.escape(LABELS[k])}</th>" for k in order)
        + "</tr></thead><tbody>"
    )
    for metric in metrics[order[0]]:
        table += (
            "<tr><th>"
            + html.escape(metric.replace("_", " "))
            + "</th>"
            + "".join("<td>" + html.escape(str(metrics[k][metric])) + "</td>" for k in order)
            + "</tr>"
        )
    table += "</tbody></table><p>Counts measure output differences, not factual accuracy. "
    table += "Extraction differs between pipelines; chunk-count differences do not identify a winner.</p>"
    page = "<h1>Selected ingestion pipelines</h1><p>Sequential execution; independently saved results.</p>"
    page += '<div class="pipelines">' + "".join(columns) + "</div>" + table
    page += "<style>.pipelines{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:20px}"
    page += "pre{white-space:pre-wrap;overflow-wrap:anywhere}td,th{padding:8px;text-align:left}"
    page += "@media(max-width:900px){.pipelines{grid-template-columns:1fr}}</style>"
    write_json(output / "metrics.json", metrics)
    atomic_write(output / "index.html", shell("Selected ingestion pipelines", page).encode())
    return metrics
