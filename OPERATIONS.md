# Running and understanding the ingestion application

## Two-workflow profile

The current OpenAI profile executes Hybrid + image context and Topological + image context sequentially. See [PIPELINES.md](PIPELINES.md) for
the authoritative execution order, module ownership, local OCR requirements,
batch-level recovery and measured HTML matrix. The two-workflow execution details
below describe the older saved-Docling comparison mode, which remains available
for historical runs; they do not describe the new selected-pipeline coordinator.

## What is implemented

The application accepts arbitrary PDFs with a year for each document. It extracts
text, tables and images, generates two chunking representations, and publishes an
HTML comparison. OpenAI image classification and chunk context are enabled by the
OpenAI processing profile. They require internet access even when Docker runs locally.

Implemented does not mean that every document has been processed or that accuracy
has been established. Software tests use controlled fixtures and simulated API
responses. A live run on representative PDFs is still needed to assess OCR, table
extraction, image decisions and generated context. The future retrieval ensemble,
embedding indexes, cross-encoder, ColPali and 1,000-request experiment in
[Discussion.md](Discussion.md) are not implemented retrieval services.

## Terminology

| Term | Meaning in this application |
| --- | --- |
| Document | One uploaded PDF plus its year and document ID |
| Document job | One queued processing run for that document and configuration |
| Multi-file upload | Several documents submitted together; each has an independent job |
| Page batch | Up to 10 new pages processed in a fresh subprocess to bound model memory |
| Context page | The preceding page, when available, used during parsing; not published again as new evidence |
| Checkpoint | Published progress every 50 pages and at the final page |
| Hybrid workflow | Docling HybridChunker narrative grouping plus application table/image/token rules |
| Topology-aware workflow | Groups source elements by section occurrence and reading order |
| Image annotation | Generated description and contextual classification of a detected image crop |
| Chunk context | Short generated explanation prepended to retrieval text; original chunk content stays separate |
| Artifact | A saved file such as JSON, JSONL, HTML, a PDF or an image |
| Node preparation | Conversion to LlamaIndex node records; it does not create embeddings or a search index |

The internal `indexing_status` field currently describes node preparation. It must
not be interpreted as confirmation that vector indexing or RAG retrieval has run.

## Docker startup

Run commands from the repository root with Docker Desktop using Linux containers.
Keep the private `.env` out of Git. Default Compose connects to Supabase; it does not
start a separate local PostgreSQL container. Verify these settings without printing
credentials to logs:

- `DATABASE_URL`: the intended Supabase PostgreSQL connection string.
- `STORAGE_BACKEND=supabase`, the matching `SUPABASE_URL`, backend key and private bucket.
- `CONFIG_PATH=configs/openai.yaml` to enable image classification and chunk context.
- `OPENAI_API_KEY`: the private key for those requests.

Use the existing README setup instructions for the exact credential fields. The
coordinator uses a session advisory lock, so use a direct database connection or a
session pooler, not a transaction-mode pooler. See
[Supabase connection guidance](https://supabase.com/docs/guides/database/connecting-to-postgres).

First setup, if models have not already been downloaded:

```powershell
New-Item -ItemType Directory -Force models,data,artifacts,reports
docker compose build api
docker compose run --rm --build download-models
docker compose up -d api
```

After source/configuration changes:

```powershell
docker compose build api
docker compose up -d api
docker compose ps
docker compose logs --tail=100 api
```

Building an image does not update an already running container. `up -d` recreates
the service when its image/configuration changed. Source code and YAML profiles are
copied into the image; only models and data/artifact/report directories are mounted.
Startup applies database migrations and then starts FastAPI and its job coordinator.

Open [the document library](http://localhost:8000) and select one or multiple PDFs.
Review the year for every file, then choose **Queue documents**. Select **View both
workflows** to inspect the completed report. Metrics appear below the two chunk columns.
The existing library appears on startup; Docker does not automatically reprocess it.

Avoid restarting during processing when practical. An interrupted job is recovered
as paused and can be resumed from its published checkpoint. Configuration/model
fingerprint changes require **Reprocess**, because combining old and new extraction
policies in one run would be misleading. Uncheckpointed work can be repeated.

## Exact execution order today

1. Upload and validate each PDF/year; save its original and queue the document job.
2. One coordinator claims work in queue order. PostgreSQL allows only one active
   coordinator across instances through a global advisory lock.
3. For each page batch, start a disposable worker process. Docling parses the pages;
   RapidOCR supplies OCR, with one-way Tesseract fallback on failures. Text extraction
   from native PDF text remains distinct from OCR.
4. Collect image signals, read crop text and, when enabled, ask OpenAI to classify
   and describe every detected crop. Retain uncertain/substantive images. Exclude
   clearly decorative image-derived content from retrieval, preserving the originals.
5. Build Hybrid ingestion chunks, add optional OpenAI chunk context, persist ingestion
   records and publish supporting artifacts. Save checkpoints at the stated intervals.
6. Once all pages are checkpointed, build the comparison from saved extraction.
   Assemble Hybrid across all saved batches first, then Topology-aware across all
   batches. This currently reassembles Hybrid comparison chunks; it does not rerun
   Docling/OCR/image annotation. Identical contextual requests reuse the local cache.
7. Publish comparison JSONL, graph, metrics and HTML. Supporting files are uploaded
   before the entry HTML. Only then mark the comparison-enabled job complete.
8. Move to the next queued document.

Resuming comparison on an empty local cache restores canonical data, manifests,
image crops and native Docling JSON from private Storage. Generated request caches
are local optimizations; on a new machine, uncached context calls may incur cost again.

`complete` means the job's required processing/publication finished. Individual
annotations/context calls can still be flagged failed or unverified in scorecards.
It does not mean measured extraction or answer accuracy is 100%.

## Where results are stored

| Content | Supabase mode | Local mode |
| --- | --- | --- |
| Document/year/page metadata, jobs and checkpoints | PostgreSQL | Local PostgreSQL |
| Hybrid ingestion chunks, elements, images/annotation metadata and relationships | PostgreSQL | Local PostgreSQL |
| Original PDFs and published extraction/image/HTML artifacts | Private Storage bucket, plus local cache | Mounted data/artifact folders |
| Both comparison chunk sets, topology graph and comparison metrics | JSONL/JSON/HTML in private Storage; summary metrics also in the parent job record | Artifact files and parent job record |
| Downloaded model files | Local mounted `models/` | Local mounted `models/` |
| Annotation/context response caches | Local `data/` cache; published annotation records also accompany artifacts | Local `data/` cache |
| Additional execution reports and container logs | Local report folder / Docker logs | Local report folder / Docker logs |

Therefore, “everything is in Supabase” is too broad. Important published document
results are shared, but model files and working caches are local. Topology-aware
comparison chunks are not currently separate rows in the relational chunks table.
The chunks API exposes relational ingestion chunks; the comparison inspector and
JSONL files expose both representations. Neither representation has embeddings yet.

## CPU processing now, parallel processing later

Batching is a memory/recovery policy, not a restriction imposed by CPUs. More CPU
cores and sufficient RAM can support concurrent work. A GPU may accelerate supported
Docling models, but does not automatically parallelize this application's scheduler.
Tokenization, chunk assembly, file I/O and database writes still use CPU resources.
OpenAI inference runs remotely; a local GPU does not accelerate those API calls.

Today the worker sets `OMP_NUM_THREADS=4`; figure RapidOCR also sets four ONNX Runtime
threads. The dependency configuration explicitly selects CPU PyTorch wheels, and standard
Compose exposes no GPU. Docling's accelerator device is not explicitly
overridden by the application. On the current CPU-only container it runs without a
CUDA device. The global coordinator lock intentionally serializes document jobs.
Starting more API replicas does not create parallel ingestion, and the fixed localhost
port mapping also needs redesign for multiple replicas on one machine.

### Proposed scaling work, not a switch already implemented

1. **Measure the bottleneck:** model time, peak RAM/VRAM, CPU utilization, API wait,
   Storage upload time and database load. Optimize the limiting stage.
2. **Prefer independent documents first:** replace the global single-worker policy
   with atomic per-job claims, leases/heartbeats and bounded worker concurrency.
   Preserve idempotent writes, retry safety and a single final publisher per job.
3. **Budget CPU and memory:** expose thread counts and worker count as validated
   settings. Avoid multiplying many workers by many native inference threads.
4. **Add GPU support deliberately:** provide a tested compatible image, host drivers,
   container GPU access, explicit Docling accelerator settings and a compatible OCR
   runtime. Accelerator configuration is documented by
   [Docling](https://docling-project.github.io/docling/_generated/examples/run_with_accelerator/).
   The current Tesseract fallback remains a CPU path.
5. **Parallelize chunk workflows after extraction:** pass immutable prepared content
   to separate workers, write into separate workflow directories, and join completed
   results before publishing the comparison. Both must use identical source/policy versions.
6. **Treat page parallelism separately:** adjacent-page context, section propagation
   and cross-page tables impose dependencies. Preserve ordering and boundary handling
   before processing page batches independently.
7. **Bound API concurrency:** use request/token limits, retries and shared cache
   coordination for OpenAI calls. More CPU/GPU hardware does not increase API quotas.
8. **Benchmark and test recovery:** compare throughput, p95 completion time, cost and
   source/quality invariants; test worker crashes and duplicate delivery before promotion.

Start with a small measured concurrency, then increase only when memory and quality
checks allow it. Keep the sequential mode as a reproducible baseline and fallback.

## Code reading guide

| File | Responsibility |
| --- | --- |
| `api.py`, `dashboard.html` | Upload endpoints, document library, result selection |
| `config.py`, `configs/*.yaml` | Validated processing settings and configuration fingerprints |
| `jobs.py` | Queue ownership, page batches, checkpoints, comparison publication |
| `worker.py` | One disposable page-batch process and preceding-page handling |
| `parser.py`, `ocr_options.py`, `ocr.py` | Docling conversion, engine configuration/fallback, crop text |
| `images.py`, `annotations.py` | Image signals, structured vision response, retention decisions |
| `chunking.py`, `topology.py` | Two chunking strategies and shared source/image rules |
| `contextual.py` | Per-chunk OpenAI context, provenance, cache and final token checks |
| `ingestion.py` | Per-batch orchestration, relational persistence, artifact publication |
| `chunk_comparison.py`, `export.py` | Inspectable comparison, metrics and source views |
| `cloud.py` | Private object upload/download and local cache |
| `tests/` | Fixture-based contracts and regression checks |

## Verification boundary

Automated tests establish software behavior, not OCR or RAG accuracy. A successful
Docker build establishes packaging, not a completed live ingestion. Before calling a
release fully verified, run a small representative PDF set through the rebuilt
container, inspect both workflows and context failures, verify private artifact links
from a fresh cache, and measure extraction/annotation quality. PostgreSQL-specific
tests require a dedicated test database; never use a shared document library as the
destructive test database.


### Verification recorded on 2026-09-15

- 50 local automated tests passed; the optional PostgreSQL-specific suite was not run.
- Docker image build passed, including native vision-library imports.
- An isolated network-disabled container converted page 7 of `report_2025.pdf` with
  RapidOCR and no conversion failures, then generated 26 Hybrid and 13 Topology-aware
  chunks. All 39 chunks satisfied the configured token ceiling.
- The container verified English Tesseract availability and the multi-upload API route.
- Smoke artifacts are in `artifacts/verification-20260915/`; OpenAI was disabled for
  this smoke test, and nothing from it was written to Supabase.
- The running API was healthy in Supabase mode with the OpenAI profile, but still
  used the previous image. The audit built the replacement image without restarting
  that service. Full live OpenAI enrichment and new-version multi-document ingestion
  into Supabase remain unverified.
