"""Offline comparison over identical saved extraction; no OCR, LLM or database writes."""

import html
import json
import math
import os
import re
import time
from collections import Counter
from pathlib import Path
from statistics import median
from urllib.parse import quote

from ingestion.checks import scorecard
from ingestion.chunking import create_chunks
from ingestion.config import ProcessingConfig
from ingestion.contextual import enrich_chunks, source_budget
from ingestion.document_bundle import load_bundle
from ingestion.export import safe_table, shell
from ingestion.storage import atomic_write, write_json
from ingestion.topology import enrich_retrieval, topology_chunks

LABELS = {
    "enriched_hybrid": "Hybrid + image context",
    "topology": "Topology-aware + image context",
}


def retrieval_evaluation(
    rows: list[dict], gold: list[dict] | None, cap: int, context_tokens: int = 3000, top_k: int = 5
) -> dict:
    """Optional reproducible lexical baseline, never presented as embedding accuracy."""
    if not gold:
        return {"status": "not_run", "reason": "No independently verified question/evidence set"}
    usable = [r for r in rows if r["chunk"].token_count <= cap]
    documents = [Counter(re.findall(r"\w+", r["chunk"].retrieval_text.lower())) for r in usable]
    frequency = Counter(term for d in documents for term in d)
    average = sum(sum(d.values()) for d in documents) / max(len(documents), 1)
    details = []
    for item in gold:
        if not item.get("verified") or not item.get("question") or not item.get("evidence"):
            raise ValueError("Every gold question needs verified=true and nonempty evidence")
        terms = set(re.findall(r"\w+", item["question"].lower()))
        scores = []
        for index, counts in enumerate(documents):
            score = 0.0
            for term in terms:
                tf = counts[term]
                if tf:
                    idf = math.log(
                        1 + (len(documents) - frequency[term] + 0.5) / (frequency[term] + 0.5)
                    )
                    score += (
                        idf * tf * 2.5 / (tf + 1.5 * (0.25 + 0.75 * sum(counts.values()) / average))
                    )
            scores.append((score, index))
        selected: list[dict] = []
        spent = 0
        for score, index in sorted(scores, key=lambda pair: (-pair[0], pair[1])):
            if score <= 0 or len(selected) >= top_k:
                break
            row = usable[index]
            if spent + row["chunk"].token_count <= context_tokens:
                selected.append(row)
                spent += row["chunk"].token_count
        matches = []
        for evidence in item["evidence"]:
            matches.append(
                next(
                    (
                        rank
                        for rank, row in enumerate(selected, 1)
                        if row["bundle"] == evidence["bundle"]
                        and evidence["element_id"] in row["chunk"].element_ids
                        and evidence["quote"] in row["chunk"].retrieval_text
                    ),
                    None,
                )
            )
        ranks = [r for r in matches if r is not None]
        details.append(
            {
                "question": item["question"],
                "evidence_recall": len(ranks) / len(matches),
                "hit": bool(ranks),
                "reciprocal_rank": 1 / min(ranks) if ranks else 0,
                "context_tokens": spent,
                "chunks": [r["bundle"] + ":" + r["chunk"].id for r in selected],
            }
        )
    return {
        "status": "measured",
        "method": "BM25 lexical baseline; not embedding or answer accuracy",
        "top_k": top_k,
        "context_token_limit": context_tokens,
        "questions": len(details),
        "hit_rate": sum(d["hit"] for d in details) / len(details),
        "evidence_recall": sum(d["evidence_recall"] for d in details) / len(details),
        "mrr": sum(d["reciprocal_rank"] for d in details) / len(details),
        "details": details,
    }


def _validate_gold(gold: list[dict] | None, sources: dict) -> None:
    for question in gold or []:
        if (
            not question.get("verified")
            or not question.get("question")
            or not question.get("evidence")
        ):
            raise ValueError("Gold labels must have verified questions with evidence")
        for evidence in question["evidence"]:
            if not evidence.get("quote", "").strip():
                raise ValueError("Gold evidence requires a nonempty exact source quote")
            key = (evidence.get("bundle"), evidence.get("element_id"))
            if key not in sources or evidence["quote"] not in sources[key]:
                raise ValueError(f"Gold evidence is not present in canonical source: {key}")


def compare_bundles(
    paths: list[Path],
    config: ProcessingConfig,
    output: Path,
    gold: list[dict] | None = None,
    *,
    progress=None,
    context_client=None,
    context_cache: Path | None = None,
) -> dict:
    if config.contextual_enrichment_enabled and context_client is None:
        raise ValueError("Provide an OpenAI client for contextual enrichment")
    if not paths:
        raise ValueError("At least one saved bundle is required")
    paths = [p.resolve() for p in paths]
    if len({p.name for p in paths}) != len(paths):
        raise ValueError("Bundle directory names must be unique")
    if output.exists() and any(output.iterdir()):
        raise ValueError("Use an empty comparison output directory")
    output.mkdir(parents=True, exist_ok=True)
    budget = source_budget(config)
    rows: dict[str, list[dict]] = {key: [] for key in LABELS}
    checks: dict[str, list[dict]] = {key: [] for key in LABELS}
    timings = dict.fromkeys(LABELS, 0.0)
    documents, sources, graphs = [], {}, {}
    prepared = []
    seen_pages: set[tuple[str, int, int]] = set()
    for path in paths:
        doc = load_bundle(path, config)
        manifest = json.loads((path / "manifest.json").read_text("utf-8"))
        original_config = manifest.get("config", {})
        if (
            doc.native_chunks
            and original_config.get("target_tokens", config.target_tokens) != config.target_tokens
        ):
            raise ValueError("Cached native chunks use a different target; regenerate them first")
        overlap = {(doc.checksum, doc.year, p) for p in doc.pages} & seen_pages
        if overlap:
            raise ValueError("Selected bundles overlap in source pages; select one run per page")
        seen_pages.update((doc.checksum, doc.year, p) for p in doc.pages)
        sources.update({(path.name, e.id): e.text for e in doc.elements})
        projected = enrich_retrieval(doc)
        prepared.append((path, doc, projected))
        documents.append(
            {
                "bundle": path.name,
                "filename": doc.filename,
                "year": doc.year,
                "topics": list(
                    dict.fromkeys(heading for e in doc.elements for heading in e.section)
                )[:12],
                "checksum": doc.checksum,
                "pages": doc.pages,
                "total_document_pages": doc.page_count,
                "elements": len(doc.elements),
                "assets": len(doc.assets),
                "retained_assets": sum(a.decision != "exclude_from_retrieval" for a in doc.assets),
                "annotations": len(doc.annotations),
                "failures": doc.failures,
                "warnings": doc.warnings,
                "native_document": str(path / "raw/docling_document.json"),
            }
        )
        # Native schema stays intact; the prepared projection and linked sidecar are inspectable.
        write_json(
            output / "documents" / path.name / "enriched_document.json",
            {
                "format": "application-document-bundle-v1",
                "native_document": os.path.relpath(
                    path / "raw/docling_document.json", output / "documents" / path.name
                ).replace("\\", "/"),
                "asset_root": os.path.relpath(path, output / "documents" / path.name).replace(
                    "\\", "/"
                ),
                "source": doc.model_copy(update={"native_chunks": []}).model_dump(mode="json"),
                "search_projection": projected.model_copy(update={"native_chunks": []}).model_dump(
                    mode="json"
                ),
                "note": "Linked native Docling document + application enrichment, not native Docling JSON.",
            },
        )
        source_output = output / "documents" / path.name / "source.html"
        source_body = []
        assets = {a.id: a for a in doc.assets}
        for element in doc.elements:
            source_body.append(
                f'<section id="e-{html.escape(element.id, quote=True)}"><h2>Page {element.page}</h2>'
                + (
                    safe_table(element.table)
                    if element.table
                    else "<pre>" + html.escape(element.text) + "</pre>"
                )
            )
            if element.asset_id in assets:
                asset = assets[element.asset_id]
                src = quote(
                    os.path.relpath(path / asset.key, source_output.parent).replace("\\", "/"),
                    safe="/.",
                )
                source_body.append(
                    f'<img loading="lazy" src="{src}" alt="Source figure"><p>{html.escape(asset.decision)}: {html.escape(asset.reason)}</p>'
                )
            source_body.append("</section>")
        atomic_write(source_output, shell(doc.filename, "".join(source_body)).encode())
    for key in LABELS:
        if progress:
            progress(key)
        for path, doc, projected in prepared:
            started = time.perf_counter()
            source = projected.model_copy(deep=True)
            if key == "topology":
                chunks, graph = topology_chunks(source, config, budget)
                graphs[path.name] = graph
            else:
                chunks = create_chunks(source, config, budget)
            enrich_chunks(
                source, chunks, config, context_client, context_cache or output / "context_cache"
            )
            timings[key] += time.perf_counter() - started
            checks[key].append(scorecard(source, chunks, config.max_tokens))
            for c in chunks:
                rows[key].append({"bundle": path.name, "path": path, "chunk": c, "doc": doc})
    _validate_gold(gold, sources)
    metrics = {}
    for key, items in rows.items():
        tokens = sorted(r["chunk"].token_count for r in items)
        aggregate = {}
        for name in (
            "token_budget_compliance",
            "parsed_element_retention",
            "parsed_asset_retention",
            "provenance_validity",
        ):
            n = sum(c[name]["numerator"] for c in checks[key])
            d = sum(c[name]["denominator"] for c in checks[key])
            aggregate[name] = {"numerator": n, "denominator": d, "value": n / d if d else None}
        annotations = {
            (r["bundle"], a.asset_id): a
            for r in items
            for a in r["doc"].annotations
            if any(
                x.id == a.asset_id and x.decision != "exclude_from_retrieval"
                for x in r["doc"].assets
            )
        }
        searchable = 0
        for (bundle, aid), annotation in annotations.items():
            text = " ".join(
                r["chunk"].retrieval_text
                for r in items
                if r["bundle"] == bundle and aid in r["chunk"].asset_ids
            )
            # Fragment boundaries add whitespace; test coverage using normalized word sequences.
            needle = " ".join(annotation.description.split())
            if needle and needle in " ".join(text.split()):
                searchable += 1
        metrics[key] = {
            "chunks": len(items),
            "total_tokens": sum(tokens),
            "median_tokens": median(tokens) if tokens else None,
            "max_tokens": max(tokens) if tokens else None,
            "overflow_chunks": sum(t > config.max_tokens for t in tokens),
            "chunking_seconds": round(timings[key], 3),
            "searchable_annotation_descriptions": searchable,
            "available_annotations": len(annotations),
            "contextual_enrichment": {
                "enabled": config.contextual_enrichment_enabled,
                "successful": sum(bool(r["chunk"].contextual_text) for r in items),
                "failed": sum(
                    r["chunk"].contextual_provenance.get("status") == "failed" for r in items
                ),
            },
            **aggregate,
            "retrieval": retrieval_evaluation(items, gold, config.max_tokens),
        }
        atomic_write(
            output / (key + ".jsonl"),
            (
                "\n".join(
                    json.dumps(
                        {"bundle": row["bundle"], **row["chunk"].model_dump(mode="json")},
                        ensure_ascii=False,
                    )
                    for row in items
                )
                + "\n"
            ).encode(),
        )
    report = {
        "title": "Hybrid and topology-aware chunking comparison",
        "version": 2,
        "documents": documents,
        "config": config.model_dump(mode="json"),
        "tokenizer": budget.identity,
        "metrics": metrics,
        "accuracy_winner": None,
        "conclusion": "No answer-accuracy winner established. Structural checks are not accuracy.",
        "fairness": "Both workflows use the same saved extraction, image context, tokenizer, limits "
        "and batch scope. Optional OpenAI context is generated per chunk using the same policy. "
        "Native hybrid preparation time is excluded.",
        "limits": [
            "Saved batch boundaries are retained; no new cross-batch section reconstruction.",
            "Generated annotations and their suggested links remain unverified.",
            "Topology groups available source elements by section occurrence and reading order.",
            "No embeddings or answer generation are performed. Optional retrieval test is BM25 only.",
            "Element-ID retention does not prove complete or correct extracted text.",
        ],
    }
    write_json(output / "metrics.json", report)
    write_json(output / "topology_graphs.json", graphs)
    atomic_write(output / "index.html", render_report(report, rows, output).encode())
    return report


def render_report(report: dict, rows: dict, output: Path) -> str:
    esc = html.escape

    def link(path: Path) -> str:
        return esc(quote(os.path.relpath(path, output).replace("\\", "/"), safe="/.:"), quote=True)

    columns = "".join(f"<th>{esc(label)}</th>" for label in LABELS.values())
    body = [
        "<header><p class='eyebrow'>DOCUMENT LAB / CONTROLLED COMPARISON</p>"
        "<h1>Two chunking workflows.</h1>"
        "<p>Hybrid + image context and Topology-aware + image context.</p></header>",
        "<nav><a href='#scope'>Scope</a><a href='#results'>Results</a>"
        "<a href='#criteria'>Evaluation</a><a href='#inspect'>Inspect chunks</a></nav>",
        "<section id='scope'><h2>What was actually processed</h2>",
    ]
    for checksum, year in dict.fromkeys(
        (d["checksum"], d.get("year")) for d in report["documents"]
    ):
        docs = [
            d for d in report["documents"] if d["checksum"] == checksum and d.get("year") == year
        ]
        pages = sorted({p for d in docs for p in d["pages"]})
        body.append(
            f"<p><strong>{esc(docs[0]['filename'])}</strong> · year {docs[0].get('year', 'unspecified')} · {len(pages)} of "
            f"{docs[0]['total_document_pages']} pages · {sum(d['annotations'] for d in docs)} "
            f"saved annotations / {sum(d['retained_assets'] for d in docs)} retained images.</p>"
            f"<details><summary>Exact page scope</summary><p>{esc(str(pages))}</p></details>"
        )
        topics = list(dict.fromkeys(t for d in docs for t in d.get("topics", [])))[:12]
        body.append(
            "<p>Sections found in the processed document: "
            + esc("; ".join(topics) or "No section headings detected")
            + ".</p>"
        )
    body.append(
        "<p>OCR and OpenAI annotations are reused, not rerun. Missing or failed annotations "
        "remain missing. Image attachment is not an image embedding.</p>"
        "<div class='routes'><article><h3>Hybrid + image context</h3>"
        "<p>Docling/OCR + image context → hybrid narrative groups → table and image chunk policy.</p>"
        "<p>Searchable text includes figure OCR, captions and generated image descriptions. "
        "The current table policy produces row units with repeated headers.</p></article>"
        "<article><h3>Topology-aware + image context</h3>"
        "<p>Docling/OCR + image context → section and reading-order relationships → bounded chunks.</p>"
        "<p>Packs neighboring evidence inside section occurrences, with explicit figure and parent "
        "links. Uses the same searchable image context and table-row units.</p></article></div>"
        "<p>The native Docling JSON is preserved. OpenAI records live in a linked application "
        "enrichment layer; the combined bundle is not passed off as native Docling JSON.</p></section>"
        "<section id='results'><h2>Measured results</h2><p>" + esc(report["fairness"]) + "</p>"
        f"<div class='table-scroll'><table><thead><tr><th>Measure</th>{columns}</tr></thead><tbody>"
    )
    body.append(
        "<tr><th>Target / maximum tokens</th>"
        + "".join(
            f"<td>{report['config']['target_tokens']} / {report['config']['max_tokens']}</td>"
            for _ in LABELS
        )
        + "</tr>"
    )
    body.append(
        "<tr><th>OpenAI chunk context</th>"
        + "".join(
            "<td>"
            + esc(str(report["metrics"][key].get("contextual_enrichment", "Not requested")))
            + "</td>"
            for key in LABELS
        )
        + "</tr>"
    )
    for name, label in [
        ("chunks", "Chunks"),
        ("total_tokens", "Total serialized tokens"),
        ("median_tokens", "Median chunk tokens"),
        ("max_tokens", "Largest chunk"),
        ("overflow_chunks", "Overflows retained for review"),
        ("parsed_element_retention", "Parsed element-ID retention"),
        ("parsed_asset_retention", "Retained image attachment coverage"),
        ("token_budget_compliance", "Token-budget compliance"),
        ("provenance_validity", "Valid source references"),
        ("searchable_annotation_descriptions", "Searchable full annotation descriptions"),
        ("chunking_seconds", "Chunk assembly seconds (one local run)"),
    ]:
        values = []
        for metric in report["metrics"].values():
            value = metric[name]
            if isinstance(value, dict):
                value = (
                    f"{value['value']:.1%} ({value['numerator']}/{value['denominator']})"
                    if value["value"] is not None
                    else "Not applicable"
                )
            values.append(f"<td>{esc(str(value))}</td>")
        body.append(f"<tr><th>{label}</th>{''.join(values)}</tr>")
    body.append(
        "</tbody></table></div><h3>Which is best?</h3><p><strong>"
        + esc(report["conclusion"])
        + "</strong></p><p>Compare Hybrid + image context with Topology-aware + image context using verified "
        "retrieval and answer evaluations. More retained images or fewer chunks alone does not "
        "establish a better retriever.</p></section>"
        "<section id='criteria'><h2>Evaluation criteria</h2><table><tr><th>Criterion</th>"
        "<th>How to evaluate</th><th>Current evidence</th></tr>"
        "<tr><td>Extraction accuracy</td><td>Check OCR, tables and figure labels against PDF pages.</td>"
        "<td>Shared extraction; no new ground-truth assessment.</td></tr>"
        "<tr><td>Chunk integrity</td><td>Token limits, source IDs, attached images and annotation text.</td>"
        "<td>Measured above; not factual accuracy.</td></tr>"
        "<tr><td>Retrieval</td><td>Verified questions with exact evidence. Compare Recall@5, hit rate, "
        "MRR and a 3,000-token context budget using the same retriever.</td><td>"
        "Optional labelled BM25 baseline below. Embedding retrieval not run.</td></tr>"
        "<tr><td>Answer accuracy</td><td>Same answering model and prompt; score correctness, "
        "grounding, citations and numeric/table answers.</td><td>Not run.</td></tr>"
        "<tr><td>Efficiency</td><td>Repeated warm/cold latency, index size, token usage and API cost.</td>"
        "<td>Single-run assembly timing and serialized tokens only.</td></tr></table>"
    )
    for key, metric in report["metrics"].items():
        body.append(
            f"<details><summary>{esc(LABELS[key])} · retrieval evaluation</summary><pre>"
            + esc(json.dumps(metric["retrieval"], indent=2))
            + "</pre></details>"
        )
    body.append(
        "<h3>Limits and source warnings</h3><ul>"
        + "".join("<li>" + esc(limit) + "</li>" for limit in report["limits"])
        + "</ul>"
    )
    for doc in report["documents"]:
        body.append(
            f"<details><summary>Pages {min(doc['pages'])}–{max(doc['pages'])}: "
            f"{len(doc['warnings'])} warnings</summary><pre>"
            + esc("\n".join(doc["warnings"]) + "\nFailures: " + str(doc["failures"]))
            + "</pre></details>"
        )
    body.append(
        "</section><section id='inspect'><h2>Inspect the actual chunks</h2>"
        "<p>Each variant includes source links, image crops, serialized retrieval text, and parent IDs. "
        "AI descriptions are unverified. Expand a batch to inspect all chunks.</p>"
    )
    for document in report["documents"]:
        bundle = document["bundle"]
        body.append(
            f"<details><summary>{esc(document['filename'])} · pages "
            f"{min(document['pages'])}–{max(document['pages'])}</summary>"
            f"<p><a href='documents/{quote(bundle)}/enriched_document.json'>Enriched document bundle</a>"
            "</p><div class='variants'>"
        )
        for key in LABELS:
            body.append(f"<div><h3>{esc(LABELS[key])}</h3>")
            for row in rows[key]:
                if row["bundle"] != bundle:
                    continue
                c = row["chunk"]
                if c.contextual_provenance:
                    body.append(
                        "<p>Context status: "
                        + esc(c.contextual_provenance.get("status", ""))
                        + " · scope: "
                        + esc(c.contextual_provenance.get("scope", ""))
                        + "</p>"
                    )
                source_links = " · ".join(
                    f"<a href='documents/{quote(bundle)}/source.html#e-{quote(eid, safe='')}'>{esc(eid)}</a>"
                    for eid in c.element_ids
                )
                body.append(
                    f"<details id='{key}-{bundle}-{c.id}'><summary>Chunk {c.ordinal} · "
                    f"{c.token_count} tokens · {len(c.asset_ids)} images · pages "
                    f"{esc(str(c.source_pages))}</summary><p>{esc(' › '.join(c.section))}</p>"
                    f"<pre>{esc(c.retrieval_text)}</pre><small>Parent: {esc(c.parent_id or '')}"
                    f"<br>Sources: {source_links}</small>"
                )
                for asset in row["doc"].assets:
                    if asset.id in c.asset_ids:
                        body.append(
                            f"<figure><img loading='lazy' src='{link(row['path'] / asset.key)}' "
                            f"alt='{esc(asset.caption or 'Source figure', quote=True)}'>"
                            f"<figcaption>{esc(asset.caption)} · page {asset.page}</figcaption></figure>"
                        )
                body.append(f"<p class='warning'>{esc('; '.join(c.warnings))}</p></details>")
            body.append("</div>")
        body.append("</div></details>")
    body.append(
        "</section><footer><a href='metrics.json'>Machine-readable metrics</a> · "
        "<a href='topology_graphs.json'>Topology graph</a>"
        "<p>Generated from local saved artifacts. Keep the source artifact folders alongside this report.</p></footer>"
    )
    style = """body{margin:0;background:#f6f5f0;color:#202d30;font:16px/1.6 system-ui,sans-serif}
main{max-width:1450px;margin:auto;padding:48px 32px}header{max-width:850px;padding:30px 0}
h1{font-size:clamp(38px,5vw,70px);line-height:1.08;letter-spacing:-2px}h2{font-size:30px}
.eyebrow{letter-spacing:2px;font-size:12px;color:#426c65}a{color:#17685b}nav{display:flex;gap:24px;flex-wrap:wrap}
section{padding:32px 0;border-bottom:1px solid #cdd4cf}.routes,.variants{display:grid;gap:24px;grid-template-columns:repeat(2,minmax(0,1fr))}
article{border-top:3px solid #426c65}
table{border-collapse:collapse;width:100%;background:white}td,th{padding:12px;text-align:left;border-bottom:1px solid #d6dcd7}
th{font-weight:600}.table-scroll{overflow:auto}details{margin:12px 0;border:1px solid #d6dcd7;padding:12px;background:#fff}
summary{cursor:pointer;font-weight:600}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:13px/1.6 ui-monospace,monospace}
img{max-width:100%;height:auto}figure{margin:12px 0}small,figcaption{font-size:12px;overflow-wrap:anywhere}
.warning{color:#934a1b}footer{padding:30px 0}@media(max-width:950px){.variants,.routes{grid-template-columns:1fr}main{padding:24px 16px}}
"""
    rendered = "".join(body)
    before, remainder = rendered.split("<section id='results'>", 1)
    metrics, remainder = remainder.split("<section id='inspect'>", 1)
    inspection, footer = remainder.split("<footer>", 1)
    rendered = (
        before
        + "<section id='inspect'>"
        + inspection
        + "<section id='results'>"
        + metrics
        + "<footer>"
        + footer
    )
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{esc(report['title'])}</title><style>{style}</style></head><body><main>"
        + rendered
        + "</main></body></html>"
    )
