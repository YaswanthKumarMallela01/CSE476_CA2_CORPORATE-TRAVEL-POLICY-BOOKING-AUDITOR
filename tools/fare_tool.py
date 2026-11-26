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
    
    Priority:
    1. Exact route+date match in data/mock/fares.json
    2. Same route (any date template) in fares.json
    3. Reverse route (DEST-ORIGIN) template in fares.json
    4. Synthetic mock schedule for valid Indian/International airport pairs
    """
    if fares_path is None:
        from config import FARES_PATH
        fares_path = FARES_PATH

    if not origin or not destination or origin == destination:
        return []

    data = _load_fares(fares_path)
    route_key = f"{origin}-{destination}"
    reverse_key = f"{destination}-{origin}"

    route_fares = data.get(route_key, {})
    date_fares = route_fares.get(date, [])

    # Fallback 1: route exists on another date -> reuse template schedule
    if not date_fares and route_fares:
        first_date = sorted(route_fares.keys())[0]
        date_fares = route_fares[first_date]
        logger.info("Using route schedule template from %s on %s for requested date %s", route_key, first_date, date)

    # Fallback 2: reverse route exists
    if not date_fares and reverse_key in data and data[reverse_key]:
        rev_dates = data[reverse_key]
        first_date = sorted(rev_dates.keys())[0]
        date_fares = rev_dates[first_date]
        logger.info("Using reverse route template %s on %s for %s", reverse_key, first_date, route_key)

    # Fallback 3: synthetic mock carrier fares for valid known IATA routes
    if not date_fares:
        date_fares = _generate_fallback_fares(origin, destination)

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


def _generate_fallback_fares(origin: str, destination: str) -> list[dict]:
    """Generate deterministic mock airline inventory for additional valid routes."""
    import config
    known = config.INDIAN_IATA | config.INTERNATIONAL_IATA
    if origin not in known or destination not in known:
        return []

    is_intl = not (origin in config.INDIAN_IATA and destination in config.INDIAN_IATA)
    tag = f"{origin}{destination}"
    if is_intl:
        return [
            {"fare_id": f"F-{tag}-1", "airline": "AirIndia", "price_inr": 68000, "cabin": "economy", "duration_hours": 7.5, "contracted": True, "refundable": False},
            {"fare_id": f"F-{tag}-2", "airline": "IndiGo", "price_inr": 72000, "cabin": "economy", "duration_hours": 7.8, "contracted": True, "refundable": True},
            {"fare_id": f"F-{tag}-3", "airline": "Vistara-AI", "price_inr": 76500, "cabin": "economy", "duration_hours": 7.4, "contracted": True, "refundable": True},
            {"fare_id": f"F-{tag}-4", "airline": "SkyJet", "price_inr": 65000, "cabin": "economy", "duration_hours": 8.2, "contracted": False, "refundable": False},
            {"fare_id": f"F-{tag}-5", "airline": "AirIndia", "price_inr": 108000, "cabin": "business", "duration_hours": 7.5, "contracted": True, "refundable": True},
        ]
    return [
        {"fare_id": f"F-{tag}-1", "airline": "IndiGo", "price_inr": 8400, "cabin": "economy", "duration_hours": 2.2, "contracted": True, "refundable": False},
        {"fare_id": f"F-{tag}-2", "airline": "AirIndia", "price_inr": 9100, "cabin": "economy", "duration_hours": 2.1, "contracted": True, "refundable": True},
        {"fare_id": f"F-{tag}-3", "airline": "Vistara-AI", "price_inr": 10400, "cabin": "economy", "duration_hours": 2.1, "contracted": True, "refundable": True},
        {"fare_id": f"F-{tag}-4", "airline": "SkyJet", "price_inr": 7900, "cabin": "economy", "duration_hours": 2.4, "contracted": False, "refundable": False},
        {"fare_id": f"F-{tag}-5", "airline": "AirIndia", "price_inr": 25000, "cabin": "business", "duration_hours": 2.1, "contracted": True, "refundable": True},
    ]
