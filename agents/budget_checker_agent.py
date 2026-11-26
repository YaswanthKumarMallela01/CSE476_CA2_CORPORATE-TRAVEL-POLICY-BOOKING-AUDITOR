"""
Budget Checker Agent — applies fare caps, class rules, carrier preference, and budget checks.

All arithmetic is deterministic in code. No LLM calls.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from semantic_kernel.functions import kernel_function

from core.decision_rules import (
    CONTRACTED_AIRLINES,
    filter_compliant_fares,
    get_fare_cap,
    is_class_allowed,
    select_best_fare,
)
from core.memory import MemoryStore
from core.schemas import (
    BudgetCheck,
    CabinClass,
    FareOption,
    FareOptions,
    ParsedRequest,
    RetrievedPolicy,
)

logger = logging.getLogger(__name__)


class BudgetCheckerPlugin:
    """Semantic Kernel plugin exposing deterministic fare cap and department budget checking."""

    def __init__(self, agent: "BudgetCheckerAgent") -> None:
        self.agent = agent

    @kernel_function(
        name="check_budget_and_caps",
        description="Check fare caps, travel class eligibility, preferred carriers, and remaining department budget.",
    )
    async def check_budget_and_caps_sk(
        self,
        parsed_json: str,
        fares_json: str,
        policy_json: str,
    ) -> str:
        parsed = ParsedRequest.model_validate_json(parsed_json)
        fares = FareOptions.model_validate_json(fares_json)
        policy = RetrievedPolicy.model_validate_json(policy_json)
        res = await self.agent.check(parsed, fares, policy)
        return res.model_dump_json()


class BudgetCheckerAgent:
    """Checks fare compliance, caps, and department budget.
    
    Memory interactions:
    - Reads: department budget ledger, trip history
    - Writes: budget check result to audit log
    """

    def __init__(self, memory: MemoryStore) -> None:
        self.memory = memory
        self.plugin = BudgetCheckerPlugin(self)

    async def check(
        self,
        parsed: ParsedRequest,
        fare_options: FareOptions,
        policy: RetrievedPolicy,
    ) -> BudgetCheck:
        """Run budget and compliance checks.
        
        Applies TP-2.1 (class), TP-2.2 (fare cap), TP-4.1 (budget), TP-5.1 (carrier).
        All logic is deterministic.
        """
        # Read department budget from memory
        emp = self.memory.get_employee(parsed.employee_id)
        department = emp.department if emp else "UNKNOWN"
        dept_remaining = self.memory.get_department_budget(department)
        grade = emp.grade if emp else "L1"

        # Read trip history from memory
        trips = self.memory.get_trips_for_employee(parsed.employee_id)
        self.memory.write_audit_log(
            parsed.request_id, "budget_checker", "read_budget_and_history",
            json.dumps({
                "department": department,
                "remaining_inr": dept_remaining,
                "employee_grade": grade,
                "trip_count": len(trips),
            }),
        )

        # Determine the fare cap
        # Use the appropriate cabin class for cap lookup
        # If requested class is not economy, check if it's allowed first
        requested_cabin = parsed.requested_class.value
        cap = get_fare_cap(requested_cabin, parsed.is_international)

        # If no specific cap found (e.g., business domestic), check economy
        if cap is None:
            cap = get_fare_cap("economy", parsed.is_international) or 0.0

        cap_clause = "TP-2.2"

        # Check if requested fare exceeds cap
        over_cap = False
        if parsed.requested_fare and cap:
            over_cap = parsed.requested_fare > cap

        # Filter compliant fares
        compliant = filter_compliant_fares(
            fares=fare_options.options,
            is_international=parsed.is_international,
            grade=grade,
            dept_remaining=dept_remaining,
            requested_cabin=parsed.requested_class,
        )

        best = select_best_fare(compliant)
        fits_budget = len(compliant) > 0

        reasons: list[str] = []
        if over_cap:
            reasons.append(f"Requested fare exceeds {cap_clause} cap of INR {cap:,.0f}")
        if not fits_budget:
            reasons.append(f"No compliant fare fits department budget of INR {dept_remaining:,.0f}")
        if best:
            reasons.append(f"Best compliant fare: {best.airline} INR {best.price_inr:,.0f}")

        result = BudgetCheck(
            cap_inr=cap or 0.0,
            cap_clause=cap_clause,
            dept_remaining_inr=dept_remaining,
            compliant_options=compliant,
            cheapest_compliant=best,
            fits_budget=fits_budget,
            over_cap_requested=over_cap,
            reasons=reasons,
        )

        # Write to memory
        self.memory.write_audit_log(
            parsed.request_id, "budget_checker", "budget_check_result",
            json.dumps({
                "cap_inr": cap,
                "dept_remaining": dept_remaining,
                "compliant_count": len(compliant),
                "cheapest_price": best.price_inr if best else None,
                "fits_budget": fits_budget,
                "over_cap": over_cap,
            }),
        )

        logger.info(
            "Budget check for %s: cap=%.0f, remaining=%.0f, compliant=%d, fits=%s",
            parsed.request_id, cap or 0, dept_remaining, len(compliant), fits_budget,
        )
        return result
