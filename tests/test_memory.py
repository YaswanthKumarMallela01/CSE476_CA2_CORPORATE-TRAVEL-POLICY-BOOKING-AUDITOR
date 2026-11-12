"""Tests for the shared SQLite memory store."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from core.memory import MemoryStore
from core.schemas import TripRecord, TripStatus

DATA_DIR = Path(__file__).parent.parent / "data"


@pytest.fixture
def memory():
    """Create a fresh in-memory store seeded from JSON."""
    store = MemoryStore(":memory:")
    store.seed_from_json(
        DATA_DIR / "mock" / "employees.json",
        DATA_DIR / "mock" / "budgets.json",
        DATA_DIR / "mock" / "trip_history_seed.json",
    )
    yield store
    store.close()


class TestEmployees:
    def test_get_employee(self, memory):
        emp = memory.get_employee("E101")
        assert emp is not None
        assert emp.grade == "L3"
        assert emp.department == "SALES"

    def test_get_unknown_employee(self, memory):
        assert memory.get_employee("E999") is None

    def test_all_employees_loaded(self, memory):
        for eid in ["E101", "E102", "E103", "E104", "E105"]:
            assert memory.get_employee(eid) is not None


class TestTripHistory:
    def test_e105_has_3_trips(self, memory):
        trips = memory.get_trips_for_employee("E105")
        assert len(trips) == 3

    def test_quarter_count_e105_q4(self, memory):
        count = memory.count_trips_in_quarter("E105", date(2026, 11, 12))
        assert count == 3

    def test_quarter_count_e101_q4(self, memory):
        count = memory.count_trips_in_quarter("E101", date(2026, 11, 12))
        assert count == 0  # E101's trip is in Q3

    def test_add_trip(self, memory):
        trip = TripRecord(
            trip_id="T_NEW",
            employee_id="E101",
            origin="DEL",
            destination="BOM",
            depart_date=date(2026, 12, 5),
            fare_inr=9500,
            status=TripStatus.APPROVED,
            clause_id="TP-2.2",
        )
        memory.add_trip(trip)
        trips = memory.get_trips_for_employee("E101")
        assert any(t.trip_id == "T_NEW" for t in trips)


class TestBudget:
    def test_get_budget(self, memory):
        assert memory.get_department_budget("SALES") == 120000
        assert memory.get_department_budget("ENG") == 300000
        assert memory.get_department_budget("OPS") == 9000

    def test_deduct_budget(self, memory):
        memory.deduct_budget("SALES", 10000)
        assert memory.get_department_budget("SALES") == 110000


class TestAuditLog:
    def test_write_and_read(self, memory):
        memory.write_audit_log("REQ-1", "test_agent", "test_action", '{"key": "val"}')
        logs = memory.get_audit_log("REQ-1")
        assert len(logs) == 1
        assert logs[0].agent == "test_agent"


class TestResetToSeed:
    def test_reset_clears_and_reseeds(self, memory):
        memory.write_audit_log("REQ-1", "test", "action")
        memory.deduct_budget("SALES", 50000)

        memory.reset_to_seed(
            DATA_DIR / "mock" / "employees.json",
            DATA_DIR / "mock" / "budgets.json",
            DATA_DIR / "mock" / "trip_history_seed.json",
        )
        assert memory.get_department_budget("SALES") == 120000
        assert memory.get_audit_log("REQ-1") == []
