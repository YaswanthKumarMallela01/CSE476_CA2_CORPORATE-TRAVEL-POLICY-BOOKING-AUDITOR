"""Tests for interactive follow-up clarification, rich HITL dossiers, booking history, and Flask APIs."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from agents.approval_manager import ApprovalManager
from core import hitl
from core.memory import MemoryStore
from core.rag import PolicyRAG
from core.schemas import Decision, RequestStatus

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
async def test_incomplete_request_triggers_follow_up_questions(memory, rag):
    """Partial prompt without employee/origin must ask follow-up questions instead of misparsing."""
    manager = ApprovalManager(memory, rag, use_llm=False)
    res = await manager.process_request(
        "Book flight to Bengaluru tomorrow for sales demo",
        allow_clarification=True,
    )
    assert res.decision == Decision.NEEDS_INFO
    assert res.status == RequestStatus.NEEDS_CLARIFICATION
    assert res.destination_iata == "BLR"
    assert res.origin_iata == ""
    assert res.depart_date == "2026-11-13"
    assert res.purpose_text == "sales demo"

    q_fields = {q["field"] for q in res.follow_up_questions}
    assert "employee_id" in q_fields
    assert "origin_iata" in q_fields

    # Ensure FareSearchAgent did not execute before clarification resolved
    logs = memory.get_audit_log(res.request_id)
    assert not any(e.agent == "fare_search" for e in logs)
    assert any(e.action == "clarification_requested" for e in logs)


@pytest.mark.asyncio
async def test_clarification_resolution_completes_audit(memory, rag):
    """Supplying answers to follow-up questions completes the multi-agent audit accurately."""
    manager = ApprovalManager(memory, rag, use_llm=False)
    first = await manager.process_request(
        "Book flight to Bengaluru tomorrow for sales demo",
        allow_clarification=True,
    )
    assert first.decision == Decision.NEEDS_INFO

    final = await manager.process_request(
        "Book flight to Bengaluru tomorrow for sales demo",
        allow_clarification=False,
        clarifications={
            "employee_id": "E101",
            "origin": "DEL",
            "depart_date": "2026-11-27",
        },
        request_id=first.request_id,
    )
    assert final.decision == Decision.APPROVE
    assert final.primary_clause_id == "TP-2.2"
    assert final.employee_name == "Aarav Sharma"
    assert final.employee_department == "SALES"
    assert final.origin_iata == "DEL"
    assert final.destination_iata == "BLR"
    assert final.best_compliant_fare is not None
    assert final.best_compliant_fare.price_inr == 7800


@pytest.mark.asyncio
async def test_escalated_request_rich_review_and_auto_budget_deduction(memory, rag):
    """Escalated requests carry full context into review_queue and deduct budget on approval."""
    manager = ApprovalManager(memory, rag, use_llm=False)
    res = await manager.process_request(
        "Urgent travel needed. Delhi to Goa on 15 Nov 2026, returning 16 Nov. Client meeting. Economy. Employee E101.",
        allow_clarification=True,
    )
    assert res.decision == Decision.ESCALATE
    assert res.primary_clause_id == "TP-1.1"
    assert res.best_compliant_fare is None
    assert res.recommended_fare is not None
    assert res.recommended_fare.price_inr == 8200
    assert res.employee_name == "Aarav Sharma"

    # Check review queue payload has rich context
    item = memory.get_review_item(res.request_id)
    assert item is not None
    aud = json.loads(item.audit_result_json)
    assert aud["employee_name"] == "Aarav Sharma"
    assert aud["origin_iata"] == "DEL"
    assert aud["destination_iata"] == "GOI"
    assert len(aud["available_fares"]) >= 4

    # Approve WITHOUT explicit fare_inr — hitl.submit_verdict resolves recommended_fare & deducts SALES budget
    before_sales = memory.get_department_budget("SALES")
    verdict_res = hitl.submit_verdict(res.request_id, "approve", "Approved urgent visit", "VP Sales", memory)
    assert verdict_res["final_status"] == "HUMAN_APPROVED"
    assert verdict_res["fare_inr"] == 8200
    assert memory.get_department_budget("SALES") == before_sales - 8200

    # Check booking_requests history updated
    hist = memory.get_booking_history()
    rec = next(h for h in hist if h["request_id"] == res.request_id)
    assert rec["resolved_name"] == "Aarav Sharma"
    assert rec["status"] == "HUMAN_APPROVED"
    assert rec["fare_inr"] == 8200
    assert rec["human_note"] == "Approved urgent visit"
