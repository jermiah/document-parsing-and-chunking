# Design discussion

## Two workflows and two chunking strategies

Both workflows use Docling + RapidOCR with Tesseract fallback, extracted image crops,
OpenAI image classification/annotation, deterministic filtering, and chunk context.

| Workflow | Chunking | Output |
| --- | --- | --- |
| Hybrid + image context | Hybrid grouping and token/table policy | Chunks with source and image context |
| Topological + image context | Inspector audit profile, SIR hierarchy, Refiner and capacity audit | Chunks with source lineage and image context |

One worker completes Hybrid batches, then Topological batches for each queued document.
Each currently repeats extraction with the same settings; extracted artifacts are not
shared between workflows. The HTML displays both chunk sets and comparison metrics.
The topological parser uses local Docling/OCR; OpenAI receives figure crops and
extracted text, never full pages for primary extraction. SIR queries are batch-scoped.
See [PIPELINES.md](PIPELINES.md) for implementation details and limits.

### Evaluation before RAG selection

There are two chunking strategies: Hybrid and
agentic topological. Compare parser accuracy separately from chunk boundary quality.
Counts, token distributions, image retention, failures and processing time are
mechanical measurements; they do not establish retrieval accuracy.

First label representative text, tables and figures, then build verified questions
with source evidence. Evaluate each pipeline independently before testing a combined
retriever. The workflows use the same extraction settings. A comparison using identical saved
extraction further isolates chunking quality from extraction variability. The planned dense
and BM25 retrieval, deduplication, cross-encoder reranking and optional ColPali branch
below remain future experiments. We will select or combine representations based on
measured accuracy, latency and cost rather than assume that more branches are better.

## Comparison criteria

| Criterion | What to examine on the same source pages |
| --- | --- |
| Text | Missing paragraphs, characters, numbers and reading order |
| Tables | Headers, merged cells, row alignment, units and numeric accuracy |
| Images | Retained figures, excluded decoration and correct text associations |
| Provenance | Correct page numbers, positions and source-element references |
| Chunks | Coherent boundaries, table continuity, token budgets and image limits |
| Annotations | Descriptions supported by the visible figure; uncertainty and failures |
| Operations | Processing time, hardware, model/service requirements and failure rate |

Counts and speed alone do not establish quality. Review representative text-heavy,
scanned, chart-heavy and table-heavy pages, record observations in the notebook, and
only then justify a preferred approach. No measured superiority is claimed yet.

## ColPali: whether to use it for this assessment

**ColPali is not implemented here.** It is a visual document retrieval model rather
than a replacement for the required structured parser. It embeds rendered pages as
multiple vectors and matches text queries through late interaction. This can exploit
visual cues that extracted text misses.

For this assessment, my engineering choice is to retain structured ingestion and
explain ColPali as a future retrieval extension. The required deliverables are extracted
content, chunks, relational metadata and an inspector; ColPali alone would not produce
those deliverables. It also does not create image descriptions or answer questions.

| Without ColPali: current implementation | With ColPali: possible extension |
| --- | --- |
| Parse and inspect text, tables, images and chunks | Keep that pipeline and add visual page indexing |
| No query ranking | Encode a query and rank page representations |
| Relational metadata and disk artifacts | Additional multi-vector storage and compatible scoring |
| Inspect explicit source elements and associations | Retrieve relevant pages, then link them to stored elements/chunks |

The potential advantage is finding relevant charts, tables and layouts even when OCR
or text serialization loses useful visual context. The costs are added inference,
vector storage, scoring infrastructure and retrieval evaluation. These benefits would
matter if the assignment expanded to visual search; they are not measured improvements
to the current extraction/chunking task. A future design could combine ColPali page
retrieval with either parser's structured evidence.

## Chunk size and multiple images

Respect headings, paragraphs and table rows before applying token limits. The BGE-M3
tokenizer counts chunk text and context prefixes for consistent chunk budgets.
Preserve table headers and flag indivisible oversized rows. Limit a chunk to two
images; larger image groups become related sibling chunks. Keep page and source-element
references for inspection. This structure-aware hybrid chunking preserves document context within token budgets.

## Image storage, annotation and context

Store original PDFs and content-addressed image crops on disk, with geometry, hashes
and associations in PostgreSQL for application runs. Crops can be regenerated from
the original. Optional object storage is also supported; its operational verification is described below.
Geometry and repetition are supporting signals, not sufficient grounds for exclusion.
When OpenAI is enabled, each detected crop receives contextual classification and
annotation. Clearly decorative, non-substantive regions without stated uncertainty
can be excluded regardless of size; substantive/mixed content and known charts are
protected. Ambiguous images or failed classifications remain available for review.
Without vision classification, unclassified assets remain available.
OpenAI annotations add descriptions, image types and suggested text links;
they remain unverified generated content, separate from captions, OCR and source pixels.
Validate charts against visible labels, numbers and units.

## Validation and limitations

Automated tests verify software contracts using small fixtures and simulated model
responses; the optional PostgreSQL test needs a dedicated test database. A real
one-page API ingestion has been verified. Full-report parsing, live annotation
quality and the live two-workflow comparison still require execution and visual
review. No human-labeled accuracy result
is claimed in this submission.

## Shared review workflow

The API home page lists saved documents and runs, opens inspectors and original PDFs,
and supports upload/reprocessing. Local mode retains disk and Docker PostgreSQL
persistence. Optional Supabase mode uses shared PostgreSQL records and a private
Storage bucket for originals and run artifacts; other configured installations
fetch missing files into their cache. Uploads complete before a successful run is
published. The notebook remains a separate experiment and does not write to this
database. The cloud database connection and a real one-page ingestion have been verified.
A second API instance with empty local folders loaded the original, inspector,
supporting images and chunk from the cloud without reparsing. Full-report quality
and live image-annotation accuracy still require separate review.


## Planned RAG: accuracy first, with measured latency and cost

My plan is to evaluate a retrieval ensemble before deciding which approach to keep.
Answer correctness, supporting evidence and accurate citations take priority over
latency for this use case. This does not mean that more retrieval branches or model
calls are automatically better. I will retain additional complexity only when its
measured benefit justifies the latency, cost and maintenance burden.

### What exists and what remains experimental

Two workflows produce candidate representations: Hybrid and Inspector/SIR/Refiner
topological chunks, both using Docling/RapidOCR extraction.
They share image classification, annotation and generic contextual enrichment but
have independent extraction. Their boundaries and source recognition can differ.
Multiple representations of the same PDF are not independent corroborating sources.

The OpenAI profile also generates chunk-specific contextual prefixes. Original chunk
text and provenance remain separate. The current context can cover a saved page batch
rather than the complete PDF; oversized inputs use selected sections and nearby
content, and that scope is recorded. The explanatory context is prepended to the
retrieval text intended for embedding and BM25 indexing. Its effect on retrieval
quality will be measured during evaluation.

The current application accepts multiple PDFs with explicit years, runs two
pipelines sequentially in page batches, and shows their saved chunks above the
comparison metrics. Each pipeline uses relational ingestion records and saved
JSONL/HTML artifacts. Dense/BM25 production indexes, cross-encoder reranking, query
planning, answer generation, ColPali and monitoring remain planned, not benchmarked.
The optional BM25 evaluation command remains an offline baseline.

### Selecting representations, then dense and lexical retrieval

After evaluating the two workflows, I propose testing a combined Hybrid/topological
retriever, choosing the Hybrid extraction backend from those results. Each uses:

- **Dense retrieval:** semantic matching between query and chunk embeddings.
- **BM25:** lexical matching for names, identifiers, specific terminology and other
  exact terms that semantic retrieval may miss.

Docling's HybridChunker describes chunk construction; hybrid retrieval describes
combining dense and lexical search. They are separate choices. Each index should use
contextualized retrieval text, while original source passages and images remain
available for grounding. Use the same embedding model and compatible query/document
encoding for the controlled comparison. A workflow tag can separate representations
within one vector store; two physical databases are not required.

```text
Question + conversation context
  -> Standalone question, preserving entity/year/scope constraints
  -> Original question + useful subqueries
       |-> Hybrid chunks: dense search + BM25
       |-> Topology-aware chunks: dense search + BM25
  -> Rank fusion + source-aware exact deduplication
  -> Cross-encoder reranking against subqueries and original question
  -> Evidence coverage check; targeted follow-up only for unresolved needs
  -> Remove redundant overlap and select evidence within the answer context budget
  -> Grounded answer using source text, relevant images and page citations
```

I will start with reciprocal rank fusion (RRF) to combine result rankings, instead
of adding raw BM25 and vector scores with incompatible scales. Branch contributions
must remain traceable. Rephrasing the same query many times must not give that evidence
unlimited extra weight: combine variants within each information need and keep branch
weights fixed during an experiment. RRF is a starting choice to evaluate, not a claim
that it is optimal.

### Query condensation, decomposition and bounded retrieval rounds

I distinguish three operations:

1. **Condensation:** turn a conversational follow-up into a standalone question.
2. **Decomposition:** split a multi-part question into distinct evidence needs.
3. **Expansion:** use alternative wording for one evidence need.

For example, comparing revenue in two years and explaining the change creates three
needs: the first year's figure, the second year's figure, and the explanation. The
original question remains in the search plan so decomposition does not drop constraints.
Simple factual questions need not be decomposed. Independent subqueries can run in
parallel with bounded concurrency; dependent subqueries run after the evidence they
need has been obtained. Local ingestion can remain sequential independently of this
query-time execution policy.

My initial experimental limits are at most three subqueries plus the original question,
one initial round, and up to two targeted follow-up rounds. These are tuning defaults,
not requirements or validated optima. Four queries across two representations and two
retrieval methods already produce 16 searches per round. Further rounds should address
a specific missing fact, ambiguous unit, unresolved reference or source conflict.

Stop when required evidence is covered, a round adds no useful evidence, or the
configured round/time/cost budget is reached. If evidence remains insufficient, state
the limitation or abstain. An evaluator's confidence is not proof of correctness;
iterative workflows require clear criteria and demonstrated incremental value.

### Deduplication, reranking and evidence validation

Exact deduplication should use document identity/version, year, page, source-element
IDs, text spans or table cells, and image references. IDs must be scoped to their
source document/batch. Different generated contextual prefixes do not make identical
source evidence unique. Preserve retrieval origins when merging duplicates.

Partial overlap is different: a larger chunk may add an explanation or a related
figure. Keep such candidates until relevance is assessed, then reduce redundant
content in the final context. Never merge similar passages across years just because
their wording matches. Apply year filters only when warranted by the question; a
cross-year question must retain all relevant reports.

A cross-encoder reads the question and candidate together. Rerank against each
subquery to preserve evidence for every part, then assess the final evidence set
against the original question. Do not let a global top-k remove all support for one
required subquestion. The final selection also observes a token budget and image
budget, rather than an arbitrary fixed chunk count alone.

Runtime checks assess coverage, source support, entities, years, units and contradictions.
They can trigger a follow-up but do not measure true accuracy without reference labels.
Generated annotations and contextual prefixes must not become substitutes for original
text, tables or pixels. A text-only cross-encoder sees image descriptions, not their
pixels; visual questions need visual evidence verification. A reranker also cannot
recover evidence absent from the candidate set.

### ColPali as a future visual retrieval branch

In addition to the text-based ensemble, I will evaluate **ColPali** as an independent
visual-page retrieval branch and as a standalone retrieval baseline. It encodes page
images into multiple vectors and matches text queries through late interaction.
This can surface tables, figures and layout information lost during text extraction.
It does not eliminate the assignment's need for structured ingestion and inspectable
source records.

The potential extension is:

```text
Question -> text ensemble -> ranked chunk candidates
         -> ColPali       -> ranked page candidates
                  -> source mapping, evidence selection and grounded answer
```

Map page candidates back to document/year/page IDs and retain the page image. Compare
at the source-evidence/page level, rather than treating a whole page and a short chunk
as equivalent units. Use rank-based fusion or separately calibrated scores; ColPali
scores are not directly comparable with BM25 or dense scores. A text cross-encoder
cannot faithfully rerank a pixel-only candidate without a textual representation.
Test multimodal reranking, or preserve a bounded visual candidate allocation for the
answering vision model, and evaluate that choice separately.

Experiments should compare ColPali alone, the text ensemble alone, and their combination.
Selective visual routing for chart/table questions is another candidate if always-on
visual retrieval adds substantial latency with little benefit for ordinary text queries.
Costs to measure include page rendering, multi-vector indexing/storage, device memory,
query scoring and visual-model calls. Rendering full pages also includes decorations
excluded from the text pipeline; assess whether this affects relevance. No visual
retrieval advantage on these uploaded documents is assumed in advance.

## Evaluation and monitoring plan: approximately 1,000 retrieval requests

By 1,000 retrievals I mean approximately **1,000 user-question requests**, not 1,000
retriever implementations or 1,000 internal search calls. One request may produce many
branch searches and multiple rounds. This is a proposed experiment after retrieval
is implemented, not a running monitor, scheduled job or completed benchmark.

### Experimental design

Build representative questions spanning direct facts, paraphrases, tables/numbers,
charts, cross-section reasoning, cross-document/year comparisons, conversational
follow-ups and unanswerable questions. Include arbitrary uploaded document types,
not only the four assessment reports. Report results by these categories as well as
in aggregate. Repeated near-identical queries must not dominate the evaluation.

Use an initial 200 requests for development and rubric calibration, then freeze models,
prompts, chunk versions, candidate budgets, fusion weights and thresholds for the next
800 held-out requests. Keep related questions/documents grouped where possible to
reduce leakage. This split is a starting plan; 1,000 requests does not itself guarantee
statistical confidence or enough examples in every category. Extend collection if
important slices remain too small or differences are inconclusive.

Replay eligible requests against the candidate configurations in shadow evaluation.
Only the chosen primary system supplies the user-facing response. Pin the same corpus
snapshot, filters, answer model and prompt. For text variants, use the same embedding
model and reranker. Compare under equal final evidence-token budgets, and also run an
equal-total-candidate comparison so extra searches are not mistaken for better strategy.
For visual comparisons, additionally report image count, resolution and vision-input
cost; a page image is not equivalent to a text token budget.

| Experiment | Question it answers |
| --- | --- |
| Hybrid chunks: dense + BM25 + cross-encoder | How strong is the first single-workflow baseline? |
| Topology-aware chunks: dense + BM25 + cross-encoder | Does the second representation help on its own? |
| Both chunk sets + fusion/deduplication, without reranking | What does candidate combination contribute? |
| Both chunk sets + fusion/deduplication + cross-encoder | Does the proposed ensemble beat the best single workflow? |
| Ensemble + decomposition | Does breaking down questions improve coverage? |
| Ensemble + decomposition + targeted follow-up rounds | Do additional rounds resolve gaps and improve final answers? |
| Best single workflow with a larger candidate pool | Could increased retrieval depth provide the same gain more simply? |
| ColPali alone / ensemble plus ColPali | What is the incremental value of visual retrieval? |

Use smaller development experiments to shortlist feasible configurations rather than
running every expensive alternative indefinitely. Evaluate contextual enrichment on/off
as a separate controlled comparison. Keep the final held-out set untouched by tuning.

### Ground truth and separate metric layers

Reference labels should identify verified answers and supporting source spans, table
cells or page regions, not a particular chunker's IDs. Manually review the test set's
reference evidence. Automated checks help with exact numeric values, units, years and
citations. A model judge can assist with a fixed rubric after calibration against
human judgments, but cannot be the only authority. Audit disagreements and keep
judge-model/prompt versions. User satisfaction signals are useful but are not ground truth.

| Layer | Metrics and interpretation |
| --- | --- |
| Extraction / image filtering | OCR and table correctness on reviewed samples; exclusion precision, unwanted-image recall and useful-image retention |
| Chunking | Source coverage, boundaries, token compliance, image associations and overlap; these are not answer accuracy |
| Query planning | Coverage of necessary information needs; omitted constraints, irrelevant subqueries and decomposition rate |
| Candidate retrieval | Evidence Recall@k and recall within a fixed evidence budget; relevance judgments for precision and nDCG/MRR where applicable |
| Branch contribution | Unique relevant evidence supplied by each branch; gain/loss when that branch is removed |
| Iterative retrieval | Newly covered evidence needs per round, zero-gain rounds, repeated searches and stop-reason distribution |
| Final answer | Correctness, completeness, numeric/unit/year accuracy, citation support, unsupported-claim rate and appropriate abstention |
| Efficiency / reliability | End-to-end and stage p50/p95 latency; tokens, calls, cost/request, index size, indexing cost, timeouts and failures |

Log a request/experiment ID, corpus and configuration versions, original/standalone
question, subqueries and dependencies, filters, branch ranks/scores, source identities,
deduplication decisions, reranker output, evidence selected, retrieval rounds and stop
reason, answer/citations, timings, usage and evaluation labels. Store query/source text
only under the application's retention and access policy; never log API credentials.
Distinguish cache hits and misses, warm and cold runs, and failures from successful
latency measurements. Keep failed requests in reliability and end-to-end quality
reporting instead of silently dropping them.

### How I will score and select an approach

Accuracy is the primary objective. I will first require acceptable grounding, numeric
accuracy and reliability, then compare quality improvements against the agreed p95
latency and cost ceilings. Those ceilings and minimum meaningful quality gains must
be set before the held-out evaluation; they are product choices, not universal values.

For a readable experimental grade, an initial proposal is:

`Quality grade = 100 × (0.60 × answer correctness + 0.20 × evidence recall + 0.20 × citation support)`

Each component is measured on a 0–1 scale using a fixed rubric. For unanswerable
questions, report correct abstention separately and leave evidence recall undefined
rather than inventing a denominator. Compute the combined grade on answerable
questions and retain the full per-category scorecard. The weights are provisional and
must be frozen before held-out testing. Do not use a reranker score or the model's
self-confidence as answer correctness. The grade is a summary, not a way to hide a
critical regression or offset hallucinations with fast responses.

Report paired differences on the same requests with confidence intervals, grouped by
related question/document where appropriate. Inspect quality versus p95 latency and
cost as separate axes. Avoid declaring a winner from small numerical differences or
repeatedly tuning on interim held-out results.

After the planned evaluation window:

- **Retain the ensemble** if it produces a meaningful, consistent quality gain over
  the best single workflow within operational limits.
- **Retire a redundant branch** if it adds little unique relevant evidence and no
  reliable answer-quality gain while increasing latency or cost.
- **Route selectively** if topology-aware context, extra rounds or ColPali help only
  certain question types. Evaluate the router itself; routing mistakes can lose recall.
- **Keep collecting evidence** if results are inconclusive; do not force a winner
  because the request counter reached 1,000.

Keep retired variants reproducible for regression checks and rollback. After choosing
a default, continue monitoring category-level quality, failures, latency and cost, and
re-evaluate after material changes to documents, models or chunking policies. This is
how I intend to move from an experimental accuracy-first ensemble to a justified,
maintainable retrieval architecture.


## Execution and scaling reference

The topological workflow runs its own extraction, builds a section tree and refines
chunks using source evidence. The saved-extraction comparison uses a separate
deterministic grouping helper. See [PIPELINES.md](PIPELINES.md) for the execution
flow and current limits. Image annotation adds descriptions alongside these stages.

The current coordinator processes document jobs sequentially with bounded page batches.
This is a deliberate memory and recovery policy; CPUs can also execute independent
jobs concurrently when RAM and thread budgets allow it. A future GPU can accelerate
supported model stages, but both hardware acceleration and bounded worker concurrency
need explicit configuration and implementation. See [OPERATIONS.md](OPERATIONS.md)
for the current execution order, persistence boundaries, glossary, code map and scaling
plan. The present chunking/HTML implementation and the experimental query-time RAG
architecture above have separate completion and evaluation criteria.
