"""
Request Parser Agent — structures natural-language travel requests into ParsedRequest.

Uses the LLM for structured extraction; falls back to regex/rule-based parsing
if the LLM is unavailable or returns invalid JSON.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import date, datetime
from typing import Optional

import config
from core.memory import MemoryStore
from core.schemas import CabinClass, ParsedRequest, PurposeCategory

logger = logging.getLogger(__name__)

# Business-eligible purposes
BUSINESS_PURPOSES = {"client meeting", "conference", "training", "project delivery", "audit"}
PERSONAL_KEYWORDS = {"personal", "vacation", "leisure", "family", "holiday", "pleasure", "tourism"}


class RequestParserAgent:
    """Parses raw text travel requests into structured ParsedRequest objects.
    
    Uses LLM for extraction with regex fallback.
    Memory interactions:
    - Reads: employee profile
    - Writes: parsed request to audit log
    """

    def __init__(self, memory: MemoryStore, use_llm: bool = True) -> None:
        self.memory = memory
        self.use_llm = use_llm

    async def parse(self, raw_text: str) -> ParsedRequest:
        """Parse a raw travel request into a structured ParsedRequest.
        
        Tries LLM extraction first, falls back to regex.
        Always validates and recomputes is_international and days_ahead in code.
        """
        request_id = f"REQ-{uuid.uuid4().hex[:8].upper()}"
        parsed_data: Optional[dict] = None

        if self.use_llm:
            try:
                parsed_data = await self._llm_parse(raw_text)
            except Exception as e:
                logger.warning("LLM parse failed, falling back to regex: %s", e)

        if not parsed_data or not parsed_data.get("employee_id"):
            logger.info("Using rule-based parser")
            parsed_data = self._regex_parse(raw_text)

        # Build the ParsedRequest
        employee_id = parsed_data.get("employee_id", "").upper().strip()

        # Resolve city names to IATA
        origin = self._resolve_iata(parsed_data.get("origin", ""))
        destination = self._resolve_iata(parsed_data.get("destination", ""))

        # Parse dates
        depart_date = self._parse_date(parsed_data.get("depart_date", ""))
        return_date_val = parsed_data.get("return_date")
        return_date = self._parse_date(return_date_val) if return_date_val else None

        # Purpose
        purpose_text = parsed_data.get("purpose_text", "unspecified").strip()
        purpose_category = self._classify_purpose(purpose_text)

        # Class
        raw_class = parsed_data.get("requested_class", "economy").lower().strip()
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
                requested_fare = float(str(requested_fare).replace(",", "").replace("INR", "").strip())
            except (ValueError, TypeError):
                requested_fare = None

        # Compute derived fields IN CODE (not from LLM)
        is_international = not (origin in config.INDIAN_IATA and destination in config.INDIAN_IATA)
        days_ahead = (depart_date - config.TODAY).days if depart_date else 0

        # Read employee profile from memory
        emp = self.memory.get_employee(employee_id)
        if emp:
            self.memory.write_employee_access(employee_id, request_id)
        else:
            logger.warning("Employee %s not found in memory", employee_id)

        parsed = ParsedRequest(
            request_id=request_id,
            employee_id=employee_id,
            origin_iata=origin,
            destination_iata=destination,
            depart_date=depart_date or config.TODAY,
            return_date=return_date,
            purpose_text=purpose_text,
            purpose_category=purpose_category,
            requested_class=requested_class,
            requested_fare=requested_fare,
            is_international=is_international,
            days_ahead=days_ahead,
        )

        # Write to shared memory (audit log)
        self.memory.write_audit_log(
            request_id, "request_parser", "parsed_request",
            parsed.model_dump_json(),
        )
        logger.info("Parsed request %s: %s->%s on %s", request_id, origin, destination, depart_date)
        return parsed

    async def _llm_parse(self, raw_text: str) -> dict:
        """Use LLM for structured extraction."""
        from llm_factory import llm_json_call

        system_msg = (
            "You are a travel request parser. Extract structured fields from the request. "
            "Return a JSON object with these fields:\n"
            "- employee_id: string (e.g., 'E101')\n"
            "- origin: string (city name or IATA code)\n"
            "- destination: string (city name or IATA code)\n"
            "- depart_date: string (YYYY-MM-DD format)\n"
            "- return_date: string or null (YYYY-MM-DD format)\n"
            "- purpose_text: string (the stated purpose)\n"
            "- requested_class: string ('economy', 'business', or 'premium_economy')\n"
            "- requested_fare: number or null (fare amount in INR if mentioned)\n"
            "Return ONLY the JSON object, no other text."
        )
        result = await llm_json_call(raw_text, system_message=system_msg, schema_hint="parse_request_v1")
        return result

    def _regex_parse(self, raw_text: str) -> dict:
        """Fallback rule-based parser without catastrophic regex backtracking."""
        text = raw_text.lower()
        result: dict = {}

        # Employee ID
        emp_match = re.search(r'\b([eE]\d{3})\b', raw_text)
        if emp_match:
            result["employee_id"] = emp_match.group(1).upper()

        # Origin and destination by finding known cities
        # Find all known cities and their start positions in raw_text
        found_cities = []
        for city_name in config.CITY_TO_IATA:
            # Match whole words only
            pattern = r'\b' + re.escape(city_name) + r'\b'
            for m in re.finditer(pattern, text):
                found_cities.append((m.start(), city_name))
        
        # Sort by position in text
        found_cities.sort(key=lambda x: x[0])
        if len(found_cities) >= 2:
            result["origin"] = found_cities[0][1]
            result["destination"] = found_cities[1][1]
        elif len(found_cities) == 1:
            result["origin"] = found_cities[0][1]
            result["destination"] = "BOM"

        # Dates: look for standard formats
        dates_found = []
        # Pattern 1: DD Mon YYYY (e.g., 2 Dec 2026, 27 Nov 2026, 15 Nov 2026)
        for m in re.finditer(r'\b(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})\b', raw_text):
            try:
                for fmt in ["%d %b %Y", "%d %B %Y"]:
                    try:
                        dates_found.append(datetime.strptime(m.group(1), fmt).date())
                        break
                    except ValueError:
                        continue
            except Exception:
                pass

        # Pattern 2: YYYY-MM-DD
        for m in re.finditer(r'\b(\d{4}-\d{2}-\d{2})\b', raw_text):
            try:
                dates_found.append(datetime.strptime(m.group(1), "%Y-%m-%d").date())
            except ValueError:
                pass

        if dates_found:
            result["depart_date"] = dates_found[0].isoformat()
            if len(dates_found) > 1:
                result["return_date"] = dates_found[1].isoformat()

        # Purpose: check known purpose keywords first
        all_purposes = sorted(list(BUSINESS_PURPOSES | PERSONAL_KEYWORDS), key=len, reverse=True)
        for kw in all_purposes:
            if kw in text:
                result["purpose_text"] = kw
                break
        
        if "purpose_text" not in result:
            m_purp = re.search(r'purpose\s*[:\s]*([^\.,\n]+)', raw_text, re.IGNORECASE)
            if m_purp:
                result["purpose_text"] = m_purp.group(1).strip()
            else:
                result["purpose_text"] = "unspecified"

        # Class
        if "business" in text:
            result["requested_class"] = "business"
        elif "premium" in text:
            result["requested_class"] = "premium_economy"
        else:
            result["requested_class"] = "economy"

        # Fare: look for INR / numbers
        fare_matches = re.findall(r'(?:inr|rs\.?|₹)?\s*(\d{1,3}(?:,\d{3})+|\d{4,6})', text)
        fares_found = []
        for fm in fare_matches:
            val_str = fm.replace(",", "").strip()
            try:
                val = float(val_str)
                # Ignore years like 2026
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
        # Already an IATA code?
        if cleaned in config.INDIAN_IATA | {"SIN", "LHR", "JFK", "DXB"}:
            return cleaned
        # Look up by city name
        lower = city_or_iata.strip().lower()
        iata = config.CITY_TO_IATA.get(lower)
        if iata:
            return iata
        # Try partial match
        for city, code in config.CITY_TO_IATA.items():
            if city in lower or lower in city:
                return code
        logger.warning("Could not resolve IATA for: %s", city_or_iata)
        return cleaned

    def _parse_date(self, date_str: str) -> Optional[date]:
        """Parse a date string in various formats."""
        if not date_str:
            return None
        for fmt in ["%Y-%m-%d", "%d %b %Y", "%d %B %Y", "%d/%m/%Y", "%m/%d/%Y"]:
            try:
                return datetime.strptime(date_str.strip(), fmt).date()
            except ValueError:
                continue
        logger.warning("Could not parse date: %s", date_str)
        return None

    def _classify_purpose(self, purpose_text: str) -> PurposeCategory:
        """Classify purpose text into a category."""
        lower = purpose_text.lower().strip()
        if not lower or lower == "unspecified":
            return PurposeCategory.UNSPECIFIED
        for kw in PERSONAL_KEYWORDS:
            if kw in lower:
                return PurposeCategory.PERSONAL
        for kw in BUSINESS_PURPOSES:
            if kw in lower:
                return PurposeCategory.BUSINESS_ELIGIBLE
        return PurposeCategory.UNSPECIFIED
