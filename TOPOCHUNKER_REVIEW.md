# Topological chunking: implementation and upstream mapping

## Processing flow

Docling + RapidOCR (Tesseract fallback) → extracted text, tables and figure crops →
figure OCR → OpenAI image classification and annotation → deterministic image filtering →
Inspector audit profile → SIR → Refiner semantic boundaries → token capacity audit →
per-chunk title/context → shared retrieval context enrichment → saved chunks and HTML.

OpenAI receives image crops for image interpretation and extracted text for topology
and context decisions. Full-page renders are never uploaded by the topological parser.
OCR recognizes text; Docling layout detection locates the image regions to crop.
Canonical source assets remain inspectable even when excluded from retrieval.

## Mapping to the reference

References: [TopoChunker paper](https://arxiv.org/html/2603.18409v2),
[Inspector implementation](https://github.com/liushifu12138/TopoChunker/blob/main/agents/inspector.py),
[Refiner tools](https://github.com/liushifu12138/TopoChunker/blob/main/agents/refiner_tools.py).

| Concept | This application's adaptation |
| --- | --- |
| Inspector extraction routing | Deliberately fixed to local Docling/OCR. One compact, count-based decision selects a post-extraction structural, narrative or visual audit profile. It cannot request page uploads. This is not the upstream multi-probe ReAct extraction router. |
| Structured Intermediate Representation (SIR) | Explicit document/section/element nodes with parent, children, previous/next sibling links, lineage, coordinates and subtree token counts. |
| Semantic_Slicer | Refiner selects immutable span boundary IDs. Python validates and packs spans within the configured tokenizer budget. |
| Atomic visual nodes | Tables and figures remain indivisible; oversize units are preserved with an overflow warning and must not be embedded over the model limit. |
| SIR_Query | Bounded traversal of source nodes, ancestors, adjacent siblings and children. Returned evidence and decisions are saved; unknown IDs are rejected. Definitions are interpreted from visible source evidence rather than a separate entity index. |
| Chunk_Enhancer | A separate decision for each final chunk produces a 3–8 word title and optional source-linked context. It does not reuse one window title for every final chunk or rewrite source text. |
| Failure handling | Invalid decisions retain source content with failure metadata; uncertain image classifications remain for review. Generated text remains explicitly unverified. |

The three audit profiles guide Refiner attention: heading groups, narrative transitions,
or visual relationships. All preserve the same source and atomicity constraints.
The shared chunk-context enrichment remains a separate stage, so model cost includes
both final-chunk enhancement and contextual enrichment when enabled.

## Explicit limits

This is an adaptation, not a complete replication or a claim to the paper's benchmark
accuracy. SIR and evidence queries cover the current page batch. Definitions in other
batches are unavailable; cross-page tables are not automatically joined. Docling/OCR
may omit or misread source content, which downstream slicing cannot repair.
The upstream Longformer semantic extraction and MinerU visual backend are not used.
The stored graph and source spans make these limits visible for evaluation.

Evaluate extraction fidelity, useful-image retention, boundary quality, token overflow,
retrieval recall and answer grounding separately. Tests verify software contracts;
real accuracy requires labeled documents/questions and a retrieval experiment.

## Code and saved artifacts

- `pipelines/topological/parser.py`: local extraction adapter.
- `pipelines/topological/sir.py`: graph pointers and bounded traversal.
- `pipelines/topological/agents.py`: Inspector, boundary decisions, queries and enhancement.
- `pipelines/topological/pipeline.py`: tree construction, atomicity and capacity packing.
- `topology_sir.json`: graph and Inspector decision per batch.
- `raw/`: structured agent decisions, usage and requested evidence.
- Chunk provenance: source spans, Inspector profile, boundary and enhancement metadata.

The older `src/ingestion/topology.py` saved-document comparison helper is separate
and does not call these agents. See [PIPELINES.md](PIPELINES.md) for selectable workflows.

## Updating existing runs

The application policy version changes because the extraction backend changed.
Use **Reprocess** for a new run; an old checkpoint must not mix page-VLM extraction
with the new Docling topology backend. Existing saved results are retained.
