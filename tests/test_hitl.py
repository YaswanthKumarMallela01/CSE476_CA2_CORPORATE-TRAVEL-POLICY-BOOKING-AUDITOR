"""Tests for the Human-in-the-Loop review flow."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from core import hitl
from core.memory import MemoryStore
from core.schemas import (
    AuditResult,
    Decision,
    FareOption,
    CabinClass,
    RequestStatus,
    TripRecord,
    TripStatus,
)

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


def _make_escalated_result(request_id="REQ-E101-001", employee_id="E101") -> AuditResult:
    return AuditResult(
        request_id=request_id,
        decision=Decision.ESCALATE,
        primary_clause_id="TP-1.1",
        justification="Short notice travel",
        routed_to_human=True,
        status=RequestStatus.PENDING_HUMAN,
    )


class TestEnqueue:
    def test_enqueue_creates_pending(self, memory):
        result = _make_escalated_result()
        hitl.enqueue(result, memory)
        items = memory.get_review_queue("PENDING")
        assert len(items) == 1
        assert items[0].request_id == "REQ-E101-001"

    def test_enqueue_logs_audit(self, memory):
        result = _make_escalated_result()
        hitl.enqueue(result, memory)
        logs = memory.get_audit_log("REQ-E101-001")
        assert any("enqueued" in e.action for e in logs)


class TestSubmitVerdict:
    def test_approve_verdict(self, memory):
        result = _make_escalated_result()
        hitl.enqueue(result, memory)

        # Also create a trip record so budget deduction works
        trip = TripRecord(
            trip_id="REQ-E101-001",
            employee_id="E101",
            origin="DEL", destination="GOI",
            depart_date=date(2026, 11, 15),
            fare_inr=0, status=TripStatus.PENDING,
        )
        memory.add_trip(trip)

        verdict_result = hitl.submit_verdict(
            "REQ-E101-001", "approve", "Approved by manager", "manager1", memory, fare_inr=8200,
        )
        assert verdict_result["final_status"] == "HUMAN_APPROVED"

        # Check review queue updated
        item = memory.get_review_item("REQ-E101-001")
        assert item.status == "HUMAN_APPROVED"

    def test_reject_verdict(self, memory):
        result = _make_escalated_result(request_id="REQ-E101-002")
        hitl.enqueue(result, memory)
        trip = TripRecord(
            trip_id="REQ-E101-002",
            employee_id="E101",
            origin="DEL", destination="GOI",
            depart_date=date(2026, 12, 5),
            fare_inr=0, status=TripStatus.PENDING,
        )
        memory.add_trip(trip)

        hitl.submit_verdict("REQ-E101-002", "reject", "Not eligible", "manager1", memory)
        item = memory.get_review_item("REQ-E101-002")
        assert item.status == "HUMAN_REJECTED"

    def test_approve_deducts_budget(self, memory):
        result = _make_escalated_result()
        hitl.enqueue(result, memory)
        trip = TripRecord(
            trip_id="REQ-E101-001",
            employee_id="E101",
            origin="DEL", destination="GOI",
            depart_date=date(2026, 11, 15),
            fare_inr=0, status=TripStatus.PENDING,
        )
        memory.add_trip(trip)

        initial_budget = memory.get_department_budget("SALES")
        hitl.submit_verdict("REQ-E101-001", "approve", "OK", "manager", memory, fare_inr=8200)
        new_budget = memory.get_department_budget("SALES")
        assert new_budget == initial_budget - 8200

    def test_double_verdict_raises(self, memory):
        result = _make_escalated_result()
        hitl.enqueue(result, memory)
        trip = TripRecord(
            trip_id="REQ-E101-001", employee_id="E101",
            origin="DEL", destination="GOI", depart_date=date(2026, 11, 15),
            fare_inr=0, status=TripStatus.PENDING,
        )
        memory.add_trip(trip)
        hitl.submit_verdict("REQ-E101-001", "approve", "OK", "mgr", memory)
        with pytest.raises(ValueError, match="already reviewed"):
            hitl.submit_verdict("REQ-E101-001", "approve", "Again", "mgr", memory)


class TestFullLoop:
    """Test the complete HITL loop: escalate → verdict → updated history."""

    def test_approve_then_check_history(self, memory):
        """After approving an escalated request, a follow-up should see the updated trip count."""
        # Initial: E101 has 0 trips in Q4
        assert memory.count_trips_in_quarter("E101", date(2026, 11, 12)) == 0

        # Escalate and add trip
        result = _make_escalated_result()
        hitl.enqueue(result, memory)
        trip = TripRecord(
            trip_id="REQ-E101-001", employee_id="E101",
            origin="DEL", destination="GOI", depart_date=date(2026, 11, 15),
            fare_inr=8200, status=TripStatus.PENDING,
        )
        memory.add_trip(trip)

        # Approve
        hitl.submit_verdict("REQ-E101-001", "approve", "Approved", "mgr", memory, fare_inr=8200)

        # Trip status should now be approved
        trips = memory.get_trips_for_employee("E101")
        approved_trip = next((t for t in trips if t.trip_id == "REQ-E101-001"), None)
        assert approved_trip is not None
        assert approved_trip.status == TripStatus.APPROVED

        # Quarter count should increment
        assert memory.count_trips_in_quarter("E101", date(2026, 11, 12)) == 1

        # Budget should be deducted
        assert memory.get_department_budget("SALES") == 120000 - 8200
