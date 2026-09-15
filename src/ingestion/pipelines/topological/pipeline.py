"""SIR lineage, atomic visual nodes, semantic boundaries and capacity auditing."""

import re

from ingestion.chunking import eligible_elements
from ingestion.pipelines.topological.agents import enhance, inspect, refine
from ingestion.pipelines.topological.parser import TopologicalParser
from ingestion.pipelines.topological.sir import SIRIndex
from ingestion.schemas import Chunk, stable_id
from ingestion.storage import write_json


def parser(settings, config):
    return TopologicalParser()


def chunk(doc, config, budget, client, output):
    if client is None:
        raise ValueError("Topological agents require an OpenAI client")
    elements, _ = eligible_elements(doc)
    probes = {
        "pages": len(doc.pages),
        "elements": len(elements),
        "headings": sum(e.kind == "heading" for e in elements),
        "tables": sum(e.kind == "table" for e in elements),
        "figures": sum(bool(e.asset_id) for e in elements),
        "extraction_failures": len(doc.failures),
    }
    try:
        inspection = inspect(client, config, probes, output).model_dump()
    except Exception as exc:
        inspection = {"path": "SEMANTIC_FLOW", "status": "fallback", "error": type(exc).__name__}
    registry = {e.id: e.text or e.caption for e in elements}
    root = stable_id(doc.checksum, "sir", doc.pages)
    nodes = [{"id": root, "kind": "document", "parent_id": None, "lineage": [doc.filename]}]
    stack, groups = [], []
    previous_path = []
    for e in sorted(elements, key=lambda e: e.order):
        path = e.section
        common = 0
        while common < min(len(path), len(previous_path)) and path[common] == previous_path[common]:
            common += 1
        # A repeated heading is a new occurrence, even when its title matches.
        if e.kind == "heading" and common == len(path) and common:
            common -= 1
        stack = stack[:common]
        for depth in range(common, len(path)):
            parent = stack[-1] if stack else root
            nid = stable_id(root, e.id, depth)
            nodes.append(
                {
                    "id": nid,
                    "kind": "section",
                    "parent_id": parent,
                    "lineage": [doc.filename] + path[: depth + 1],
                }
            )
            stack.append(nid)
        parent = stack[-1] if stack else root
        atomic = e.kind in {"table", "picture", "chart"}
        text = e.text or e.caption
        if e.table and not text:
            text = "\n".join(c["text"] for c in e.table.get("table_cells", []))
        if not text and e.asset_id:
            text = "[Figure]"
        if not text:
            continue
        registry[e.id] = text
        node = {
            "id": e.id,
            "kind": e.kind,
            "parent_id": parent,
            "lineage": [doc.filename] + path,
            "atomic": atomic,
            "token_count": budget.count(text),
            "page": e.page,
            "bbox": e.bbox,
        }
        nodes.append(node)
        spans = (
            [(text, 0, len(text))]
            if atomic
            else [
                (m.group(), m.start(), m.end())
                for m in re.finditer(r"[^.!?\n]+[.!?\n]*|[.!?\n]+", text)
            ]
        )
        for body, start, end in spans:
            unit = {
                "text": body,
                "start": start,
                "end": end,
                "element_id": e.id,
                "atomic": atomic,
                "page": e.page,
                "asset_id": e.asset_id,
            }
            if not groups or groups[-1]["parent"] != parent:
                groups.append({"parent": parent, "section": path, "units": []})
            groups[-1]["units"].append(unit)
        previous_path = list(path)
    # Aggregate subtree token weights bottom-up without repeated tree traversals.
    by_id = {n["id"]: n for n in nodes}
    for n in reversed(nodes):
        n["subtree_tokens"] = n.get("subtree_tokens", 0) + n.get("token_count", 0)
        if n["parent_id"]:
            p = by_id[n["parent_id"]]
            p["subtree_tokens"] = p.get("subtree_tokens", 0) + n["subtree_tokens"]
    sir = SIRIndex(nodes, registry)
    write_json(
        output / "topology_sir.json",
        {
            "root": root,
            "nodes": nodes,
            "scope": "current_page_batch",
            "implementation": "docling-topology-v2",
            "inspector": inspection,
        },
    )
    result = []
    for gi, group in enumerate(groups):
        # Bound each semantic audit input; capacity splitting below is lossless.
        windows, window, size = [], [], 0
        for unit in group["units"]:
            if window and size + len(unit["text"]) > 12000:
                windows.append(window)
                window, size = [], 0
            window.append(unit)
            size += len(unit["text"])
        if window:
            windows.append(window)
        for wi, units in enumerate(windows):
            warnings = []
            try:
                if any(len(u["text"]) > 12000 for u in units):
                    raise ValueError("Oversized source unit requires deterministic capacity audit")
                decision = refine(
                    client,
                    config,
                    [{**u, "id": i} for i, u in enumerate(units)],
                    [doc.filename] + group["section"],
                    registry,
                    output,
                    f"{gi}-{wi}",
                    sir=sir,
                    audit_profile=inspection["path"],
                )
                boundaries = decision.boundary_ids + [len(units)]
                metadata = decision.model_dump()
            except Exception as exc:
                boundaries = [0, len(units)]
                metadata = {"status": "failed", "error": type(exc).__name__}
                warnings.append("Refiner failed; deterministic source-preserving fallback")
            prefix = doc.filename + " | " + " > ".join(group["section"]) + "\n"
            for begin, end in zip(boundaries, boundaries[1:]):
                pending = []

                def emit():
                    if not pending:
                        return
                    body = "".join(u["text"] for u in pending)
                    retrieval = prefix + body
                    final_metadata = {**metadata, "inspector": inspection}
                    try:
                        if budget.count(retrieval) > budget.cap:
                            raise ValueError("Oversized atomic unit requires review")
                        signature = enhance(
                            client,
                            config,
                            body,
                            list(dict.fromkeys(u["element_id"] for u in pending)),
                            sir,
                            output,
                            len(result),
                        )
                        final_metadata["enhancement"] = {
                            "status": "generated_unverified",
                            **signature.model_dump(),
                        }
                    except Exception as exc:
                        final_metadata["enhancement"] = {
                            "status": "failed",
                            "error": type(exc).__name__,
                        }
                    signature_data = final_metadata["enhancement"]
                    supplement = "\n".join(
                        v for v in (signature_data.get("title"), signature_data.get("context")) if v
                    )
                    if supplement:
                        enhanced = (
                            "[Refiner context â€” generated, unverified]\n"
                            + supplement
                            + "\n"
                            + retrieval
                        )
                        if budget.count(enhanced) <= budget.cap:
                            retrieval = enhanced
                    pages = sorted({u["page"] for u in pending})
                    assets = list(dict.fromkeys(u["asset_id"] for u in pending if u["asset_id"]))
                    count = budget.count(retrieval)
                    ordinal = len(result)
                    result.append(
                        Chunk(
                            id=stable_id(doc.config_hash, "agent-topology", ordinal, retrieval),
                            ordinal=ordinal,
                            text=body,
                            retrieval_text=retrieval,
                            start_page=pages[0],
                            end_page=pages[-1],
                            source_pages=pages,
                            element_ids=list(dict.fromkeys(u["element_id"] for u in pending)),
                            asset_ids=assets,
                            section=group["section"],
                            token_count=count,
                            tokenizer=budget.identity,
                            parent_id=group["parent"],
                            source_spans=[
                                {
                                    "element_id": u["element_id"],
                                    "start": u["start"],
                                    "end": u["end"],
                                    "atomic": u["atomic"],
                                }
                                for u in pending
                            ],
                            topology_provenance=final_metadata,
                            warnings=warnings
                            + (
                                ["atomic_overflow: retain for review; do not embed"]
                                if count > budget.cap
                                else []
                            ),
                        )
                    )
                    pending.clear()

                for unit in units[begin:end]:
                    if unit["atomic"]:
                        emit()
                        pending.append(unit)
                        emit()
                        continue
                    for text, start, stop in budget.split(unit["text"], prefix):
                        part = {
                            **unit,
                            "text": text,
                            "start": unit["start"] + start,
                            "end": unit["start"] + stop,
                        }
                        if pending and budget.count(
                            prefix + "".join(u["text"] for u in pending) + text
                        ) > min(config.target_tokens, budget.cap):
                            emit()
                        pending.append(part)
                emit()
    return result
