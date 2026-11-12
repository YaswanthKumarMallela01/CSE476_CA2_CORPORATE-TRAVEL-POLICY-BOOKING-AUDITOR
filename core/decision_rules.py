"""
Deterministic decision rules for the Travel Auditor system.
The LLM is NEVER used for the decision itself — only these rules determine
APPROVE / MODIFY / ESCALATE.

Clause precedence (first match is PRIMARY):
    TP-6.1 > TP-1.1 > TP-7.1 > TP-9.1 > TP-4.1 > TP-2.1 > TP-2.2
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from core.schemas import (
    BudgetCheck,
    CabinClass,
    Decision,
    FareOption,
    FareOptions,
    ParsedRequest,
    PurposeCategory,
    RetrievedPolicy,
)

logger = logging.getLogger(__name__)

# Clause precedence order — first match wins as PRIMARY
CLAUSE_PRECEDENCE = ["TP-6.1", "TP-1.1", "TP-7.1", "TP-9.1", "TP-4.1", "TP-2.1", "TP-2.2"]

# Contracted airlines
CONTRACTED_AIRLINES = {"AirIndia", "IndiGo", "Vistara-AI"}

# Fare caps (INR, round trip)
FARE_CAPS = {
    ("economy", False): 15_000,     # Economy domestic
    ("economy", True): 90_000,      # Economy international
    ("business", True): 120_000,    # Business international
}

# Grade numeric mapping for comparison
GRADE_LEVEL = {"L1": 1, "L2": 2, "L3": 3, "L4": 4, "L5": 5, "L6": 6, "L7": 7, "L8": 8}


@dataclass
class DecisionResult:
    """Result of the deterministic decision engine."""
    decision: Decision
    primary_clause_id: str
    secondary_clause_ids: list[str] = field(default_factory=list)
    best_compliant_fare: Optional[FareOption] = None
    reasons: list[str] = field(default_factory=list)


def get_fare_cap(cabin: str, is_international: bool) -> Optional[float]:
    """Get the fare cap for a cabin class and route type."""
    key = (cabin.lower(), is_international)
    return FARE_CAPS.get(key)


def is_class_allowed(
    requested_class: CabinClass,
    duration_hours: float,
    grade: str,
) -> bool:
    """Check TP-2.1: Business/Premium Economy only if duration >= 6h AND grade >= L5."""
    if requested_class == CabinClass.ECONOMY:
        return True
    grade_num = GRADE_LEVEL.get(grade, 0)
    return duration_hours >= 6.0 and grade_num >= 5


def filter_compliant_fares(
    fares: list[FareOption],
    is_international: bool,
    grade: str,
    dept_remaining: float,
    max_duration_hours: float = 0.0,
    requested_cabin: Optional[CabinClass] = None,
) -> list[FareOption]:
    """Filter fares to those satisfying TP-2.1, TP-2.2, TP-4.1, and TP-5.1.
    
    If employee requested Business/Premium Economy and is eligible, returns compliant
    fares in that cabin. Otherwise, defaults to Economy (TP-2.1).
    Returns fares sorted by price (ascending), with contracted preferred per TP-5.1.
    """
    target_cabin = requested_cabin if requested_cabin in (CabinClass.BUSINESS, CabinClass.PREMIUM_ECONOMY) else None

    # Check if target cabin has eligible compliant fares
    if target_cabin:
        target_fares = [
            f for f in fares
            if f.cabin == target_cabin
            and is_class_allowed(f.cabin, f.duration_hours, grade)
            and (get_fare_cap(f.cabin.value, is_international) is None or f.price_inr <= get_fare_cap(f.cabin.value, is_international))
            and f.price_inr <= dept_remaining
        ]
        if target_fares:
            return _apply_carrier_preference(target_fares)

    compliant: list[FareOption] = []
    for fare in fares:
        # TP-2.1: class check
        cabin = fare.cabin
        duration = fare.duration_hours
        if cabin != CabinClass.ECONOMY:
            if not is_class_allowed(cabin, duration, grade):
                continue

        # TP-2.2: fare cap
        cap = get_fare_cap(cabin.value, is_international)
        if cap is not None and fare.price_inr > cap:
            continue

        # TP-4.1: department budget
        if fare.price_inr > dept_remaining:
            continue

        compliant.append(fare)

    # TP-5.1: prefer contracted. Sort by price; among equal prices prefer contracted.
    # But a non-contracted is allowed only if cheaper by >= 10% than best contracted.
    return _apply_carrier_preference(compliant)


def _apply_carrier_preference(fares: list[FareOption]) -> list[FareOption]:
    """Apply TP-5.1 carrier preference filtering and sorting.
    
    A non-contracted airline is kept only if:
    - It is cheaper than the best contracted option by at least 10%, OR
    - No contracted option exists.
    """
    contracted = [f for f in fares if f.contracted]
    non_contracted = [f for f in fares if not f.contracted]

    if not contracted:
        # No contracted options — allow all non-contracted
        return sorted(non_contracted, key=lambda f: (f.price_inr, f.duration_hours))

    best_contracted_price = min(f.price_inr for f in contracted)

    # Keep non-contracted only if cheaper by >= 10%
    threshold = best_contracted_price * 0.90
    eligible_non_contracted = [f for f in non_contracted if f.price_inr <= threshold]

    all_eligible = contracted + eligible_non_contracted
    # Sort: price ascending, then duration ascending
    return sorted(all_eligible, key=lambda f: (f.price_inr, f.duration_hours))


def select_best_fare(compliant_fares: list[FareOption]) -> Optional[FareOption]:
    """Select the best compliant fare: lowest price, ties broken by shortest duration."""
    if not compliant_fares:
        return None
    return compliant_fares[0]  # Already sorted by price then duration


def decide(
    parsed: ParsedRequest,
    budget_check: BudgetCheck,
    quarter_trip_count: int,
    today: date,
    employee_grade: str,
    fare_options: FareOptions,
) -> DecisionResult:
    """Run the deterministic decision engine.
    
    Applies clause precedence: TP-6.1 > TP-1.1 > TP-7.1 > TP-9.1 > TP-4.1 > TP-2.1 > TP-2.2.
    
    Returns ESCALATE, MODIFY, or APPROVE with the primary clause and compliant fare.
    """
    violated: list[str] = []
    escalate_clauses: list[str] = []
    reasons: list[str] = []

    # TP-6.1: Purpose check
    if parsed.purpose_category != PurposeCategory.BUSINESS_ELIGIBLE:
        escalate_clauses.append("TP-6.1")
        reasons.append(f"Purpose '{parsed.purpose_text}' is not business-eligible (TP-6.1)")

    # TP-1.1: Advance booking
    min_days = 14 if parsed.is_international else 7
    if parsed.days_ahead < min_days:
        escalate_clauses.append("TP-1.1")
        reasons.append(
            f"Only {parsed.days_ahead} days advance notice; "
            f"{'international' if parsed.is_international else 'domestic'} requires {min_days} (TP-1.1)"
        )

    # TP-7.1: Trip frequency
    if quarter_trip_count >= 3:
        escalate_clauses.append("TP-7.1")
        reasons.append(f"Employee already has {quarter_trip_count} trips this quarter (TP-7.1)")

    # TP-9.1: High value
    # Check against the requested fare or the cheapest compliant fare
    check_fare = parsed.requested_fare
    if check_fare is None and budget_check.cheapest_compliant:
        check_fare = budget_check.cheapest_compliant.price_inr
    if check_fare is not None and check_fare > 100_000:
        escalate_clauses.append("TP-9.1")
        reasons.append(f"Fare INR {check_fare:,.0f} exceeds 100,000 threshold (TP-9.1)")

    # TP-4.1: Department budget
    if not budget_check.fits_budget and not budget_check.compliant_options:
        escalate_clauses.append("TP-4.1")
        reasons.append(f"No compliant fare fits department remaining budget of INR {budget_check.dept_remaining_inr:,.0f} (TP-4.1)")

    # If any escalation rules fire, ESCALATE
    if escalate_clauses:
        # Primary clause = highest precedence among fired escalation clauses
        primary = _highest_precedence(escalate_clauses)
        secondary = [c for c in escalate_clauses if c != primary]
        # Add any modify-level violations as secondary too
        modify_clauses = _check_modify_violations(parsed, budget_check, employee_grade, fare_options)
        for mc in modify_clauses:
            if mc not in secondary and mc != primary:
                secondary.append(mc)
        return DecisionResult(
            decision=Decision.ESCALATE,
            primary_clause_id=primary,
            secondary_clause_ids=secondary,
            best_compliant_fare=None,
            reasons=reasons,
        )

    # Check for MODIFY conditions
    modify_clauses_list = _check_modify_violations(parsed, budget_check, employee_grade, fare_options)
    if modify_clauses_list and budget_check.cheapest_compliant:
        primary = _highest_precedence(modify_clauses_list)
        secondary = [c for c in modify_clauses_list if c != primary]
        return DecisionResult(
            decision=Decision.MODIFY,
            primary_clause_id=primary,
            secondary_clause_ids=secondary,
            best_compliant_fare=budget_check.cheapest_compliant,
            reasons=reasons + [f"Proposing compliant fare: {budget_check.cheapest_compliant.airline} at INR {budget_check.cheapest_compliant.price_inr:,.0f}"],
        )

    # APPROVE — fully compliant
    # Determine the primary clause (the most relevant one that was checked and passed)
    # Use the fare-related clause as primary since those are what we verified
    primary_clause = _determine_approve_clause(parsed, budget_check, fare_options)
    return DecisionResult(
        decision=Decision.APPROVE,
        primary_clause_id=primary_clause,
        secondary_clause_ids=[],
        best_compliant_fare=budget_check.cheapest_compliant,
        reasons=["Request is fully compliant with all policy clauses"],
    )


def _check_modify_violations(
    parsed: ParsedRequest,
    budget_check: BudgetCheck,
    grade: str,
    fare_options: FareOptions,
) -> list[str]:
    """Check for TP-2.1 and TP-2.2 violations that can be resolved by MODIFY."""
    violations: list[str] = []
    
    # TP-2.1: Class violation
    if parsed.requested_class != CabinClass.ECONOMY:
        # Check if any fare has enough duration to justify non-economy
        max_duration = max((f.duration_hours for f in fare_options.options), default=0)
        if not is_class_allowed(parsed.requested_class, max_duration, grade):
            violations.append("TP-2.1")

    # TP-2.2: Fare cap violation
    if budget_check.over_cap_requested:
        violations.append("TP-2.2")

    return violations


def _highest_precedence(clauses: list[str]) -> str:
    """Return the clause with highest precedence (lowest index in CLAUSE_PRECEDENCE)."""
    best_idx = len(CLAUSE_PRECEDENCE)
    best_clause = clauses[0] if clauses else ""
    for c in clauses:
        if c in CLAUSE_PRECEDENCE:
            idx = CLAUSE_PRECEDENCE.index(c)
            if idx < best_idx:
                best_idx = idx
                best_clause = c
    return best_clause


def _determine_approve_clause(
    parsed: ParsedRequest,
    budget_check: BudgetCheck,
    fare_options: FareOptions,
) -> str:
    """For APPROVE decisions, determine the most relevant clause.
    
    We pick the clause that was most relevant to the compliance check.
    For non-economy requests that pass: TP-2.1.
    For fare-capped checks: TP-2.2.
    Default: TP-2.2 (fare compliance).
    """
    if parsed.requested_class != CabinClass.ECONOMY:
        return "TP-2.1"
    return "TP-2.2"
