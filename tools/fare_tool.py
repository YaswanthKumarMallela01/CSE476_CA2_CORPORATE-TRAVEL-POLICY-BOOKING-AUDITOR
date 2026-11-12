"""
Mock fare search tool exposed as a Semantic Kernel plugin function.
Reads fare data from data/mock/fares.json.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from core.schemas import CabinClass, FareOption

logger = logging.getLogger(__name__)

_fare_data: Optional[dict] = None


def _load_fares(fares_path: Path) -> dict:
    """Load and cache fare data from JSON."""
    global _fare_data
    if _fare_data is None:
        with open(fares_path) as f:
            _fare_data = json.load(f)
        logger.info("Loaded fares for %d routes", len(_fare_data))
    return _fare_data


def reset_fare_cache() -> None:
    """Reset the fare data cache (for testing)."""
    global _fare_data
    _fare_data = None


def search_fares(
    origin: str,
    destination: str,
    date: str,
    cabin: Optional[str] = None,
    fares_path: Optional[Path] = None,
) -> list[FareOption]:
    """Search for fares matching the route and date.
    
    Args:
        origin: Origin IATA code (e.g., 'DEL').
        destination: Destination IATA code (e.g., 'BOM').
        date: Departure date as YYYY-MM-DD string.
        cabin: Optional cabin class filter ('economy', 'business').
        fares_path: Path to fares JSON. Uses config default if None.
    
    Returns:
        List of FareOption sorted by price ascending.
    """
    if fares_path is None:
        from config import FARES_PATH
        fares_path = FARES_PATH

    data = _load_fares(fares_path)
    route_key = f"{origin}-{destination}"
    route_fares = data.get(route_key, {})
    date_fares = route_fares.get(date, [])

    if not date_fares:
        logger.warning("No fares found for %s on %s", route_key, date)
        return []

    options = []
    for f in date_fares:
        fare = FareOption(
            fare_id=f["fare_id"],
            airline=f["airline"],
            price_inr=f["price_inr"],
            cabin=CabinClass(f["cabin"]),
            duration_hours=f["duration_hours"],
            contracted=f["contracted"],
            refundable=f["refundable"],
        )
        if cabin and fare.cabin.value != cabin:
            continue
        options.append(fare)

    options.sort(key=lambda x: x.price_inr)
    logger.info("Found %d fares for %s on %s", len(options), route_key, date)
    return options
