"""
Policy Retrieval Agent — RAG over the travel policy using ChromaDB.

Generates multiple retrieval queries from the parsed request,
retrieves and merges clause matches, with optional LLM re-ranking.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from core.memory import MemoryStore
from core.rag import PolicyRAG
from core.schemas import ParsedRequest, RetrievedClause, RetrievedPolicy

logger = logging.getLogger(__name__)


class PolicyRetrievalAgent:
    """Retrieves relevant policy clauses for a travel request.
    
    Memory interactions:
    - Reads: prior decisions for the employee
    - Writes: retrieved clause IDs to audit log
    """

    def __init__(
        self,
        memory: MemoryStore,
        policy_rag: PolicyRAG,
        use_llm_rerank: bool = True,
    ) -> None:
        self.memory = memory
        self.rag = policy_rag
        self.use_llm_rerank = use_llm_rerank

    async def retrieve(self, parsed: ParsedRequest) -> RetrievedPolicy:
        """Retrieve relevant policy clauses for the parsed request.
        
        1. Generate 3-5 retrieval queries based on request attributes
        2. Multi-query retrieve from ChromaDB
        3. Optionally re-rank with LLM
        4. Return clauses with scores
        """
        # Read prior decisions from memory
        prior_trips = self.memory.get_trips_for_employee(parsed.employee_id)
        self.memory.write_audit_log(
            parsed.request_id, "policy_retrieval", "read_prior_trips",
            json.dumps({"employee_id": parsed.employee_id, "prior_trip_count": len(prior_trips)}),
        )

        # Generate retrieval queries
        queries = self._generate_queries(parsed)
        logger.info("Generated %d retrieval queries for %s", len(queries), parsed.request_id)

        # Multi-query RAG retrieval
        raw_results = self.rag.multi_query_retrieve(queries)

        # Build RetrievedClause objects
        clauses = [
            RetrievedClause(
                clause_id=r["clause_id"],
                text=r["text"],
                score=r["score"],
            )
            for r in raw_results
        ]

        # Optional LLM re-rank
        if self.use_llm_rerank and len(clauses) > 3:
            try:
                clauses = await self._llm_rerank(parsed, clauses)
            except Exception as e:
                logger.warning("LLM re-rank failed, using raw scores: %s", e)

        # Determine applicable clause IDs (all retrieved with score above threshold)
        applicable = [c.clause_id for c in clauses if c.score > 0.2]

        result = RetrievedPolicy(clauses=clauses, applicable_clause_ids=applicable)

        # Write to memory
        self.memory.write_audit_log(
            parsed.request_id, "policy_retrieval", "retrieved_clauses",
            json.dumps({"clause_ids": applicable, "total_retrieved": len(clauses)}),
        )

        return result

    def _generate_queries(self, parsed: ParsedRequest) -> list[str]:
        """Generate 3-5 targeted retrieval queries from the request."""
        queries = []

        # 1. Travel class query
        if parsed.requested_class.value != "economy":
            queries.append(f"business class premium economy travel class grade employee policy")
        else:
            queries.append(f"economy class fare cap domestic international")

        # 2. Fare cap query
        queries.append(f"fare cap {'international' if parsed.is_international else 'domestic'} {parsed.requested_class.value} maximum")

        # 3. Advance booking query
        queries.append(f"advance booking days notice {'international' if parsed.is_international else 'domestic'} travel")

        # 4. Purpose query
        queries.append(f"business purpose travel eligible personal leisure")

        # 5. Budget and frequency
        queries.append(f"department budget trip frequency quarter limit approval")

        return queries

    async def _llm_rerank(
        self, parsed: ParsedRequest, clauses: list[RetrievedClause]
    ) -> list[RetrievedClause]:
        """Use LLM to re-rank clauses by relevance to the request."""
        from llm_factory import llm_json_call

        clause_texts = "\n".join(
            f"- {c.clause_id}: {c.text[:100]}..." for c in clauses
        )
        prompt = (
            f"Given a travel request: {parsed.employee_id}, "
            f"{parsed.origin_iata}->{parsed.destination_iata}, "
            f"class={parsed.requested_class.value}, "
            f"purpose={parsed.purpose_text}, "
            f"days_ahead={parsed.days_ahead}, "
            f"international={parsed.is_international}\n\n"
            f"Rank these policy clauses by relevance (most relevant first):\n{clause_texts}\n\n"
            f"Return a JSON array of clause_ids in order of relevance, e.g. [\"TP-2.1\", \"TP-2.2\"]"
        )

        result = await llm_json_call(prompt, schema_hint="rerank_v1")
        if isinstance(result, list):
            # Re-order clauses
            order = {cid: i for i, cid in enumerate(result)}
            clauses.sort(key=lambda c: order.get(c.clause_id, len(order)))
        return clauses
