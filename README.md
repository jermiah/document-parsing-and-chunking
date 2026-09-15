# Document parsing and chunking

## Two chunking workflows

Choose **Hybrid + image context**, **Topological + image context**, or both.
Both use local Docling + RapidOCR with automatic Tesseract fallback, OpenAI image
classification and annotation, deterministic image filtering, and chunk context enrichment.
Hybrid completes first, followed by Topological, in sequential page batches.
Each workflow currently performs its own extraction using the same settings.
See [the pipeline guide](PIPELINES.md) for setup, checkpoints and the HTML comparison.

## How it works

1. **Upload a PDF and its year** through the document library or API. The application
   queues a background processing job.
2. **Extract the document content independently for each selected pipeline.** Use
   Docling + RapidOCR with automatic Tesseract fallback for both workflows.
3. **Process images.** The application collects image crops, captions and OCR text.
   The optional OpenAI profile adds image classifications and descriptions.
4. **Create chunks.** Content is grouped within token limits, with source references
   and relevant image context.
5. **Enrich chunk context.** With the OpenAI profile enabled, OpenAI generates a short
   explanation of how each chunk relates to the document's available content. This
   explanation is stored separately and prepended to the retrieval text. Original
   source text remains unchanged; generated context is marked as unverified.
6. **Save progress and results.** Checkpoints make completed chunks available while
   processing continues. PostgreSQL stores ingestion chunks and metadata; originals
   and inspection files use the configured local or Supabase storage.
7. **Inspect the output.** Open the saved chunks and HTML inspectors from the library.
   The final report shows selected pipelines side by side, with metrics below the chunks.

## Repository layout

```text
README.md                          Setup and usage
Documents/                      Input PDFs
configs/                        Processing profiles
scripts/                        Model download command
src/ingestion/                  Application package (named ingestion)
  pipelines/                    Separate pipeline implementations
    docling_hybrid/             Docling parsing and Hybrid chunking
    topological/               Inspector, SIR and Refiner
    registry.py                 Pipeline names and execution order
    report.py                   Selected-pipeline HTML comparison
  ingestion.py                  Shared ingestion stages and persistence
  jobs.py                       Sequential coordinator and checkpoints
  worker.py                     One page-batch subprocess
migrations/                     Database schema
tests/                          Automated checks
Dockerfile                      Builds the API image
compose.yaml                    Default: API with Supabase
compose.supabase.yaml           Equivalent explicit Supabase setup
compose.local.yaml              Optional local PostgreSQL setup
```

The shared modules remain under `src/ingestion/`: `annotations.py` handles image
classification, `images.py` applies retention policy, `contextual.py` enriches chunks,
and `tokenizer.py`, `schemas.py`, `storage.py` and `export.py` provide common services.
The root `parser.py` forwards imports to `pipelines/docling_hybrid/parser.py` for
compatibility. Its smaller size reflects relocation of the implementation.
See [PIPELINES.md](PIPELINES.md) for the current code reading guide.

## Choose how to run

| Mode | Database and saved results | Command from the repository root |
| --- | --- | --- |
| Supabase | Shared cloud database with previously saved chunks | `docker compose up -d` |
| Local PostgreSQL | Separate database on your machine; upload PDFs to create local chunks | `docker compose -f compose.local.yaml up -d` |

## Option 1: Supabase with saved chunks

Docker Desktop must be running with Linux containers. Run these commands from the
repository root:

```powershell
git clone https://github.com/jermiah/document-parsing-and-chunking.git
cd document-parsing-and-chunking
```

**The private `.env` file is shared separately with the evaluator.** Save it as
`.env`. GitHub contains only `.env.example`, with empty credential
fields. Database passwords, the Supabase backend key and the OpenAI key are kept
out of Git and the Docker image. Compose reads the private file at runtime.

If setting up your own project, copy `.env.example` to `.env` and fill in the
credentials supplied separately. Do not overwrite an already configured `.env`.

If you received the configured `.env`, keep its connection and storage values to
access the existing shared chunks. Otherwise copy `.env.example` and fill these
fields with the values supplied separately (angle-bracket values are placeholders):

```dotenv
DATABASE_URL=postgresql+psycopg://postgres.fctgqpxhufjotwlenjxg:<encoded-password>@aws-1-eu-west-1.pooler.supabase.com:5432/postgres?sslmode=require
STORAGE_BACKEND=supabase
SUPABASE_URL=https://fctgqpxhufjotwlenjxg.supabase.co
SUPABASE_SECRET_KEY=<private-backend-key>
SUPABASE_BUCKET=document-ingestion
CONFIG_PATH=configs/default.yaml
OPENAI_API_KEY=
DATA_DIR=data
ARTIFACT_DIR=artifacts
REPORT_DIR=reports
```

Use the supplied complete `DATABASE_URL` when available; its password must be
URL-encoded. The database connection and Storage settings must refer to the same
shared project to see its saved results. Choosing a new project starts a separate
library. The Supabase project URL alone is not the PostgreSQL connection string.

For image annotations on new runs, set `CONFIG_PATH=configs/openai.yaml` and fill
`OPENAI_API_KEY` with the separately supplied key. Viewing existing saved chunks
and annotations does not require an OpenAI request.

Create runtime folders, build, and download parser models once:

```powershell
New-Item -ItemType Directory -Force models,data,artifacts,reports
docker compose build api
docker compose run --rm --build download-models
docker compose up -d
```

On Linux/macOS use `mkdir -p models data artifacts reports`. The container runs as
UID/GID 10001; give that user write access to these folders.

The API connects to Supabase PostgreSQL through SQLAlchemy, applies pending
migrations, then starts. Supabase also stores original PDFs and inspection artifacts
in a private bucket. The default Compose setup starts the API; PostgreSQL runs in
Supabase. Docker creates its own Python virtual environment using uv.

## Option 2: Local PostgreSQL

Use this mode to process documents into a database on your own machine. It uses
PostgreSQL 17 in Docker. A fresh local database starts empty; cloud chunks are not
copied automatically. An existing local volume retains its previous results.

From the repository root, create `.env` from `.env.example` if it does not exist.
For local mode use these settings. If switching from Supabase, keep a private copy
of your cloud settings outside Git so you can restore them later.

```dotenv
DATABASE_URL=postgresql+psycopg://rag:rag@localhost:5432/rag
STORAGE_BACKEND=local
SUPABASE_URL=
SUPABASE_SECRET_KEY=
SUPABASE_BUCKET=document-ingestion
CONFIG_PATH=configs/default.yaml
OPENAI_API_KEY=
DATA_DIR=data
ARTIFACT_DIR=artifacts
REPORT_DIR=reports
```

The `rag` username, password and database above are the local development defaults
in `compose.local.yaml`. They are not Supabase credentials. Local Docker supplies
its own `DATABASE_URL` with hostname `db` to the API, and uses local file storage.
The `.env` URL with `localhost` is for Python or database tools running on your host.
Local Compose passes `CONFIG_PATH` and `OPENAI_API_KEY` from `.env` to the API;
Supabase settings are not used in this mode.

If the Supabase API is currently running, stop it first with `docker compose down`.
Then run:

```powershell
New-Item -ItemType Directory -Force models,data,artifacts,reports
docker compose -f compose.local.yaml build api
docker compose -f compose.local.yaml run --rm --build download-models
docker compose -f compose.local.yaml up -d
docker compose -f compose.local.yaml ps
```

Skip model downloading if the model files are already present. The database health
check must pass before the API starts and applies migrations. Open the document
library, upload a PDF and its year, then inspect its saved chunks.

| Connection | Host | Port | Database / user / password |
| --- | --- | --- | --- |
| Browser to API, either mode | `localhost` | `8000` | Not applicable |
| Host database tool to local PostgreSQL | `localhost` | `5432` | `rag` / `rag` / `rag` |
| API container to local PostgreSQL | `db` | `5432` | Set automatically by local Compose |
| API to Supabase PostgreSQL | Host in the supplied `DATABASE_URL` | `5432` for this session pooler | Supplied separately |

Local ports bind to `127.0.0.1`. If port 5432 is occupied, stop the conflicting
service or change the local Compose mapping to `127.0.0.1:5433:5432`. In that case,
host tools and the host `.env` URL use 5433; the API container still uses `db:5432`.

For later local starts, logs and shutdown:

```powershell
docker compose -f compose.local.yaml up -d
docker compose -f compose.local.yaml logs -f api
docker compose -f compose.local.yaml down
```

Chunks persist in the `postgres-data` Docker volume; original PDFs and generated
files persist in the mounted `data`, `artifacts` and `reports` folders. Closing the
terminal or stopping containers does not erase them. `down -v` deletes the local
database volume, so use ordinary `down` to retain results.

To switch back to Supabase, stop local mode with the command above, restore your
private Supabase `.env`, then run `docker compose up -d`. Run one mode at a time:
both use the same API port and Compose project. Switching does not migrate data.

## Batches, progress and checkpoints

Uploading a PDF queues a background job and returns HTTP **202 Accepted** with an
`ingestion_run_id`. The API remains available while one worker processes jobs
sequentially. Each subprocess handles **10 new pages**, plus one preceding context
page when applicable, and exits to release model memory before the next batch.

A durable checkpoint is published after **each completed batch** in selected-pipeline
mode. The older single-pipeline mode publishes every **50 processed pages** and after
the final remaining pages. Each checkpoint makes chunks and batch inspectors available.
The library shows both processed pages and checkpointed pages; these differ until
the next checkpoint. Refreshing or closing the browser does not cancel the worker.

- `GET /v1/runs/{run_id}`: current status, progress and error information.
- `POST /v1/runs/{run_id}/resume`: resume a failed/paused checkpointed job.
- **Resume** keeps the same run, configuration and pipeline selection. It restarts
  the interrupted pipeline after its last committed batch; earlier pipelines remain saved.
- **Reprocess** creates a new run using current settings. Concurrent duplicate
  submissions with the same document/configuration reuse the queued/running job.
- After a container interruption, checkpointed jobs are marked paused; click Resume.
  Older runs created before this feature have no checkpoint and require Reprocess.

Original PDFs and completed batch files are uploaded before checkpoint publication
in Supabase mode. PostgreSQL stores the checkpoint pointer and saved batch references.
Local mode retains the same information in its database volume and mounted folders.
Changes to the saved configuration or model fingerprint require a new run.

Section context is carried between batches and the preceding page supplies parsing
context; its elements are excluded from the new batch to avoid duplicate pages.
Chunks remain bounded by batch boundaries. Tables touching those boundaries are
flagged for review; the implementation does not automatically merge split tables.

The terminal `doc-ingest ingest` command queues a job with the pipeline-comparison profile;
the API coordinator must be running. The older single-pipeline profile is synchronous.
Use the API/library for checkpointed large-document processing. Stop or restart
Docker only when necessary; a restart can discard work since the last checkpoint.

## View and rerun chunks

Open [the document library](http://localhost:8000/). It shows saved documents and jobs,
with links to original PDFs, chunk JSON and HTML inspectors. Upload a PDF and its
year to process it. **Reprocess** creates a new run using the current configuration.
Existing completed runs remain available.

Installations configured with the same Supabase project can view saved cloud results
without parsing again. Local folders cache downloaded files. Access uses the private
backend configuration shared with the evaluator; the localhost address belongs to
the machine running Docker.

For API testing, open [Swagger](http://localhost:8000/docs). Upload with
`POST /v1/documents/ingest` and provide `year`. Poll the returned run ID; a `debug_url`
becomes available after the first checkpoint. Batch inspectors are always saved for jobs. `GET /v1/documents/{document_id}/chunks` returns saved
chunks. Matching completed runs are reused unless `force=true` is supplied.

For later Supabase starts and status checks (local mode uses `-f compose.local.yaml`):

```powershell
docker compose up -d
docker compose ps
docker compose logs -f api
```

Ctrl+C leaves the log viewer. `docker compose down` stops the local API; saved
Supabase results remain. After source changes use `docker compose up --build -d`.
After editing `.env`, use `docker compose up -d --force-recreate api`.
The existing `docker compose -f compose.supabase.yaml ...` commands also work.

## Historical saved-document comparison command

This diagnostic command reuses one saved Docling extraction. It is separate from
the current selectable pipelines, whose topological branch has its own Inspector,
extraction and Refiner. Use [PIPELINES.md](PIPELINES.md) for that flow.

The comparison contains **Hybrid + image context** and **Topology-aware + image
context**. Both use the same document extraction and image context. The
`prepare-document` command performs Docling conversion, figure OCR, filtering
and optional OpenAI annotations, then stops before chunking. It saves native
`raw/docling_document.json`, source `canonical.json`, image files, and an
`enrichment.json` sidecar. The sidecar is application metadata, not a modified
Docling schema. Generated descriptions remain marked unverified.

From the repository root, prepare a new document bundle:

```powershell
uv run --no-sync doc-ingest prepare-document Documents/report_2022.pdf --year 2022 --pages 21 22 23 --config configs/openai.yaml --output artifacts/prepared/report-2022-pages-21-23
```

This command requires the Docling extra and model files. The OpenAI profile makes
paid annotation requests when no matching cache exists. Use `configs/default.yaml`
for conversion without OpenAI annotations. Use an empty output directory.

Compare existing cached runs without repeating OCR, annotations or database writes:

```powershell
uv run --no-sync doc-ingest compare-chunks --source-root artifacts/runs --filename report_2022.pdf --config configs/openai.yaml --output artifacts/comparisons/report-2022
```

Alternatively, compare a prepared bundle:

```powershell
uv run --no-sync doc-ingest compare-chunks --bundles artifacts/prepared/report-2022-pages-21-23 --config configs/openai.yaml --output artifacts/comparisons/prepared-report-2022
```

The prepared-bundle case computes native hybrid groups from saved Docling JSON;
it requires `docling-core` and its chunking dependencies. Cached runs with native
chunks need only the base environment. Overlapping runs are rejected: explicitly
select disjoint run directories with `--bundles` if a PDF has been reprocessed.
Cached native chunks require the same target-token setting as their source run.

The HTML report at the output directory's `index.html` compares:

1. **Hybrid + image context**: native narrative groups plus application table/image
   policy, with figure OCR, captions and unverified image descriptions serialized
   into retrieval text. The current table policy produces row units with repeated headers.
2. **Topology-aware + image context**: the same enriched text, packed in reading order within
   consecutive section occurrences, with explicit parent and figure relationships.

The topology comparison groups elements by section and reading order using fixed
rules. It retains saved batch boundaries.
The two workflows share image enrichment, so the comparison isolates chunking.
Native hybrid preparation time is excluded from the assembly-time comparison.
Source images and native documents remain linked to their saved artifact folders;
keep those folders with the report. JSONL chunks, topology graphs, enriched
document projections and machine-readable metrics accompany the HTML.

Metrics cover token limits, source-reference retention, image attachments,
searchable annotations, token volume and assembly time. They do not establish
answer accuracy. The report explicitly leaves the accuracy winner undetermined.

For a labelled **BM25 lexical retrieval baseline**, pass `--gold path/to/gold.json`.
The JSON is an array of objects with `question`, `verified: true`, and an `evidence`
array. Each evidence item must provide `bundle` (saved directory name),
`element_id` (canonical source ID), and a nonempty exact source `quote`. Check
labels against the original PDF before marking them verified. The evaluator
validates quotes against the canonical extraction, reports hit rate, evidence
recall and MRR, and applies top-5 and 3,000-token context limits. This is not an
embedding benchmark or generated-answer accuracy. A production evaluation must
use representative independently verified questions, the same embedding and
answering models, and assess correctness, grounding, numeric accuracy and citations.

## Image annotations and validation

Select `CONFIG_PATH=configs/openai.yaml` and supply `OPENAI_API_KEY` privately.
Retained image crops, captions, OCR and nearby text are sent to the configured
OpenAI model. Annotations include image type, description, labels, axes, units and
source links. Requests incur API usage. Results are validated and cached.

Inspect `annotation_status` independently of parsing status. Compare descriptions
and numeric values in the HTML inspector against the original PDF. Generated
annotations are marked unverified. The default profile performs parsing with
annotations disabled; the OpenAI profile enables them.

## Local Python development with uv

From the repository root, using Python 3.12 and uv 0.9.10 with the private `.env`:

```sh
uv sync --locked --extra docling --extra dev
uv run --no-sync python scripts/download_models.py
uv run --no-sync doc-ingest migrate
uv run --no-sync uvicorn ingestion.api:create_app --factory --host 127.0.0.1 --port 8000
```

When using local PostgreSQL with host Python, start only the database first:
`docker compose -f compose.local.yaml up -d db`, and use the local `.env` values
shown above. For Supabase, use the supplied cloud `.env` and no local database.

Stop the Docker API first if it already occupies port 8000. Local Python creates
`.venv`; Docker builds a separate environment inside its image.

## Tests

```sh
uv sync --locked --extra dev
uv run --no-sync pytest -q
uv run --no-sync ruff check src tests scripts migrations
uv run --no-sync mypy src
```

Most tests use deterministic parser responses and SQLite. The PostgreSQL integration
test requires `TEST_POSTGRES_URL` pointing to a dedicated migrated test database.

Docker startup, a one-page PDF upload, saved chunks and cloud artifact loading have
been verified. Full-report accuracy and live image annotation quality require
separate review.

Read [Discussion.md](Discussion.md) for design choices and tradeoffs.

### Automatic OCR fallback

Both profiles use `ocr_engine: rapidocr` with `ocr_fallback_engine: tesseract_cli`.
Docling retries the selected page batch once with Tesseract if RapidOCR setup or
conversion raises, or conversion reports a non-success status. A conversion failure
can have causes outside OCR; the retry does not diagnose its cause. If both engines
return partial results, the original partial result and its failure status remain.
A failed retry cannot turn a partial conversion into a successful run.
Retained figure crops also retry with Tesseract when RapidOCR raises. Empty OCR text
does not trigger fallback: images may contain no text. This is failure recovery,
not an automatic quality comparison between engines.

Conversion attempts are stored in `raw/ocr_attempts.json` and document metadata;
figure attempts are stored in asset signals. Set `ocr_fallback_engine: null` to
disable retries. Selecting Tesseract as primary runs it once without cycling back.
Rebuild Docker to install the new runtime dependencies. Local runs require the
`tesseract` executable on PATH and English `eng` traineddata. Existing cached
artifacts are unchanged; changed configuration fingerprints apply to new runs.


### Image classification and chunk context enrichment

The OpenAI profile (`configs/openai.yaml`) now enables two distinct enrichment steps:

1. Every detected image crop reaches OpenAI with its caption, OCR, nearby text and
   section. Geometry/repetition are signals, not exclusion rules. A strict structured
   response describes the image and classifies its purpose. Only clearly decorative,
   non-substantive regions without stated uncertainty are excluded. Substantive/mixed
   images are retained; failures and ambiguity remain `review_required`. Original crops,
   descriptions, reasons and provenance remain in canonical JSON and the HTML inspector.
2. After chunking in each selected pipeline, OpenAI generates a short explanation
   of how each chunk relates to the available document content.
   The generated prefix is stored separately as `contextual_text` and prepended to
   `retrieval_text`; the original `text`, source spans and IDs remain unchanged.

The final retrieval text is intended for BOTH embeddings and BM25. This application
still does not build an embedding index or production BM25 index. The comparison's
optional BM25 evaluation consumes the contextualized retrieval text.

`contextual_reserve_tokens: 128` reserves space within the existing 700-token ceiling,
leaving a 572-token cap for source plus existing metadata/image context. The final
combined text is counted again with the configured embedding tokenizer. Oversized
source rows, invalid responses and overlong generated context are flagged; original
source is never silently truncated. Failed enrichment preserves the original retrieval
text and records failure in chunk provenance and scorecards.

Context generation uses all eligible content in the current document bundle when it
fits `contextual_max_document_chars` (default 60000). Otherwise it selects same-section
and nearby elements and records that scope. A ten-page batch is NOT the whole PDF:
`available_pages` and `complete_document_available` record the available scope.
Supporting IDs are validated, but generated claims remain unverified.

Responses are cached by input, model and policy. Configuration fingerprints and the
image classification cache version have changed; existing artifacts are not rewritten.
The default offline profile leaves OpenAI disabled and retains unclassified images.
Use `configs/openai.yaml` with `OPENAI_API_KEY` for the new processing. In particular,
`compare-chunks --config configs/openai.yaml` now makes paid text-context requests;
it reuses saved image decisions and does not rerun image classification. Prepare a
new document bundle with the OpenAI profile first to obtain the new image decisions.
Set `contextual_enrichment_enabled: false` for an offline chunk comparison.

Run after rebuilding the application image or updating the local code:

```powershell
uv run doc-ingest prepare-document Documents/report_2025.pdf --year 2025 --pages 7 52 --config configs/openai.yaml --output artifacts/prepared/report-2025-vision-v2
uv run doc-ingest compare-chunks --bundles artifacts/prepared/report-2025-vision-v2 --config configs/openai.yaml --output artifacts/comparisons/report-2025-context-v1
```

Inspect exclusions and generated context before measuring retrieval improvement.
Retrieval improvement must be established through evaluation on these documents.


### Uploading multiple documents and inspecting selected pipelines

The document library accepts any PDF reports, not only the four assessment files.
Select up to 50 PDFs, review/enter a year for each, and choose **Queue documents**.
The server validates and queues each document independently and reports accepted
and rejected files separately. A bad PDF does not discard the other uploads.
Duplicate documents with the same year/configuration reuse their existing run.

The new `POST /v1/documents/batch` endpoint accepts repeated multipart `files` fields
and a `years` field containing a JSON integer array in matching order. The original
single-document endpoint is preserved. Supply `pipelines` as a JSON array of
`docling` and/or `topology` to select workflows. The HTML sends the
checkbox selection. Without a selection, the server profile determines execution.

The local coordinator processes documents sequentially. It completes all batches of
the first selected pipeline, then the next selected pipeline, before starting the
next PDF. Each pipeline performs its own extraction. Completed batches are saved;
the final comparison is assembled from saved results after all selected pipelines finish.

Open the pipeline comparison in the library. The selected document's comparison
appears below the document list, with one column per selected pipeline.
Metrics and evaluation criteria appear BELOW the chunks. Each report retains its
filename, year, source pages, image decisions and section headings. The heading list
is source-derived context, not an AI-written whole-document summary.

Each selected pipeline saves its ingestion chunks and metadata in the relational
database, plus inspection artifacts in the configured storage. Metrics describe structural checks;
retrieval/answer accuracy remains unmeasured without a labelled evaluation set.
Rebuild/restart the local application to load the updated API, UI and worker. Existing
HTML artifacts are preserved and do not acquire the new layout retroactively.


## Execution, persistence and future parallel processing

See [OPERATIONS.md](OPERATIONS.md) for exact Docker startup,
sequential CPU execution, Supabase database versus Storage versus local caches,
checkpoint/recovery behavior, the code reading guide and the planned CPU/GPU scaling
work. Adding a GPU or more API replicas does not currently enable parallel ingestion.
The same guide distinguishes implemented chunking from planned retrieval and explains
the remaining live-verification steps.
