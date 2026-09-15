# Two chunking workflows

## Execution order

The HTML upload form lets you select any nonempty subset of the two workflows.
Docling + RapidOCR and agentic topological parsing are checked by default;
The same choices apply to Reprocess. Selections are saved with each job and preserved
on resume. Only selected pipelines run and appear in the final comparison matrix.
The topology pipeline uses OpenAI remotely and does not require a local GPU.
API clients can supply `pipelines` as a JSON array in upload/reprocess form data;
omitting it retains the configured server profile.

The `configs/openai.yaml` profile enables `pipeline_comparison: true`. For each uploaded
document, one coordinator completes these pipelines in order:

| Pipeline folder in `src/ingestion/pipelines` | Extraction | Chunking | Enrichment |
| --- | --- | --- | --- |
| `docling_hybrid` | Docling + RapidOCR, automatic Tesseract fallback | Native Hybrid grouping and shared token/table policy | OpenAI image classification/annotation and chunk context |
| `topological` | Docling + RapidOCR, automatic Tesseract fallback | SIR lineage, Refiner-selected boundaries, deterministic capacity audit | Refiner title/context plus image policy and chunk context |

Each pipeline processes at most ten new pages in a disposable worker process, with
one preceding context page when available. It persists canonical extraction, image
assets, annotations, chunks and metrics before the next batch. A completed batch is
checkpointed immediately in pipeline-comparison mode. Resume continues the interrupted
pipeline at its next uncommitted batch; earlier completed pipelines are retained.

Resume validates the saved configuration, application policy, tokenizer and model
inventory before queueing. An incompatible run returns HTTP 409 and shows
**Reprocess required**. Reprocess starts a separate run with current settings;
old chunks remain in history. A run with zero checkpointed pages has no saved page
progress to recover. Changing only the default YAML profile does not invalidate a
compatible run: Resume still uses that run's saved settings. The worker rechecks
compatibility before processing in case assets changed after the request.

Multiple uploaded documents enter the document queue. The worker completes all
selected pipelines for one document before starting the next document. It does not run
two inference processes simultaneously. Future concurrency needs explicit resource
budgets and job ownership changes, even with more CPUs or GPUs.

## Code map

- `pipelines/registry.py`: names, order and lazy dispatch.
- `pipelines/docling_hybrid/parser.py`: native Docling conversion.
- `pipelines/topological/parser.py`: local Docling conversion without Hybrid grouping.
- `pipelines/topological/agents.py`: bounded structured OpenAI decisions and SIR queries.
- `pipelines/topological/pipeline.py`: SIR, atomic nodes and lossless source-span assembly.
- `pipelines/report.py`: one column per selected pipeline, then measured comparison matrix.
- `jobs.py`: coordinator, batch checkpoints and final publication.
- `worker.py`: one disposable batch process.
- `ingestion.py`: common image filtering, annotations, context, export and persistence.

Storage, token counting and schema validation are shared infrastructure. Each pipeline
owns its extraction and chunking behavior. The old `topology.py` remains available
for historical saved-Docling comparisons; it is not called by the topological workflow.

Image processing follows **OpenAI classification → deterministic filtering → chunking**.
`images.py:filter_image_content` removes excluded images, their linked source elements
(including image text/captions), OCR and annotations from the retrieval copy before
chunking or chunk-context enrichment. Native groups containing excluded elements are
rebuilt from retained content. Filtering makes no additional model call. Useful images
retain their image context. Uncertain/failed classifications remain available for review.
The canonical source keeps originals and decisions for the inspector.

## Topological implementation and limits

Docling parses every selected page locally with layout detection, table recognition
and OCR. OpenAI never receives full-page renders for extraction. Extracted figure
crops, figure OCR and nearby text go through the shared image classification and
annotation stage; deterministic filtering removes excluded image content before SIR
construction. Original assets and decisions remain in the canonical inspector.

The Inspector selects a post-extraction audit profile using compact local counts.
The extraction backend stays Docling for all profiles. The SIR has explicit parent,
children and sibling pointers, ancestor paths, geometry and subtree token weights.
The Refiner queries this graph for bounded additional evidence and proposes semantic
boundary IDs. Python validates IDs and slices immutable source spans. After capacity
packing, each final chunk receives its own thematic title and optional evidence-linked
context. Generated text is marked unverified; failed calls preserve source content.
See [TOPOCHUNKER_REVIEW.md](TOPOCHUNKER_REVIEW.md) for the upstream mapping.

Atomic tables/figures are not split by the Refiner. Oversized atomic chunks remain
available for review and must be excluded from a future embedding request that exceeds
the embedding model's input limit. They are not silently truncated.

Important evaluation limits:

- The SIR and cross-reference lookup are batch-scoped. Full-document definitions or
  cross-page tables beyond that scope are not automatically reconstructed. Boundary
  tables are flagged for review.
- Docling/OCR extraction can omit or misread source content; deterministic slicing cannot
  repair an upstream extraction error.
- No multimodal embedding, dense retrieval, BM25 production index or reranker is
  deployed by this work. LlamaIndex nodes are prepared without generating embeddings.
- Mechanical tests establish contracts and preservation, not corpus accuracy.

## Run with Docker

The current OpenAI defaults use `gpt-5.6-terra` for image annotations,
chunk context and the topological Inspector/Refiner. The client uses
medium reasoning, omits fixed sampling temperature and reserves 4,096 additional
completion tokens for reasoning. This does not change the embedding chunk limit.
Model access is account-dependent; corpus quality improvement has not been measured.
Changing the model changes run/cache identities. Existing results are not relabeled.

In `.env`, use `CONFIG_PATH=configs/openai.yaml`, the existing
database/Storage settings, and `OPENAI_API_KEY`. Both workflows use local Docling/OCR;
no additional OCR model server is required. A local GPU is optional for Docling.
OpenAI annotations and topology decisions require an internet connection.

From the repository root:

```powershell
docker compose build api
docker compose up -d api
```

Upload PDFs at `http://localhost:8000`. The API queues the documents. After completion,
open the pipeline comparison. CLI ingestion with this profile also queues a job;
the API coordinator must be running to consume it. Existing runs preserve their
original profile; reprocess to create a run using the new pipeline-comparison configuration.

## What the HTML measures

### Run history and deletion

Uploading up to 50 PDFs queues documents independently. Reprocess creates a new
run; it does not overwrite earlier chunks. A successful run becomes the current
result. Failed and paused runs retain their saved checkpoints.

Each document card shows the latest run, with earlier runs under **Run history**.
Use **View results** or **View saved checkpoint** to select a specific run. The
results panel also has a run selector. Counts labeled **page passes saved** include
each pipeline's work: processing ten pages with two pipelines counts as twenty passes.
Errors, annotation status, full run IDs and JSON links are under **Details**.

**Delete run** asks for confirmation and removes that run's records, all child
batches (including uncheckpointed batches), chunks, evidence, annotations, checkpoint
files, generated HTML, exports and local run reports. The original PDF, other runs
and shared annotation/context caches remain available. Deleting the current result
selects a remaining completed run when one exists. This is permanent deletion, not
an archive or undo operation.

Queued, uploading and processing runs cannot be deleted. Cleanup uses a database
lock and a persistent deletion state so resume cannot race it. If cloud/file cleanup
fails or the API restarts mid-delete, use **Retry delete**. Records remain until
cleanup succeeds; partially deleted results are hidden. The endpoint is
`DELETE /v1/runs/{run_id}`. Child batch IDs cannot be deleted independently.

One column per selected pipeline shows saved chunks with pipeline-specific source inspector links.
The matrix below reports pages, failures, extracted elements/images, excluded images,
chunk counts, token statistics, warnings, contextual enrichment and elapsed time.
These are observed output differences, not accuracy scores. The report does not
re-run extraction or chunking to generate its comparison.

First evaluate extraction and chunk evidence against independently labeled pages.
Then evaluate the two chunking strategies—Hybrid and agentic topological—using the
same verified retrieval questions and extraction settings. Choose one or more representations based on measured evidence recall,
answer correctness, citation support, latency and cost. The RAG ensemble, query
decomposition and ColPali experiment remain plans in `Discussion.md`.

## Verification

The regression suite checks workflow selection, source preservation, image filtering,
SIR traversal, enhancement, overflow handling and checkpoint recovery. Model responses
are simulated in these tests. PostgreSQL integration tests require a separate test database.
A full corpus comparison and retrieval accuracy evaluation remain to be performed.

After upgrading, use Reprocess to start a run with the two-workflow configuration.
Old run artifacts remain accessible, and retired OCR runs can still be deleted through
the normal run cleanup endpoint. No additional OCR server is used by new jobs.
