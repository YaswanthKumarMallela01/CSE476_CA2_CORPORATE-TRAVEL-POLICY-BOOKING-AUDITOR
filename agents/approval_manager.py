"""
Approval Manager Agent — the MANAGER in the manager-worker architecture.

Orchestrates all four worker agents:
1. Request Parser → parse raw text
2. Policy Retrieval + Fare Search → concurrent retrieval
3. Budget Checker → compliance filtering
4. Decision Rules → deterministic verdict
5. Optional LLM justification

Delegates structured inputs, validates outputs, retries once on failure.
Logs every delegation to the audit log.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Optional

import config
from agents.budget_checker_agent import BudgetCheckerAgent
from agents.fare_search_agent import FareSearchAgent
from agents.policy_retrieval_agent import PolicyRetrievalAgent
from agents.request_parser_agent import RequestParserAgent
from core import hitl
from core.decision_rules import decide
from core.memory import MemoryStore
from core.rag import PolicyRAG
from core.schemas import (
    AuditResult,
    BudgetCheck,
    Decision,
    FareOptions,
    ParsedRequest,
    RequestStatus,
    RetrievedPolicy,
    TripRecord,
    TripStatus,
)

logger = logging.getLogger(__name__)


class ApprovalManager:
    """Manager agent that orchestrates worker agents and decides on travel requests.
    
    Coordination pattern: Manager-Worker with explicit delegation.
    
    Memory interactions:
    - Reads: all context assembled from workers; employee data; trip history for quarter count
    - Writes: final AuditResult; delegation logs; trip record; HITL enqueue
    """

    def __init__(
        self,
        memory: MemoryStore,
        policy_rag: PolicyRAG,
        use_llm: bool = True,
    ) -> None:
        self.memory = memory
        self.policy_rag = policy_rag
        self.use_llm = use_llm

        # Instantiate worker agents
        self.parser = RequestParserAgent(memory, use_llm=use_llm)
        self.policy_agent = PolicyRetrievalAgent(memory, policy_rag, use_llm_rerank=use_llm)
        self.fare_agent = FareSearchAgent(memory)
        self.budget_agent = BudgetCheckerAgent(memory)

    async def process_request(self, raw_text: str) -> AuditResult:
        """Process a raw travel request end-to-end.
        
        Steps:
        1. Parse the request
        2. Retrieve policy + search fares (concurrent)
        3. Check budget
        4. Apply decision rules
        5. Generate justification
        6. Enqueue for human review if escalated
        
        Returns: AuditResult with decision, clause, and compliant fare.
        """
        total_start = time.time()

        # Step 1: Parse
        parsed = await self._delegate_with_retry(
            "request_parser", self.parser.parse, raw_text,
        )
        if parsed is None:
            return self._error_result("PARSE_FAIL", "Failed to parse request")

        # Step 2: Retrieve policy + search fares concurrently
        policy_task = self._delegate_with_retry(
            "policy_retrieval", self.policy_agent.retrieve, parsed,
        )
        fare_task = self._delegate_with_retry(
            "fare_search", self.fare_agent.search, parsed,
        )
        policy, fares = await asyncio.gather(policy_task, fare_task)

        if policy is None:
            policy = RetrievedPolicy()
        if fares is None:
            fares = FareOptions()

        # Step 3: Budget check
        budget = await self._delegate_with_retry(
            "budget_checker", self.budget_agent.check, parsed, fares, policy,
        )
        if budget is None:
            budget = BudgetCheck()

        # Step 4: Decision rules (deterministic)
        emp = self.memory.get_employee(parsed.employee_id)
        grade = emp.grade if emp else "L1"
        quarter_count = self.memory.count_trips_in_quarter(
            parsed.employee_id, parsed.depart_date
        )

        # Read from memory for the decision (fulfills "reads shared memory" requirement)
        self.memory.write_audit_log(
            parsed.request_id, "approval_manager", "read_quarter_count",
            json.dumps({"employee_id": parsed.employee_id, "quarter_count": quarter_count, "grade": grade}),
        )

        decision_result = decide(
            parsed=parsed,
            budget_check=budget,
            quarter_trip_count=quarter_count,
            today=config.TODAY,
            employee_grade=grade,
            fare_options=fares,
        )

        # Step 5: Generate justification
        justification = await self._generate_justification(
            parsed, decision_result.decision, decision_result.primary_clause_id,
            decision_result.reasons, budget,
        )

        # Build final result
        result = AuditResult(
            request_id=parsed.request_id,
            decision=decision_result.decision,
            primary_clause_id=decision_result.primary_clause_id,
            secondary_clause_ids=decision_result.secondary_clause_ids,
            best_compliant_fare=decision_result.best_compliant_fare,
            justification=justification,
            routed_to_human=decision_result.decision == Decision.ESCALATE,
            status=RequestStatus.PENDING_HUMAN if decision_result.decision == Decision.ESCALATE else RequestStatus.DECIDED,
        )

        # Step 6: Write final result and handle escalation
        self.memory.write_audit_log(
            parsed.request_id, "approval_manager", "final_decision",
            result.model_dump_json(),
        )

        # Add trip record
        trip = TripRecord(
            trip_id=parsed.request_id,
            employee_id=parsed.employee_id,
            origin=parsed.origin_iata,
            destination=parsed.destination_iata,
            depart_date=parsed.depart_date,
            fare_inr=decision_result.best_compliant_fare.price_inr if decision_result.best_compliant_fare else 0,
            status=TripStatus.APPROVED if decision_result.decision == Decision.APPROVE else TripStatus.PENDING,
            clause_id=decision_result.primary_clause_id,
            decided_by="system",
        )
        self.memory.add_trip(trip)

        # Deduct budget if approved
        if decision_result.decision == Decision.APPROVE and decision_result.best_compliant_fare and emp:
            self.memory.deduct_budget(emp.department, decision_result.best_compliant_fare.price_inr)

        # Enqueue for HITL if escalated
        if decision_result.decision == Decision.ESCALATE:
            hitl.enqueue(result, self.memory)

        total_time = time.time() - total_start
        logger.info(
            "Request %s processed in %.2fs: %s (%s)",
            parsed.request_id, total_time, result.decision.value, result.primary_clause_id,
        )

        return result

    async def _delegate_with_retry(self, worker_name: str, func, *args, **kwargs):
        """Delegate to a worker with one retry on validation failure.
        
        Logs the delegation: who, what, input hash, output summary, latency.
        """
        for attempt in range(2):
            start = time.time()
            try:
                result = await func(*args, **kwargs)
                latency = time.time() - start
                # Log delegation
                self.memory.write_audit_log(
                    args[0].request_id if hasattr(args[0], 'request_id') else "unknown",
                    "approval_manager",
                    f"delegate_{worker_name}",
                    json.dumps({
                        "worker": worker_name,
                        "attempt": attempt + 1,
                        "latency_s": round(latency, 3),
                        "success": True,
                    }),
                )
                return result
            except Exception as e:
                latency = time.time() - start
                logger.warning(
                    "Worker %s attempt %d failed (%.2fs): %s",
                    worker_name, attempt + 1, latency, e,
                )
                if attempt == 0:
                    continue
                # Log failure
                try:
                    req_id = args[0].request_id if hasattr(args[0], 'request_id') else "unknown"
                except Exception:
                    req_id = "unknown"
                self.memory.write_audit_log(
                    req_id, "approval_manager", f"delegate_{worker_name}_failed",
                    json.dumps({"worker": worker_name, "error": str(e)[:200]}),
                )
                return None

    async def _generate_justification(
        self,
        parsed: ParsedRequest,
        decision: Decision,
        clause_id: str,
        reasons: list[str],
        budget: BudgetCheck,
    ) -> str:
        """Generate a human-readable justification.
        
        Uses LLM if available; falls back to template.
        If LLM contradicts the rule engine verdict, discard and use template.
        """
        template = self._template_justification(decision, clause_id, reasons, budget)

        if not self.use_llm:
            return template

        try:
            from llm_factory import llm_call

            prompt = (
                f"Write a brief 1-2 sentence justification for this travel request decision.\n"
                f"Decision: {decision.value}\n"
                f"Primary policy clause: {clause_id}\n"
                f"Reasons: {'; '.join(reasons)}\n"
                f"Employee: {parsed.employee_id}, Route: {parsed.origin_iata}->{parsed.destination_iata}\n"
                f"The justification must cite clause {clause_id} and agree with the {decision.value} decision.\n"
                f"Reply with ONLY the justification text, no preamble."
            )
            llm_text = await llm_call(
                prompt,
                system_message="You are a corporate travel policy auditor. Write concise justifications.",
                schema_hint="justification_v1",
            )

            # Validate LLM doesn't contradict the decision
            llm_upper = llm_text.upper()
            if decision == Decision.APPROVE and ("DENY" in llm_upper or "REJECT" in llm_upper or "ESCALAT" in llm_upper):
                logger.warning("LLM justification contradicts APPROVE decision; using template")
                return template
            if decision == Decision.ESCALATE and "APPROV" in llm_upper and "REQUIR" not in llm_upper:
                logger.warning("LLM justification contradicts ESCALATE decision; using template")
                return template

            return llm_text.strip()

        except Exception as e:
            logger.warning("LLM justification failed, using template: %s", e)
            return template

    def _template_justification(
        self,
        decision: Decision,
        clause_id: str,
        reasons: list[str],
        budget: BudgetCheck,
    ) -> str:
        """Generate a template-based justification (fallback)."""
        reason_text = "; ".join(reasons) if reasons else "All policy clauses satisfied."
        if decision == Decision.APPROVE:
            return f"Request APPROVED per {clause_id}. {reason_text}"
        elif decision == Decision.MODIFY:
            fare_info = f" Best compliant fare: INR {budget.cheapest_compliant.price_inr:,.0f} ({budget.cheapest_compliant.airline})." if budget.cheapest_compliant else ""
            return f"Request requires MODIFICATION per {clause_id}. {reason_text}{fare_info}"
        else:
            return f"Request ESCALATED to manager per {clause_id}. {reason_text}"

    def _error_result(self, request_id: str, error: str) -> AuditResult:
        """Create an error AuditResult."""
        return AuditResult(
            request_id=request_id,
            decision=Decision.ESCALATE,
            primary_clause_id="ERROR",
            justification=f"Error: {error}",
            routed_to_human=True,
            status=RequestStatus.PENDING_HUMAN,
        )
