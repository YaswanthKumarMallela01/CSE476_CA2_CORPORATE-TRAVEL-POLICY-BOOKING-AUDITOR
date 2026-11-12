"""
Policy ingestion and RAG retrieval using ChromaDB and local sentence-transformers.
Each clause is stored as a separate chunk with clause_id metadata.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

import chromadb
from chromadb.config import Settings

logger = logging.getLogger(__name__)

# Lazy-loaded embedding function
_embed_fn = None


def _get_embed_fn(model_name: str = "all-MiniLM-L6-v2"):
    """Get or create the embedding function (Chroma ONNX all-MiniLM-L6-v2 or SentenceTransformer)."""
    global _embed_fn
    if _embed_fn is None:
        try:
            from chromadb.utils.embedding_functions import DefaultEmbeddingFunction
            _embed_fn = DefaultEmbeddingFunction()
            logger.info("Loaded Chroma ONNX embedding model (all-MiniLM-L6-v2)")
        except Exception:
            from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction
            _embed_fn = SentenceTransformerEmbeddingFunction(model_name=model_name)
            logger.info("Loaded SentenceTransformer embedding model: %s", model_name)
    return _embed_fn


def parse_policy_clauses(policy_path: Path) -> list[dict[str, str]]:
    """Parse travel_policy.md into a list of {clause_id, text} dicts.
    
    Each H2 heading (## TP-X.X ...) starts a new clause.
    """
    content = policy_path.read_text(encoding="utf-8")
    clauses: list[dict[str, str]] = []
    # Split on H2 headings
    sections = re.split(r"(?m)^## ", content)
    for section in sections:
        section = section.strip()
        if not section:
            continue
        lines = section.split("\n", 1)
        heading = lines[0].strip()
        body = lines[1].strip() if len(lines) > 1 else ""
        # Extract clause ID from heading (e.g., "TP-1.1 Advance Booking")
        match = re.match(r"(TP-[\d.]+)\s+(.*)", heading)
        if match:
            clause_id = match.group(1)
            clause_title = match.group(2)
            full_text = f"{clause_id} {clause_title}: {body}"
            clauses.append({"clause_id": clause_id, "text": full_text})
    logger.info("Parsed %d policy clauses", len(clauses))
    return clauses


class PolicyRAG:
    """RAG retrieval over the travel policy using ChromaDB."""

    def __init__(
        self,
        persist_dir: str = "./chroma_db",
        embedding_model: str = "all-MiniLM-L6-v2",
        top_k: int = 5,
    ) -> None:
        self.top_k = top_k
        self.embed_fn = _get_embed_fn(embedding_model)
        self.client = chromadb.Client(Settings(
            anonymized_telemetry=False,
            is_persistent=True,
            persist_directory=persist_dir,
        ))
        self.collection = self.client.get_or_create_collection(
            name="travel_policy",
            embedding_function=self.embed_fn,
        )
        self._clause_map: dict[str, str] = {}

    def ingest(self, policy_path: Path) -> None:
        """Ingest policy clauses into ChromaDB. Idempotent."""
        clauses = parse_policy_clauses(policy_path)
        if self.collection.count() >= len(clauses):
            logger.info("Policy already ingested (%d docs), skipping.", self.collection.count())
            # Still build the clause map
            for c in clauses:
                self._clause_map[c["clause_id"]] = c["text"]
            return
        # Clear and re-ingest
        # Delete existing
        existing = self.collection.get()
        if existing["ids"]:
            self.collection.delete(ids=existing["ids"])
        ids = []
        documents = []
        metadatas = []
        for c in clauses:
            ids.append(c["clause_id"])
            documents.append(c["text"])
            metadatas.append({"clause_id": c["clause_id"]})
            self._clause_map[c["clause_id"]] = c["text"]
        self.collection.add(ids=ids, documents=documents, metadatas=metadatas)
        logger.info("Ingested %d policy clauses into ChromaDB", len(clauses))

    def retrieve(self, query: str, top_k: Optional[int] = None) -> list[dict]:
        """Retrieve top-k clauses matching a query.
        
        Returns list of {clause_id, text, score}.
        """
        k = top_k or self.top_k
        results = self.collection.query(query_texts=[query], n_results=min(k, self.collection.count()))
        items = []
        for i in range(len(results["ids"][0])):
            clause_id = results["metadatas"][0][i]["clause_id"]
            items.append({
                "clause_id": clause_id,
                "text": results["documents"][0][i],
                "score": 1.0 - (results["distances"][0][i] if results["distances"] else 0),
            })
        return items

    def multi_query_retrieve(self, queries: list[str], top_k: Optional[int] = None) -> list[dict]:
        """Run multiple queries, merge and deduplicate results by clause_id (keep best score)."""
        seen: dict[str, dict] = {}
        for q in queries:
            for item in self.retrieve(q, top_k):
                cid = item["clause_id"]
                if cid not in seen or item["score"] > seen[cid]["score"]:
                    seen[cid] = item
        # Sort by score descending
        return sorted(seen.values(), key=lambda x: x["score"], reverse=True)

    def get_clause_text(self, clause_id: str) -> str:
        """Direct clause lookup (for ablation / fallback)."""
        return self._clause_map.get(clause_id, "")

    def get_all_clauses(self) -> dict[str, str]:
        """Return all clause texts keyed by ID."""
        return dict(self._clause_map)
