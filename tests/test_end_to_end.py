"""End-to-end integration tests for the full pipeline (no LLM — uses fallbacks)."""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path

import pytest

from agents.approval_manager import ApprovalManager
from core.memory import MemoryStore
from core.rag import PolicyRAG
from core.schemas import Decision

DATA_DIR = Path(__file__).parent.parent / "data"


@pytest.fixture
def memory():
    store = MemoryStore(":memory:")
    store.seed_from_json(
        DATA_DIR / "mock" / "employees.json",
        DATA_DIR / "mock" / "budgets.json",
        DATA_DIR / "mock" / "trip_history_seed.json",
    )
    yield store
    store.close()


@pytest.fixture
def rag(tmp_path):
    r = PolicyRAG(
        persist_dir=str(tmp_path / "chroma"),
        embedding_model="all-MiniLM-L6-v2",
        top_k=5,
    )
    r.ingest(DATA_DIR / "policy" / "travel_policy.md")
    return r


@pytest.mark.asyncio
async def test_case1_approve_no_llm(memory, rag):
    """Case 1: E101, DEL→BOM, economy 9500, client meeting → APPROVE."""
    manager = ApprovalManager(memory, rag, use_llm=False)
    result = await manager.process_request(
        "I need to fly Delhi to Mumbai on 2 Dec 2026, returning 3 Dec 2026, for a client meeting. Economy class please. Employee E101."
    )
    assert result.decision == Decision.APPROVE
    assert result.primary_clause_id == "TP-2.2"
    assert result.best_compliant_fare is not None
    assert result.best_compliant_fare.price_inr == 9500


@pytest.mark.asyncio
async def test_case2_modify_no_llm(memory, rag):
    """Case 2: E101, DEL→BLR, Business requested → MODIFY TP-2.1."""
    manager = ApprovalManager(memory, rag, use_llm=False)
    result = await manager.process_request(
        "Book me a Business class flight from Delhi to Bangalore, departing 27 Nov 2026, return 28 Nov. Purpose: client meeting. I am employee E101."
    )
    assert result.decision == Decision.MODIFY
    assert result.primary_clause_id == "TP-2.1"
    assert result.best_compliant_fare is not None
    assert result.best_compliant_fare.price_inr == 7800


@pytest.mark.asyncio
async def test_case3_escalate_advance_no_llm(memory, rag):
    """Case 3: E101, DEL→GOI, 3 days ahead → ESCALATE TP-1.1."""
    manager = ApprovalManager(memory, rag, use_llm=False)
    result = await manager.process_request(
        "Urgent travel needed. Delhi to Goa on 15 Nov 2026, returning 16 Nov. Client meeting. Economy. Employee E101."
    )
    assert result.decision == Decision.ESCALATE
    assert result.primary_clause_id == "TP-1.1"


@pytest.mark.asyncio
async def test_case6_escalate_purpose_no_llm(memory, rag):
    """Case 6: E101, DEL→GOI, family vacation → ESCALATE TP-6.1."""
    manager = ApprovalManager(memory, rag, use_llm=False)
    result = await manager.process_request(
        "I want to travel from Delhi to Goa on 5 Dec 2026, returning 8 Dec 2026. Purpose: family vacation. Economy class. Employee E101."
    )
    assert result.decision == Decision.ESCALATE
    assert result.primary_clause_id == "TP-6.1"


@pytest.mark.asyncio
async def test_case8_escalate_budget_no_llm(memory, rag):
    """Case 8: E104, BOM→DEL, OPS budget 9000 → ESCALATE TP-4.1."""
    manager = ApprovalManager(memory, rag, use_llm=False)
    result = await manager.process_request(
        "Mumbai to Delhi on 30 Nov 2026, return 1 Dec 2026. Project delivery work. Economy. Employee E104."
    )
    assert result.decision == Decision.ESCALATE
    assert result.primary_clause_id == "TP-4.1"


@pytest.mark.asyncio
async def test_case9_escalate_frequency_no_llm(memory, rag):
    """Case 9: E105, DEL→CCU, 3 trips already → ESCALATE TP-7.1."""
    manager = ApprovalManager(memory, rag, use_llm=False)
    result = await manager.process_request(
        "Delhi to Kolkata, departing 3 Dec 2026, return 4 Dec 2026. Purpose: audit. Economy at INR 8000. Employee E105."
    )
    assert result.decision == Decision.ESCALATE
    assert result.primary_clause_id == "TP-7.1"


@pytest.mark.asyncio
async def test_all_agents_use_memory(memory, rag):
    """Verify every agent reads and writes to shared memory."""
    manager = ApprovalManager(memory, rag, use_llm=False)
    result = await manager.process_request(
        "I need to fly Delhi to Mumbai on 2 Dec 2026, returning 3 Dec 2026, for a client meeting. Economy class please. Employee E101."
    )
    # Check audit log has entries from all agents
    logs = memory.get_audit_log(result.request_id)
    agents = {e.agent for e in logs}
    assert "request_parser" in agents, "Request parser should write to memory"
    assert "fare_search" in agents, "Fare search should write to memory"
    assert "policy_retrieval" in agents, "Policy retrieval should write to memory"
    assert "budget_checker" in agents, "Budget checker should write to memory"
    assert "approval_manager" in agents, "Approval manager should write to memory"

    # Check reads happened (logged as read_* actions)
    actions = {e.action for e in logs}
    read_actions = [a for a in actions if "read" in a.lower()]
    assert len(read_actions) >= 3, f"At least 3 read operations expected, got: {read_actions}"
