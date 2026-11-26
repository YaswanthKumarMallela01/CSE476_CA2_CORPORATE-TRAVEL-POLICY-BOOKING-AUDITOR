"""
Request Parser Agent — structures natural-language travel requests into ParsedRequest.

Features:
- Exposes Semantic Kernel plugin (`RequestParserPlugin` with `@kernel_function`).
- Uses Gemini LLM for structured extraction with accurate rule-based fallback.
- Supports employee lookup by ID (E101) OR name (Aarav Sharma).
- Parses relative dates ('tomorrow', 'next week', 'in 10 days', etc.) relative to config.TODAY.
- Detects missing mandatory fields and builds interactive follow-up clarification questions.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import date, datetime, timedelta
from typing import Annotated, Optional

from semantic_kernel.functions import kernel_function

import config
from core.memory import MemoryStore
from core.schemas import CabinClass, ParsedRequest, PurposeCategory

logger = logging.getLogger(__name__)

# Policy-eligible business purposes + common natural-language corporate phrasings
BUSINESS_PURPOSES = {
    "client meeting",
    "conference",
    "training",
    "project delivery",
    "audit",
}
BUSINESS_ALIASES = {
    "sales demo",
    "client demo",
    "product demo",
    "demo",
    "client visit",
    "customer meeting",
    "business meeting",
    "vendor meeting",
    "workshop",
    "seminar",
    "onsite",
    "site visit",
    "pitch",
    "client pitch",
    "official meeting",
}
PERSONAL_KEYWORDS = {"personal", "vacation", "leisure", "family", "holiday", "pleasure", "tourism", "wedding", "sightseeing"}


class RequestParserPlugin:
    """Semantic Kernel plugin exposing request parsing and clarification capabilities."""

    def __init__(self, agent: "RequestParserAgent") -> None:
        self.agent = agent

    @kernel_function(
        name="parse_travel_request",
        description="Parse a natural-language travel request into structured itinerary, employee, and policy attributes.",
    )
    async def parse_travel_request_sk(
        self,
        raw_text: Annotated[str, "Raw natural language travel request"],
        employee_hint: Annotated[Optional[str], "Optional employee ID override"] = None,
    ) -> str:
        parsed = await self.agent.parse(raw_text, employee_hint=employee_hint)
        return parsed.model_dump_json()


class RequestParserAgent:
    """Parses raw text travel requests into structured ParsedRequest objects.

    Memory interactions:
    - Reads: employee directory (by ID or name)
    - Writes: parsed request and access audit events to audit_log
    """

    def __init__(self, memory: MemoryStore, use_llm: bool = True) -> None:
        self.memory = memory
        self.use_llm = use_llm
        self.plugin = RequestParserPlugin(self)

    async def parse(
        self,
        raw_text: str,
        request_id: Optional[str] = None,
        employee_hint: Optional[str] = None,
        clarifications: Optional[dict] = None,
    ) -> ParsedRequest:
        """Parse a raw travel request (plus optional context/clarifications) into ParsedRequest."""
        req_id = request_id or f"REQ-{uuid.uuid4().hex[:8].upper()}"
        clarifications = clarifications or {}
        parsed_data: Optional[dict] = None

        if self.use_llm and raw_text.strip():
            try:
                parsed_data = await self._llm_parse(raw_text)
            except Exception as e:
                logger.warning("LLM parse failed, falling back to regex: %s", e)

        regex_data = self._regex_parse(raw_text)
        if not parsed_data or not isinstance(parsed_data, dict):
            logger.info("Using rule-based parser")
            parsed_data = regex_data
        else:
            # Fill missing keys cleanly from regex without overwriting LLM values
            for k, v in regex_data.items():
                if not parsed_data.get(k) and v:
                    parsed_data[k] = v

        # Apply structured clarifications / overrides
        if employee_hint and employee_hint.strip():
            parsed_data["employee_id"] = employee_hint.strip()
        for key in ("employee_id", "origin", "destination", "depart_date", "return_date", "purpose_text", "requested_class", "requested_fare"):
            if key in clarifications and clarifications[key] not in (None, ""):
                parsed_data[key] = clarifications[key]

        # Resolve Employee ID by ID or Name from MemoryStore
        raw_emp = str(parsed_data.get("employee_id") or "").strip()
        emp = self.memory.get_employee_by_name_or_id(raw_emp) if raw_emp else None
        if not emp:
            emp = self._find_employee_in_text(raw_text)
        employee_id = emp.employee_id if emp else raw_emp.upper()

        # Resolve city names to IATA (no silent fabricated defaults!)
        raw_origin = str(parsed_data.get("origin") or "").strip()
        raw_dest = str(parsed_data.get("destination") or "").strip()
        origin = self._resolve_iata(raw_origin) if raw_origin else ""
        destination = self._resolve_iata(raw_dest) if raw_dest else ""

        # Guard if origin == destination due to single-city confusion
        if origin and destination and origin == destination:
            destination = ""

        # Parse dates
        depart_date_val = parsed_data.get("depart_date")
        depart_date = self._parse_date(str(depart_date_val)) if depart_date_val else None
        depart_date_missing = depart_date is None

        return_date_val = parsed_data.get("return_date")
        return_date = self._parse_date(str(return_date_val)) if return_date_val else None

        # Purpose
        purpose_text = str(parsed_data.get("purpose_text") or "unspecified").strip()
        if not purpose_text or purpose_text.lower() in ("none", "null", "n/a"):
            purpose_text = "unspecified"
        purpose_category = self._classify_purpose(purpose_text)

        # Class
        raw_class = str(parsed_data.get("requested_class") or "economy").lower().strip()
        if "business" in raw_class:
            requested_class = CabinClass.BUSINESS
        elif "premium" in raw_class:
            requested_class = CabinClass.PREMIUM_ECONOMY
        else:
            requested_class = CabinClass.ECONOMY

        # Fare
        requested_fare = parsed_data.get("requested_fare")
        if requested_fare is not None:
            try:
                requested_fare = float(
                    str(requested_fare).replace(",", "").replace("INR", "").replace("₹", "").strip()
                )
            except (ValueError, TypeError):
                requested_fare = None

        # Compute derived fields IN CODE
        effective_depart = depart_date or config.TODAY
        is_international = (
            bool(origin and destination)
            and not (origin in config.INDIAN_IATA and destination in config.INDIAN_IATA)
        )
        days_ahead = (effective_depart - config.TODAY).days

        # Read & log employee access in shared memory
        if emp:
            self.memory.write_employee_access(emp.employee_id, req_id)
        else:
            logger.warning("Employee '%s' not found/missing in memory", employee_id or "<empty>")

        # Check completeness & generate follow-up questions
        missing_fields, follow_ups = self._build_follow_up_questions(
            raw_text=raw_text,
            employee_id=employee_id,
            emp_obj=emp,
            origin=origin,
            destination=destination,
            depart_date_missing=depart_date_missing,
            return_date=return_date,
            purpose_text=purpose_text,
            requested_class_explicit=("business" in raw_text.lower() or "economy" in raw_text.lower() or "premium" in raw_text.lower() or "requested_class" in clarifications),
        )

        parsed = ParsedRequest(
            request_id=req_id,
            employee_id=employee_id,
            origin_iata=origin,
            destination_iata=destination,
            depart_date=effective_depart,
            return_date=return_date,
            purpose_text=purpose_text,
            purpose_category=purpose_category,
            requested_class=requested_class,
            requested_fare=requested_fare,
            is_international=is_international,
            days_ahead=days_ahead,
            raw_text=raw_text,
            employee_name=emp.name if emp else "",
            employee_department=emp.department if emp else "",
            employee_grade=emp.grade if emp else "",
            employee_found=bool(emp),
            depart_date_missing=depart_date_missing,
            missing_fields=missing_fields,
            follow_up_questions=follow_ups,
        )

        # Write to shared memory (audit log)
        self.memory.write_audit_log(
            req_id,
            "request_parser",
            "parsed_request",
            parsed.model_dump_json(),
        )
        logger.info(
            "Parsed request %s: emp=%s, %s->%s on %s (missing=%s)",
            req_id, employee_id or "?", origin or "?", destination or "?",
            effective_depart if not depart_date_missing else "?",
            missing_fields or "none",
        )
        return parsed

    def _find_employee_in_text(self, raw_text: str):
        """Attempt to match an employee ID or name directly against SQLite employee directory."""
        m = re.search(r"\b([eE]\d{3})\b", raw_text)
        if m:
            emp = self.memory.get_employee(m.group(1).upper())
            if emp:
                return emp
        text_lower = raw_text.lower()
        for emp in self.memory.get_all_employees():
            if emp.name.lower() in text_lower:
                return emp
            first_name = emp.name.split()[0].lower()
            if re.search(r"\b" + re.escape(first_name) + r"\b", text_lower):
                return emp
        return None

    def _build_follow_up_questions(
        self,
        raw_text: str,
        employee_id: str,
        emp_obj,
        origin: str,
        destination: str,
        depart_date_missing: bool,
        return_date: Optional[date],
        purpose_text: str,
        requested_class_explicit: bool,
    ) -> tuple[list[str], list[dict]]:
        """Identify missing essential booking parameters before policy/fare lookup."""
        missing: list[str] = []
        questions: list[dict] = []

        all_emps = self.memory.get_all_employees()
        emp_options = [
            {
                "value": e.employee_id,
                "label": f"{e.employee_id} · {e.name} ({e.department}, {e.grade})",
            }
            for e in all_emps
        ]
        city_options = [
            {"value": code, "label": f"{config.IATA_TO_CITY.get(code, code)} ({code})"}
            for code in sorted(
                config.INDIAN_IATA | config.INTERNATIONAL_IATA,
                key=lambda c: (0 if c in ("DEL", "BOM", "BLR", "HYD", "CCU", "GOI") else 1, config.IATA_TO_CITY.get(c, c)),
            )
        ]

        if not emp_obj:
            missing.append("employee_id")
            if employee_id:
                q_text = f"Employee '{employee_id}' was not found in the corporate directory. Please select who is requesting this trip:"
            else:
                q_text = "Which employee is making this travel request?"
            questions.append({
                "field": "employee_id",
                "question": q_text,
                "type": "select",
                "required": True,
                "options": emp_options,
            })

        if not origin:
            missing.append("origin_iata")
            questions.append({
                "field": "origin_iata",
                "question": "Which city are you departing from (Origin)?",
                "type": "select_city",
                "required": True,
                "options": city_options,
            })

        if not destination:
            missing.append("destination_iata")
            questions.append({
                "field": "destination_iata",
                "question": "Which destination city are you flying to?",
                "type": "select_city",
                "required": True,
                "options": city_options,
            })

        if depart_date_missing:
            missing.append("depart_date")
            questions.append({
                "field": "depart_date",
                "question": "What is your intended departure date?",
                "type": "date",
                "required": True,
                "default": (config.TODAY + timedelta(days=10)).isoformat(),
            })

        if purpose_text.lower() == "unspecified":
            missing.append("purpose_text")
            questions.append({
                "field": "purpose_text",
                "question": "What is the purpose of this trip? (e.g., client meeting, conference, training, project delivery, audit)",
                "type": "select_or_text",
                "required": True,
                "options": [
                    {"value": "client meeting", "label": "Client Meeting / Sales Demo"},
                    {"value": "conference", "label": "Conference / Seminar"},
                    {"value": "training", "label": "Training / Workshop"},
                    {"value": "project delivery", "label": "Project Delivery / Onsite"},
                    {"value": "audit", "label": "Corporate Audit"},
                    {"value": "personal", "label": "Personal / Leisure (Requires Exception)"},
                ],
            })

        # Helpful optional follow-ups when we already need user clarification
        if missing:
            if return_date is None:
                questions.append({
                    "field": "return_date",
                    "question": "Return date (optional — helps verify round-trip fare caps):",
                    "type": "date",
                    "required": False,
                })
            if not requested_class_explicit:
                questions.append({
                    "field": "requested_class",
                    "question": "Preferred cabin class? (Economy is default per policy TP-2.1)",
                    "type": "select",
                    "required": False,
                    "options": [
                        {"value": "economy", "label": "Economy (Standard Policy)"},
                        {"value": "premium_economy", "label": "Premium Economy (Grade L5+, ≥6h flight)"},
                        {"value": "business", "label": "Business Class (Grade L5+, ≥6h flight)"},
                    ],
                })

        return missing, questions

    async def _llm_parse(self, raw_text: str) -> dict:
        """Use Gemini LLM for structured extraction without fabricating missing fields."""
        from llm_factory import llm_json_call

        emps_summary = ", ".join(f"{e.employee_id}={e.name}" for e in self.memory.get_all_employees())
        system_msg = (
            f"You are a precise corporate travel request parser. Reference TODAY's date is {config.TODAY.isoformat()}.\n"
            f"Known employees: {emps_summary}.\n"
            "Extract ONLY what is explicitly present in the user's message. Do NOT guess or invent missing cities, dates, or employees.\n"
            "Return a JSON object with these fields:\n"
            "- employee_id: string or null (e.g., 'E101' or null if employee ID/name not mentioned)\n"
            "- origin: string or null (departure city name or IATA code; null if 'from' city not stated)\n"
            "- destination: string or null (arrival city name or IATA code; null if not stated)\n"
            "- depart_date: string or null (YYYY-MM-DD; convert relative dates like 'tomorrow' using TODAY's date; null if no date mentioned)\n"
            "- return_date: string or null (YYYY-MM-DD)\n"
            "- purpose_text: string or null (exact stated purpose e.g. 'sales demo', 'client meeting', 'family vacation', or null if omitted)\n"
            "- requested_class: string ('economy', 'business', or 'premium_economy')\n"
            "- requested_fare: number or null (fare amount in INR if mentioned)\n"
            "Return ONLY valid JSON."
        )
        result = await llm_json_call(raw_text, system_message=system_msg, schema_hint="parse_request_v2")
        return result

    def _regex_parse(self, raw_text: str) -> dict:
        """Rule-based parser with directional preposition awareness and relative date resolution."""
        text = raw_text.lower()
        result: dict = {}

        # Employee ID or Name
        emp = self._find_employee_in_text(raw_text)
        if emp:
            result["employee_id"] = emp.employee_id
        else:
            emp_match = re.search(r"\b([eE]\d{3})\b", raw_text)
            if emp_match:
                result["employee_id"] = emp_match.group(1).upper()

        # Directional Origin & Destination resolution
        city_names = sorted(config.CITY_TO_IATA.keys(), key=len, reverse=True)
        city_alt = "|".join(re.escape(c) for c in city_names)

        # Look for explicit 'from <city>' and 'to <city>'
        from_m = re.search(rf"\bfrom\s+({city_alt})\b", text)
        to_m = re.search(rf"\bto\s+({city_alt})\b", text)

        if from_m:
            result["origin"] = from_m.group(1)
        if to_m:
            result["destination"] = to_m.group(1)

        # Look for '<origin> to <destination>' or all ordered city appearances if needed
        found_cities: list[tuple[int, str]] = []
        used_spans: list[tuple[int, int]] = []
        for c in city_names:
            for m in re.finditer(r"\b" + re.escape(c) + r"\b", text):
                if any(not (m.end() <= s or m.start() >= e) for s, e in used_spans):
                    continue
                used_spans.append((m.start(), m.end()))
                found_cities.append((m.start(), c))
        found_cities.sort(key=lambda x: x[0])

        if len(found_cities) >= 2:
            if "origin" not in result:
                result["origin"] = found_cities[0][1]
            if "destination" not in result:
                # Pick first distinct city from origin
                for _, cname in found_cities:
                    if config.CITY_TO_IATA.get(cname) != config.CITY_TO_IATA.get(result.get("origin", "")):
                        result["destination"] = cname
                        break
        elif len(found_cities) == 1 and "origin" not in result and "destination" not in result:
            # Ambiguous single city without 'from'/'to' — treat as destination by default unless 'departing'/'leaving'
            cname = found_cities[0][1]
            if re.search(rf"\b(departing|leaving)\s+{re.escape(cname)}\b", text):
                result["origin"] = cname
            else:
                result["destination"] = cname

        # Dates (absolute and relative)
        dates_found: list[date] = []
        for m in re.finditer(r"\b(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})\b", raw_text):
            d = self._parse_date(m.group(1))
            if d:
                dates_found.append(d)
        for m in re.finditer(r"\b(\d{4}-\d{2}-\d{2})\b", raw_text):
            d = self._parse_date(m.group(1))
            if d:
                dates_found.append(d)

        # Day-Month without year (e.g., "15 Nov", "3 Dec")
        if not dates_found:
            for m in re.finditer(r"\b(\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*)\b", text):
                d = self._parse_date(f"{m.group(1)} {config.TODAY.year}")
                if d:
                    dates_found.append(d)

        # Relative date keywords
        if not dates_found:
            if "day after tomorrow" in text:
                dates_found.append(config.TODAY + timedelta(days=2))
            elif "tomorrow" in text:
                dates_found.append(config.TODAY + timedelta(days=1))
            elif re.search(r"\btoday\b", text):
                dates_found.append(config.TODAY)
            elif "next week" in text:
                dates_found.append(config.TODAY + timedelta(days=7))
            else:
                in_days = re.search(r"\bin\s+(\d+)\s+days?\b", text)
                if in_days:
                    dates_found.append(config.TODAY + timedelta(days=int(in_days.group(1))))

        if dates_found:
            result["depart_date"] = dates_found[0].isoformat()
            if len(dates_found) > 1:
                result["return_date"] = dates_found[1].isoformat()

        # Purpose matching
        all_purposes = sorted(
            list(BUSINESS_PURPOSES | BUSINESS_ALIASES | PERSONAL_KEYWORDS),
            key=len,
            reverse=True,
        )
        for kw in all_purposes:
            if re.search(r"\b" + re.escape(kw) + r"\b", text):
                result["purpose_text"] = kw
                break

        if "purpose_text" not in result:
            m_purp = re.search(r"(?:purpose\s*[:\s]+|for\s+(?:a|an|my)?\s*)([a-zA-Z0-9\s]{3,40})(?:[.,\n]|$)", raw_text, re.IGNORECASE)
            if m_purp:
                cand = m_purp.group(1).strip()
                # Ignore false positives like "for 2 days" or "for E101"
                if not re.match(r"^(\d+|e\d{3}|economy|business)", cand.lower()):
                    result["purpose_text"] = cand

        if "purpose_text" not in result:
            result["purpose_text"] = "unspecified"

        # Class
        if "business" in text:
            result["requested_class"] = "business"
        elif "premium" in text:
            result["requested_class"] = "premium_economy"
        else:
            result["requested_class"] = "economy"

        # Fare
        fare_matches = re.findall(r"(?:inr|rs\.?|₹)\s*(\d{1,3}(?:,\d{3})+|\d{4,6})", text)
        fares_found = []
        for fm in fare_matches:
            try:
                val = float(fm.replace(",", "").strip())
                if val != 2026 and val >= 1000:
                    fares_found.append(val)
            except ValueError:
                pass
        if fares_found:
            result["requested_fare"] = fares_found[0]

        return result

    def _resolve_iata(self, city_or_iata: str) -> str:
        """Resolve a city name or IATA code to a standard IATA code."""
        cleaned = city_or_iata.strip().upper()
        if not cleaned:
            return ""
        if cleaned in config.INDIAN_IATA | config.INTERNATIONAL_IATA:
            return cleaned
        lower = city_or_iata.strip().lower()
        iata = config.CITY_TO_IATA.get(lower)
        if iata:
            return iata
        for city, code in config.CITY_TO_IATA.items():
            if city in lower or lower in city:
                return code
        logger.warning("Could not resolve IATA for: %s", city_or_iata)
        return ""

    def _parse_date(self, date_str: str) -> Optional[date]:
        """Parse an explicit or relative date string."""
        if not date_str:
            return None
        s = date_str.strip()
        sl = s.lower()
        if sl == "tomorrow":
            return config.TODAY + timedelta(days=1)
        if sl == "today":
            return config.TODAY
        if sl == "day after tomorrow":
            return config.TODAY + timedelta(days=2)

        for fmt in ["%Y-%m-%d", "%d %b %Y", "%d %B %Y", "%d/%m/%Y", "%m/%d/%Y"]:
            try:
                return datetime.strptime(s, fmt).date()
            except ValueError:
                continue
        return None

    def _classify_purpose(self, purpose_text: str) -> PurposeCategory:
        """Classify purpose text into BUSINESS_ELIGIBLE, PERSONAL, or UNSPECIFIED."""
        lower = purpose_text.lower().strip()
        if not lower or lower == "unspecified":
            return PurposeCategory.UNSPECIFIED
        for kw in PERSONAL_KEYWORDS:
            if kw in lower:
                return PurposeCategory.PERSONAL
        for kw in BUSINESS_PURPOSES | BUSINESS_ALIASES:
            if kw in lower:
                return PurposeCategory.BUSINESS_ELIGIBLE
        return PurposeCategory.UNSPECIFIED
