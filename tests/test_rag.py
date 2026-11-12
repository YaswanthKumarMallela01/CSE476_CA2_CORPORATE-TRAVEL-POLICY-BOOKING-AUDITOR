"""Tests for the RAG pipeline (policy ingestion and retrieval)."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.rag import PolicyRAG, parse_policy_clauses

POLICY_PATH = Path(__file__).parent.parent / "data" / "policy" / "travel_policy.md"


class TestParseClauses:
    def test_parse_all_clauses(self):
        clauses = parse_policy_clauses(POLICY_PATH)
        assert len(clauses) == 8
        ids = [c["clause_id"] for c in clauses]
        assert "TP-1.1" in ids
        assert "TP-2.1" in ids
        assert "TP-2.2" in ids
        assert "TP-4.1" in ids
        assert "TP-5.1" in ids
        assert "TP-6.1" in ids
        assert "TP-7.1" in ids
        assert "TP-9.1" in ids

    def test_clause_text_contains_content(self):
        clauses = parse_policy_clauses(POLICY_PATH)
        tp11 = next(c for c in clauses if c["clause_id"] == "TP-1.1")
        assert "7 days" in tp11["text"]
        assert "14 days" in tp11["text"]


class TestPolicyRAG:
    @pytest.fixture
    def rag(self, tmp_path):
        rag = PolicyRAG(
            persist_dir=str(tmp_path / "chroma"),
            embedding_model="all-MiniLM-L6-v2",
            top_k=5,
        )
        rag.ingest(POLICY_PATH)
        return rag

    def test_ingest_count(self, rag):
        assert rag.collection.count() == 8

    def test_retrieve_advance_booking(self, rag):
        results = rag.retrieve("advance booking days domestic international")
        assert len(results) > 0
        clause_ids = [r["clause_id"] for r in results]
        assert "TP-1.1" in clause_ids

    def test_retrieve_fare_cap(self, rag):
        results = rag.retrieve("fare cap economy domestic international maximum INR")
        assert len(results) > 0
        clause_ids = [r["clause_id"] for r in results]
        assert "TP-2.2" in clause_ids

    def test_retrieve_purpose(self, rag):
        results = rag.retrieve("business purpose personal leisure eligible")
        clause_ids = [r["clause_id"] for r in results]
        assert "TP-6.1" in clause_ids

    def test_multi_query(self, rag):
        results = rag.multi_query_retrieve([
            "fare cap economy",
            "advance booking",
            "business purpose",
        ])
        clause_ids = [r["clause_id"] for r in results]
        assert len(set(clause_ids)) >= 3

    def test_get_clause_text(self, rag):
        text = rag.get_clause_text("TP-2.1")
        assert "Economy" in text or "economy" in text

    def test_idempotent_ingest(self, rag):
        """Re-ingesting should not duplicate entries."""
        rag.ingest(POLICY_PATH)
        assert rag.collection.count() == 8
