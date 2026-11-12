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
) -> dict:
    """Process a human verdict on an escalated request.
    
    1. Updates review_queue status
    2. Updates trip_history status  
    3. Deducts fare from dept_budget_ledger if approved/modified
    4. Appends to audit_log
    5. Returns the updated record summary
    
    Args:
        request_id: The request being reviewed.
        verdict: 'approve', 'reject', or 'modify'.
        note: Reviewer's note.
        reviewer: Reviewer's name/id.
        fare_inr: Fare amount to use (required for approve/modify).
        memory: The shared MemoryStore.
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

    # 3. Determine trip status and handle budget
    status_map = {
        "approve": TripStatus.APPROVED,
        "reject": TripStatus.REJECTED,
        "modify": TripStatus.APPROVED,
    }
    trip_status = status_map.get(verdict, TripStatus.REJECTED)

    # 4. Find or create trip history record
    # We need employee context from the audit result
    # Look for existing trip or create one
    trips = memory.conn.execute(
        "SELECT * FROM trip_history WHERE trip_id = ?", (request_id,)
    ).fetchone()

    if trips:
        memory.update_trip_status(request_id, trip_status)
    else:
        # Create a trip record from the audit data
        # We need employee_id from the request_id pattern or audit data
        trip = TripRecord(
            trip_id=request_id,
            employee_id=audit_data.get("request_id", request_id).split("-")[0] if "-" in request_id else request_id,
            origin="",
            destination="",
            depart_date="2026-01-01",
            fare_inr=fare_inr or 0,
            status=trip_status,
            clause_id=audit_data.get("primary_clause_id", ""),
            decided_by="human",
        )
        memory.add_trip(trip)

    # 5. Deduct budget if approved or modified with a fare
    if verdict in ("approve", "modify") and fare_inr and fare_inr > 0:
        # Find department from audit data or trip
        # We need to look up the employee
        emp_id = _extract_employee_id(request_id, audit_data)
        if emp_id:
            emp = memory.get_employee(emp_id)
            if emp:
                memory.deduct_budget(emp.department, fare_inr)
                logger.info(
                    "Budget deducted: INR %.0f from %s (verdict: %s by %s)",
                    fare_inr, emp.department, verdict, reviewer,
                )

    # 6. Write audit log
    memory.write_audit_log(
        request_id,
        "hitl",
        f"human_verdict_{verdict}",
        json.dumps({
            "verdict": verdict,
            "note": note,
            "reviewer": reviewer,
            "fare_inr": fare_inr,
        }),
    )

    final_status_map = {
        "approve": "HUMAN_APPROVED",
        "reject": "HUMAN_REJECTED",
        "modify": "HUMAN_MODIFIED",
    }

    result = {
        "request_id": request_id,
        "verdict": verdict,
        "final_status": final_status_map.get(verdict, "UNKNOWN"),
        "note": note,
        "reviewer": reviewer,
        "fare_inr": fare_inr,
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
