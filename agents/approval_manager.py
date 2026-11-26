"""
Approval Manager Agent — the MANAGER in the Semantic Kernel manager-worker architecture.

Coordinates 4 specialized worker agents (registered as Semantic Kernel plugins):
1. Request Parser Agent → structures request & identifies missing fields
2. Policy Retrieval Agent + Fare Search Agent → concurrent RAG & inventory search
3. Budget Checker Agent → deterministic cap & department budget check
4. Rule Engine → deterministic APPROVE / MODIFY / ESCALATE decision
5. Approval / Clarification Flow → interactive follow-ups, HITL queue & ledger updates
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
from core.decision_rules import decide, select_best_fare
from core.memory import MemoryStore
from core.rag import PolicyRAG
from core.schemas import (
    AuditResult,
    BudgetCheck,
    Decision,
    FareOption,
    FareOptions,
    ParsedRequest,
    RequestStatus,
    RetrievedPolicy,
    TripRecord,
    TripStatus,
)
from llm_factory import create_kernel

logger = logging.getLogger(__name__)


class ApprovalManager:
    """Manager agent that orchestrates worker plugins via Semantic Kernel and decides on travel requests.

    Coordination pattern: Manager-Worker with Semantic Kernel plugins, interactive clarification,
    and Human-in-the-Loop (HITL) routing.
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

        # Instantiate worker agents (each with its own SK plugin)
        self.parser = RequestParserAgent(memory, use_llm=use_llm)
        self.policy_agent = PolicyRetrievalAgent(memory, policy_rag, use_llm_rerank=False)
        self.fare_agent = FareSearchAgent(memory)
        self.budget_agent = BudgetCheckerAgent(memory)

        # Build Semantic Kernel and register all 4 worker plugins
        self.kernel = create_kernel(
            plugins={
                "request_parser": self.parser.plugin,
                "policy_retrieval": self.policy_agent.plugin,
                "fare_search": self.fare_agent.plugin,
                "budget_checker": self.budget_agent.plugin,
            }
        )

    async def process_request(
        self,
        raw_text: str,
        allow_clarification: bool = False,
        employee_hint: Optional[str] = None,
        clarifications: Optional[dict] = None,
        request_id: Optional[str] = None,
    ) -> AuditResult:
        """Process a raw travel request end-to-end.

        When allow_clarification=True and required info (employee, origin, destination,
        depart date, or purpose) is missing, pauses BEFORE policy/fare lookup and returns
        Decision.NEEDS_INFO with targeted follow-up questions.
        """
        total_start = time.time()

        # Step 1: Parse via RequestParserAgent
        parsed = await self._delegate_with_retry(
            "request_parser",
            self.parser.parse,
            raw_text,
            request_id=request_id,
            employee_hint=employee_hint,
            clarifications=clarifications,
        )
        if parsed is None:
            return self._error_result("PARSE_FAIL", "Failed to parse request")

        # Step 1b: Interactive Follow-Up Clarification check BEFORE querying fares or policy
        if allow_clarification and parsed.missing_fields:
            clarify_msg = await self._generate_clarification_message(parsed)
            self.memory.write_audit_log(
                parsed.request_id,
                "approval_manager",
                "clarification_requested",
                json.dumps({
                    "missing_fields": parsed.missing_fields,
                    "employee_id": parsed.employee_id,
                    "origin": parsed.origin_iata,
                    "destination": parsed.destination_iata,
                }),
            )
            extracted = {
                "employee_id": parsed.employee_id if parsed.employee_found else "",
                "employee_name": parsed.employee_name,
                "employee_department": parsed.employee_department,
                "employee_grade": parsed.employee_grade,
                "origin_iata": parsed.origin_iata,
                "destination_iata": parsed.destination_iata,
                "depart_date": None if parsed.depart_date_missing else parsed.depart_date.isoformat(),
                "return_date": parsed.return_date.isoformat() if parsed.return_date else None,
                "purpose_text": "" if parsed.purpose_text == "unspecified" else parsed.purpose_text,
                "requested_class": parsed.requested_class.value,
                "requested_fare": parsed.requested_fare,
            }
            logger.info(
                "Request %s paused for follow-up clarification (missing=%s)",
                parsed.request_id,
                parsed.missing_fields,
            )
            return AuditResult(
                request_id=parsed.request_id,
                decision=Decision.NEEDS_INFO,
                primary_clause_id="CLARIFY",
                justification=clarify_msg,
                clarification_message=clarify_msg,
                routed_to_human=False,
                status=RequestStatus.NEEDS_CLARIFICATION,
                raw_request_text=raw_text,
                employee_id=parsed.employee_id if parsed.employee_found else "",
                employee_name=parsed.employee_name,
                employee_department=parsed.employee_department,
                employee_grade=parsed.employee_grade,
                origin_iata=parsed.origin_iata,
                destination_iata=parsed.destination_iata,
                depart_date=None if parsed.depart_date_missing else parsed.depart_date.isoformat(),
                return_date=parsed.return_date.isoformat() if parsed.return_date else None,
                days_ahead=parsed.days_ahead,
                purpose_text=parsed.purpose_text,
                requested_class=parsed.requested_class.value,
                requested_fare_inr=parsed.requested_fare,
                follow_up_questions=parsed.follow_up_questions,
                extracted_partial=extracted,
            )

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
        department = emp.department if emp else "UNKNOWN"
        emp_name = emp.name if emp else (parsed.employee_id or "Unknown Employee")
        quarter_count = self.memory.count_trips_in_quarter(
            parsed.employee_id, parsed.depart_date
        )

        # Read from memory for the decision
        self.memory.write_audit_log(
            parsed.request_id, "approval_manager", "read_quarter_count",
            json.dumps({
                "employee_id": parsed.employee_id,
                "employee_name": emp_name,
                "department": department,
                "quarter_count": quarter_count,
                "grade": grade,
            }),
        )

        decision_result = decide(
            parsed=parsed,
            budget_check=budget,
            quarter_trip_count=quarter_count,
            today=config.TODAY,
            employee_grade=grade,
            fare_options=fares,
        )

        # Pick recommended reference fare for manager visibility when escalated
        recommended_fare: Optional[FareOption] = decision_result.best_compliant_fare
        if recommended_fare is None:
            recommended_fare = budget.cheapest_compliant or select_best_fare(fares.options)

        # Step 5: Generate justification
        justification = await self._generate_justification(
            parsed, decision_result.decision, decision_result.primary_clause_id,
            decision_result.reasons, budget, emp_name,
        )

        # Build complete enriched AuditResult
        result = AuditResult(
            request_id=parsed.request_id,
            decision=decision_result.decision,
            primary_clause_id=decision_result.primary_clause_id,
            secondary_clause_ids=decision_result.secondary_clause_ids,
            best_compliant_fare=decision_result.best_compliant_fare,
            justification=justification,
            routed_to_human=decision_result.decision == Decision.ESCALATE,
            status=RequestStatus.PENDING_HUMAN if decision_result.decision == Decision.ESCALATE else RequestStatus.DECIDED,
            raw_request_text=raw_text,
            employee_id=parsed.employee_id,
            employee_name=emp_name,
            employee_department=department,
            employee_grade=grade,
            origin_iata=parsed.origin_iata,
            destination_iata=parsed.destination_iata,
            depart_date=parsed.depart_date.isoformat(),
            return_date=parsed.return_date.isoformat() if parsed.return_date else None,
            days_ahead=parsed.days_ahead,
            purpose_text=parsed.purpose_text,
            requested_class=parsed.requested_class.value,
            requested_fare_inr=parsed.requested_fare,
            fare_cap_inr=budget.cap_inr,
            dept_remaining_inr=budget.dept_remaining_inr,
            quarter_trip_count=quarter_count,
            available_fares=fares.options,
            recommended_fare=recommended_fare,
        )

        # Step 6: Write final result to audit log
        self.memory.write_audit_log(
            parsed.request_id, "approval_manager", "final_decision",
            result.model_dump_json(),
        )

        # Save to rich booking_requests history table
        self.memory.save_booking_request(result)

        # Add trip record (preserving estimated fare visibility on MODIFY/ESCALATE while keeping APPROVE approved)
        recorded_fare = (
            decision_result.best_compliant_fare.price_inr
            if decision_result.best_compliant_fare
            else (recommended_fare.price_inr if recommended_fare else (parsed.requested_fare or 0.0))
        )
        trip = TripRecord(
            trip_id=parsed.request_id,
            employee_id=parsed.employee_id or "UNKNOWN",
            origin=parsed.origin_iata,
            destination=parsed.destination_iata,
            depart_date=parsed.depart_date,
            fare_inr=recorded_fare,
            status=TripStatus.APPROVED if decision_result.decision == Decision.APPROVE else TripStatus.PENDING,
            clause_id=decision_result.primary_clause_id,
            decided_by="system",
        )
        self.memory.add_trip(trip)

        # Deduct budget if approved
        if decision_result.decision == Decision.APPROVE and decision_result.best_compliant_fare and emp:
            self.memory.deduct_budget(emp.department, decision_result.best_compliant_fare.price_inr)

        # Enqueue rich dossier for HITL if escalated
        if decision_result.decision == Decision.ESCALATE:
            hitl.enqueue(result, self.memory)

        total_time = time.time() - total_start
        logger.info(
            "Request %s processed in %.2fs: %s (%s)",
            parsed.request_id, total_time, result.decision.value, result.primary_clause_id,
        )

        return result

    async def _generate_clarification_message(self, parsed: ParsedRequest) -> str:
        """Generate a concise conversational follow-up question asking for missing fields."""
        understood = []
        if parsed.employee_found:
            understood.append(f"Employee: {parsed.employee_name} ({parsed.employee_id}, {parsed.employee_department})")
        if parsed.origin_iata:
            understood.append(f"From: {parsed.origin_iata}")
        if parsed.destination_iata:
            understood.append(f"To: {parsed.destination_iata}")
        if not parsed.depart_date_missing:
            understood.append(f"Depart: {parsed.depart_date.isoformat()}")
        if parsed.purpose_text and parsed.purpose_text != "unspecified":
            understood.append(f"Purpose: {parsed.purpose_text}")

        missing_q_lines = [
            f"- {q['question']}"
            for q in parsed.follow_up_questions
            if q.get("required", True)
        ]
        summary_str = ", ".join(understood) if understood else "none yet"

        if self.use_llm:
            try:
                from llm_factory import llm_call
                prompt = (
                    f"User's travel request: \"{parsed.raw_text}\"\n"
                    f"Details understood so far: {summary_str}.\n"
                    f"Required missing details:\n" + "\n".join(missing_q_lines) + "\n\n"
                    "Write a helpful, polite 2-3 sentence follow-up response acknowledging what we understood "
                    "and asking the user for the missing information before we search flight fares and audit policy."
                )
                return (await llm_call(
                    prompt,
                    system_message="You are a polite corporate travel concierge agent.",
                    schema_hint="clarify_v1",
                )).strip()
            except Exception as e:
                logger.warning("LLM clarification prompt failed, using template: %s", e)

        prefix = f"Got it ({summary_str}). " if understood else ""
        return (
            f"{prefix}Before I search fares or check company travel policy, please provide: "
            + " ".join(q["question"] for q in parsed.follow_up_questions if q.get("required", True))
        )

    async def _delegate_with_retry(self, worker_name: str, func, *args, **kwargs):
        """Delegate to a worker with one retry on validation failure and audit logging."""
        for attempt in range(2):
            start = time.time()
            try:
                result = await func(*args, **kwargs)
                latency = time.time() - start
                req_id = (
                    kwargs.get("request_id")
                    or (args[0].request_id if args and hasattr(args[0], "request_id") else None)
                    or (result.request_id if hasattr(result, "request_id") else "unknown")
                )
                self.memory.write_audit_log(
                    req_id,
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
                req_id = kwargs.get("request_id") or (args[0].request_id if args and hasattr(args[0], "request_id") else "unknown")
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
        employee_name: str = "",
    ) -> str:
        """Generate a human-readable justification (Gemini LLM with deterministic template guard)."""
        template = self._template_justification(decision, clause_id, reasons, budget, parsed, employee_name)

        if not self.use_llm:
            return template

        try:
            from llm_factory import llm_call

            who = f"{employee_name} ({parsed.employee_id})" if employee_name else parsed.employee_id
            prompt = (
                f"Write a brief 1-2 sentence justification for this travel request decision.\n"
                f"Employee: {who}\n"
                f"Route: {parsed.origin_iata}->{parsed.destination_iata} on {parsed.depart_date}\n"
                f"Purpose: {parsed.purpose_text}, Class: {parsed.requested_class.value}\n"
                f"Decision: {decision.value}\n"
                f"Primary policy clause: {clause_id}\n"
                f"Reasons: {'; '.join(reasons)}\n"
                f"The justification must cite clause {clause_id} and agree with the {decision.value} decision.\n"
                f"Reply with ONLY the justification text, no preamble."
            )
            llm_text = await llm_call(
                prompt,
                system_message="You are a corporate travel policy auditor. Write concise, factual justifications.",
                schema_hint="justification_v2",
            )

            llm_upper = llm_text.upper()
            if decision == Decision.APPROVE and ("DENY" in llm_upper or "REJECT" in llm_upper or "ESCALAT" in llm_upper):
                return template
            if decision == Decision.ESCALATE and "APPROV" in llm_upper and "REQUIR" not in llm_upper:
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
        parsed: Optional[ParsedRequest] = None,
        employee_name: str = "",
    ) -> str:
        """Generate a template-based justification (fallback)."""
        reason_text = "; ".join(reasons) if reasons else "All policy clauses satisfied."
        if decision == Decision.APPROVE:
            return f"Request APPROVED per {clause_id}. {reason_text}"
        elif decision == Decision.MODIFY:
            fare_info = (
                f" Best compliant fare: INR {budget.cheapest_compliant.price_inr:,.0f} ({budget.cheapest_compliant.airline})."
                if budget.cheapest_compliant else ""
            )
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
