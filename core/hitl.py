"""
Human-in-the-loop review queue and verdict flow-back.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from core.memory import MemoryStore
from core.schemas import (
    AuditResult,
    HumanVerdict,
    RequestStatus,
    ReviewQueueItem,
    TripRecord,
    TripStatus,
)

logger = logging.getLogger(__name__)


def enqueue(audit_result: AuditResult, memory: MemoryStore) -> None:
    """Add an escalated request to the human review queue.
    
    Creates a review_queue row with status PENDING and stores the full
    AuditResult as JSON for the reviewer's reference.
    """
    item = ReviewQueueItem(
        request_id=audit_result.request_id,
        status="PENDING",
        audit_result_json=audit_result.model_dump_json(),
    )
    memory.enqueue_review(item)
    memory.write_audit_log(
        audit_result.request_id,
        "hitl",
        "enqueued_for_review",
        json.dumps({"decision": audit_result.decision.value, "primary_clause": audit_result.primary_clause_id}),
    )
    logger.info("Enqueued request %s for human review", audit_result.request_id)


def submit_verdict(
    request_id: str,
    verdict: str,
    note: str,
    reviewer: str,
    memory: MemoryStore,
    fare_inr: Optional[float] = None,
    airline: Optional[str] = None,
) -> dict:
    """Process a human verdict on an escalated request.
    
    1. Updates review_queue status
    2. Updates trip_history status, fare, and decided_by
    3. Updates booking_requests history
    4. Deducts fare from dept_budget_ledger if approved/modified
    5. Appends to audit_log
    6. Returns the updated record summary
    """
    # 1. Get the review item
    item = memory.get_review_item(request_id)
    if item is None:
        raise ValueError(f"Request {request_id} not found in review queue")
    if item.status != "PENDING":
        raise ValueError(f"Request {request_id} is already reviewed (status: {item.status})")

    # Parse the stored audit result for context
    audit_data = json.loads(item.audit_result_json) if item.audit_result_json else {}

    # 2. Update review_queue
    memory.update_review(request_id, verdict, note, reviewer)

    # 3. Determine trip status
    status_map = {
        "approve": TripStatus.APPROVED,
        "reject": TripStatus.REJECTED,
        "modify": TripStatus.APPROVED,
    }
    trip_status = status_map.get(verdict, TripStatus.REJECTED)

    # Resolve employee_id cleanly
    trips = memory.conn.execute(
        "SELECT * FROM trip_history WHERE trip_id = ?", (request_id,)
    ).fetchone()
    emp_id = (
        audit_data.get("employee_id")
        or (trips["employee_id"] if trips and trips["employee_id"] not in ("", "REQ") else None)
        or _extract_employee_id(request_id, audit_data)
        or ""
    )

    # Resolve fare_inr cleanly
    resolved_fare = fare_inr
    resolved_airline = airline
    if resolved_fare is None:
        for key in ("best_compliant_fare", "recommended_fare"):
            if isinstance(audit_data.get(key), dict) and audit_data[key].get("price_inr"):
                resolved_fare = float(audit_data[key]["price_inr"])
                resolved_airline = resolved_airline or audit_data[key].get("airline", "")
                break
    if resolved_fare is None and audit_data.get("requested_fare_inr"):
        resolved_fare = float(audit_data["requested_fare_inr"])
    if resolved_fare is None and isinstance(audit_data.get("available_fares"), list) and audit_data["available_fares"]:
        first_fare = audit_data["available_fares"][0]
        if isinstance(first_fare, dict) and first_fare.get("price_inr"):
            resolved_fare = float(first_fare["price_inr"])
            resolved_airline = resolved_airline or first_fare.get("airline", "")

    # 4. Find or create trip history record
    if trips:
        memory.update_trip_status(
            request_id,
            trip_status,
            fare_inr=resolved_fare if verdict in ("approve", "modify") else None,
            decided_by=f"human:{reviewer}",
        )
    else:
        trip = TripRecord(
            trip_id=request_id,
            employee_id=emp_id or "UNKNOWN",
            origin=audit_data.get("origin_iata", ""),
            destination=audit_data.get("destination_iata", ""),
            depart_date=audit_data.get("depart_date") or "2026-11-12",
            fare_inr=resolved_fare or 0.0,
            status=trip_status,
            clause_id=audit_data.get("primary_clause_id", ""),
            decided_by=f"human:{reviewer}",
        )
        memory.add_trip(trip)

    # 5. Deduct budget if approved or modified with a fare
    budget_deducted = 0.0
    dept_name = audit_data.get("employee_department") or ""
    if verdict in ("approve", "modify") and resolved_fare and resolved_fare > 0:
        if emp_id:
            emp = memory.get_employee(emp_id)
            if emp:
                memory.deduct_budget(emp.department, resolved_fare)
                budget_deducted = resolved_fare
                dept_name = emp.department
                logger.info(
                    "Budget deducted: INR %.0f from %s (verdict: %s by %s)",
                    resolved_fare, emp.department, verdict, reviewer,
                )

    final_status_map = {
        "approve": "HUMAN_APPROVED",
        "reject": "HUMAN_REJECTED",
        "modify": "HUMAN_MODIFIED",
    }
    final_status = final_status_map.get(verdict, "UNKNOWN")

    # Update booking_requests ledger
    memory.update_booking_request_verdict(
        request_id=request_id,
        final_status=final_status,
        verdict=verdict,
        note=note,
        reviewer=reviewer,
        fare_inr=resolved_fare if verdict in ("approve", "modify") else None,
        airline=resolved_airline,
    )

    # 6. Write audit log
    memory.write_audit_log(
        request_id,
        "hitl",
        f"human_verdict_{verdict}",
        json.dumps({
            "employee_id": emp_id,
            "department": dept_name,
            "verdict": verdict,
            "final_status": final_status,
            "note": note,
            "reviewer": reviewer,
            "fare_inr": resolved_fare,
            "airline": resolved_airline,
            "budget_deducted": budget_deducted,
        }),
    )

    result = {
        "request_id": request_id,
        "employee_id": emp_id,
        "department": dept_name,
        "verdict": verdict,
        "final_status": final_status,
        "note": note,
        "reviewer": reviewer,
        "fare_inr": resolved_fare,
        "airline": resolved_airline,
        "budget_deducted": budget_deducted,
    }
    logger.info("Human verdict processed: %s", result)
    return result


def get_pending_reviews(memory: MemoryStore) -> list[ReviewQueueItem]:
    """Get all pending review items."""
    return memory.get_review_queue(status="PENDING")


def get_all_reviews(memory: MemoryStore) -> list[ReviewQueueItem]:
    """Get all review items regardless of status."""
    return memory.get_review_queue()


def _extract_employee_id(request_id: str, audit_data: dict) -> Optional[str]:
    """Extract employee ID from request_id or audit data."""
    # request_id format: "REQ-EXXX-XXXX" or embedded in audit data
    if "request_id" in audit_data:
        parts = audit_data["request_id"].split("-")
        for p in parts:
            if p.startswith("E") and p[1:].isdigit():
                return p
    parts = request_id.split("-")
    for p in parts:
        if p.startswith("E") and p[1:].isdigit():
            return p
    return None
