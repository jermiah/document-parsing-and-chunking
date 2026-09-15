"""An explicit non-splitting IngestionPipeline, independent of keys and embedding models."""

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from llama_index.core.ingestion import IngestionPipeline
from llama_index.core.schema import (
    BaseNode,
    NodeRelationship,
    RelatedNodeInfo,
    TextNode,
    TransformComponent,
)

from ingestion.schemas import Chunk


class ValidateCanonicalNode(TransformComponent):
    def __call__(self, nodes: Sequence[BaseNode], **kwargs: Any) -> list[BaseNode]:
        for node in nodes:
            if not node.metadata.get("run_id") or not node.metadata.get("element_ids"):
                raise ValueError("Canonical nodes require run and source element provenance")
        return list(nodes)


def prepare_nodes(
    chunks: list[Chunk], document_id: str, run_id: str, cache_dir: Path
) -> list[BaseNode]:
    nodes = []
    for chunk in chunks:
        node = TextNode(
            id_=f"{run_id}:{chunk.id}",
            text=chunk.retrieval_text,
            metadata={
                "document_id": document_id,
                "run_id": run_id,
                "chunk_id": chunk.id,
                "element_ids": chunk.element_ids,
                "asset_ids": chunk.asset_ids,
                "start_page": chunk.start_page,
                "end_page": chunk.end_page,
            },
        )
        # Context has already been serialized and counted by the application exactly once.
        node.excluded_embed_metadata_keys = list(node.metadata)
        node.excluded_llm_metadata_keys = list(node.metadata)
        if chunk.parent_id:
            node.relationships[NodeRelationship.PARENT] = RelatedNodeInfo(
                node_id=f"{run_id}:parent:{chunk.parent_id}"
            )
        nodes.append(node)
    pipeline = IngestionPipeline(transformations=[ValidateCanonicalNode()])
    if (cache_dir / "llama_ingest_cache.json").exists():
        pipeline.load(str(cache_dir))
    result = pipeline.run(nodes=nodes)
    cache_dir.mkdir(parents=True, exist_ok=True)
    pipeline.persist(str(cache_dir))
    return list(result)
