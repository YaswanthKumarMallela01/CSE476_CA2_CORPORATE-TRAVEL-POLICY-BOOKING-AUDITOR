"""
Pydantic v2 schemas for every inter-agent message in the Travel Auditor system.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------
class PurposeCategory(str, Enum):
    BUSINESS_ELIGIBLE = "business_eligible"
    PERSONAL = "personal"
    UNSPECIFIED = "unspecified"


class CabinClass(str, Enum):
    ECONOMY = "economy"
    PREMIUM_ECONOMY = "premium_economy"
    BUSINESS = "business"


class Decision(str, Enum):
    APPROVE = "APPROVE"
    MODIFY = "MODIFY"
    ESCALATE = "ESCALATE"


class RequestStatus(str, Enum):
    DECIDED = "DECIDED"
    PENDING_HUMAN = "PENDING_HUMAN"
    HUMAN_APPROVED = "HUMAN_APPROVED"
    HUMAN_REJECTED = "HUMAN_REJECTED"
    HUMAN_MODIFIED = "HUMAN_MODIFIED"


class TripStatus(str, Enum):
    APPROVED = "approved"
    REJECTED = "rejected"
    COMPLETED = "completed"
    PENDING = "pending"


class HumanVerdict(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"
    MODIFY = "modify"


# ---------------------------------------------------------------------------
# Agent I/O Models
# ---------------------------------------------------------------------------
class ParsedRequest(BaseModel):
    """Output of RequestParserAgent."""
    request_id: str = Field(description="Unique request identifier")
    employee_id: str
    origin_iata: str
    destination_iata: str
    depart_date: date
    return_date: Optional[date] = None
    purpose_text: str
    purpose_category: PurposeCategory
    requested_class: CabinClass = CabinClass.ECONOMY
    requested_fare: Optional[float] = None
    is_international: bool = False
    days_ahead: int = 0


class RetrievedClause(BaseModel):
    """A single policy clause retrieved by RAG."""
    clause_id: str
    text: str
    score: float = 0.0


class RetrievedPolicy(BaseModel):
    """Output of PolicyRetrievalAgent."""
    clauses: list[RetrievedClause] = Field(default_factory=list)
    applicable_clause_ids: list[str] = Field(default_factory=list)


class FareOption(BaseModel):
    """A single fare option from FareSearchAgent."""
    fare_id: str
    airline: str
    price_inr: float
    cabin: CabinClass
    duration_hours: float
    contracted: bool
    refundable: bool


class FareOptions(BaseModel):
    """Output of FareSearchAgent."""
    options: list[FareOption] = Field(default_factory=list)


class BudgetCheck(BaseModel):
    """Output of BudgetCheckerAgent."""
    cap_inr: float = 0.0
    cap_clause: str = ""
    dept_remaining_inr: float = 0.0
    compliant_options: list[FareOption] = Field(default_factory=list)
    cheapest_compliant: Optional[FareOption] = None
    fits_budget: bool = False
    over_cap_requested: bool = False
    reasons: list[str] = Field(default_factory=list)


class AuditResult(BaseModel):
    """Final output of ApprovalManager."""
    request_id: str
    decision: Decision
    primary_clause_id: str
    secondary_clause_ids: list[str] = Field(default_factory=list)
    best_compliant_fare: Optional[FareOption] = None
    justification: str = ""
    routed_to_human: bool = False
    status: RequestStatus = RequestStatus.DECIDED


# ---------------------------------------------------------------------------
# Employee / Memory models
# ---------------------------------------------------------------------------
class Employee(BaseModel):
    """Employee profile."""
    employee_id: str
    name: str
    grade: str
    department: str
    email: str


class TripRecord(BaseModel):
    """A trip history record."""
    trip_id: str
    employee_id: str
    origin: str
    destination: str
    depart_date: date
    fare_inr: float
    status: TripStatus
    clause_id: str = ""
    decided_by: str = "system"
    created_at: Optional[datetime] = None


class AuditLogEntry(BaseModel):
    """An entry in the audit log."""
    id: Optional[int] = None
    request_id: str
    agent: str
    action: str
    payload_json: str = ""
    ts: Optional[datetime] = None


class ReviewQueueItem(BaseModel):
    """A human review queue item."""
    request_id: str
    status: str = "PENDING"
    human_verdict: Optional[str] = None
    human_note: Optional[str] = None
    reviewed_at: Optional[datetime] = None
    audit_result_json: str = ""
