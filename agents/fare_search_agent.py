"""
Fare Search Agent — searches mock fare data via Semantic Kernel plugin function.

Exposes search_fares as a kernel function and returns structured FareOptions.
"""

from __future__ import annotations

import json
import logging
from typing import Annotated, Optional

from semantic_kernel.functions import kernel_function

from core.memory import MemoryStore
from core.schemas import FareOptions, ParsedRequest
from tools.fare_tool import search_fares

logger = logging.getLogger(__name__)


class FareSearchPlugin:
    """Semantic Kernel plugin exposing the fare search tool."""

    @kernel_function(name="search_fares", description="Search for flight fares by route and date")
    def search_fares_sk(
        self,
        origin: Annotated[str, "Origin IATA code"],
        destination: Annotated[str, "Destination IATA code"],
        date: Annotated[str, "Departure date YYYY-MM-DD"],
        cabin: Annotated[Optional[str], "Optional cabin class filter"] = None,
    ) -> str:
        """Search fares and return as JSON string."""
        fares = search_fares(origin, destination, date, cabin)
        return json.dumps([f.model_dump() for f in fares])


class FareSearchAgent:
    """Searches for available fares for a travel request.
    
    Memory interactions:
    - Reads: employee preference history (from trip_history)
    - Writes: fares found to audit log
    """

    def __init__(self, memory: MemoryStore) -> None:
        self.memory = memory
        self.plugin = FareSearchPlugin()

    async def search(self, parsed: ParsedRequest) -> FareOptions:
        """Search for fares matching the parsed request.
        
        Uses the fare_tool through the Semantic Kernel plugin.
        """
        # Read employee preference history from memory
        prior_trips = self.memory.get_trips_for_employee(parsed.employee_id)
        self.memory.write_audit_log(
            parsed.request_id, "fare_search", "read_preferences",
            json.dumps({
                "employee_id": parsed.employee_id,
                "prior_trips": len(prior_trips),
            }),
        )

        # Search fares using the tool
        fares = search_fares(
            origin=parsed.origin_iata,
            destination=parsed.destination_iata,
            date=parsed.depart_date.isoformat(),
        )

        result = FareOptions(options=fares)

        # Write fares to memory
        self.memory.write_audit_log(
            parsed.request_id, "fare_search", "fares_found",
            json.dumps({
                "route": f"{parsed.origin_iata}-{parsed.destination_iata}",
                "date": parsed.depart_date.isoformat(),
                "count": len(fares),
                "price_range": f"{min(f.price_inr for f in fares):,.0f}-{max(f.price_inr for f in fares):,.0f}" if fares else "N/A",
            }),
        )

        logger.info(
            "Found %d fares for %s %s->%s",
            len(fares), parsed.request_id, parsed.origin_iata, parsed.destination_iata,
        )
        return result
