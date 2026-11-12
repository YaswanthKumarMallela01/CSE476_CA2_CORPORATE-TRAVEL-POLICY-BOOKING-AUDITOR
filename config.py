"""
Configuration loader for the Travel Auditor system.
Reads .env and provides validated settings with sensible defaults.
"""

import logging
import os
import sys
from datetime import date, datetime
from pathlib import Path

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Load .env from project root
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(_PROJECT_ROOT / ".env")


def _get(key: str, default: str | None = None) -> str | None:
    """Read an env var; treat empty string as unset."""
    val = os.getenv(key, "")
    return val if val else default


# ---------------------------------------------------------------------------
# REQUIRED variables — fail fast if missing
# ---------------------------------------------------------------------------
GOOGLE_API_KEY: str = _get("GOOGLE_API_KEY", "") or ""
# We validate at import time only if an LLM call is about to be made (see llm_factory).

def require_api_key() -> str:
    """Return the Gemini API key or exit with a clear message."""
    if not GOOGLE_API_KEY:
        print(
            "\n❌  GOOGLE_API_KEY is not set.\n"
            "    1. Open .env in the project root.\n"
            "    2. Set GOOGLE_API_KEY=<your key>\n"
            "    3. Get a free key at https://aistudio.google.com/apikey\n",
            file=sys.stderr,
        )
        sys.exit(1)
    return GOOGLE_API_KEY


# ---------------------------------------------------------------------------
# OPTIONAL variables with defaults
# ---------------------------------------------------------------------------
GEMINI_MODEL_NAME: str = _get("GEMINI_MODEL_NAME", "gemini-2.0-flash")  # type: ignore[assignment]

LOG_LEVEL: str = _get("LOG_LEVEL", "INFO")  # type: ignore[assignment]

# "Today" override for reproducible date logic
_today_str = _get("TODAY_DATE", "2026-11-12")
try:
    TODAY: date = datetime.strptime(_today_str, "%Y-%m-%d").date()  # type: ignore[arg-type]
except (ValueError, TypeError):
    TODAY = date(2026, 11, 12)
    logging.warning("Invalid TODAY_DATE '%s', falling back to 2026-11-12", _today_str)

RATE_LIMIT_RPM: int = int(_get("RATE_LIMIT_RPM", "10"))  # type: ignore[arg-type]
RATE_LIMIT_MAX_RETRIES: int = int(_get("RATE_LIMIT_MAX_RETRIES", "5"))  # type: ignore[arg-type]
RAG_TOP_K: int = int(_get("RAG_TOP_K", "5"))  # type: ignore[arg-type]

CHROMA_PERSIST_DIR: str = str(_PROJECT_ROOT / _get("CHROMA_PERSIST_DIR", "chroma_db"))  # type: ignore[arg-type]
LLM_CACHE_DIR: str = str(_PROJECT_ROOT / _get("LLM_CACHE_DIR", "llm_cache"))  # type: ignore[arg-type]
SQLITE_DB_PATH: str = str(_PROJECT_ROOT / _get("SQLITE_DB_PATH", "travel_auditor.db"))  # type: ignore[arg-type]
EMBEDDING_MODEL_NAME: str = _get("EMBEDDING_MODEL_NAME", "all-MiniLM-L6-v2")  # type: ignore[assignment]

# Data paths
DATA_DIR: Path = _PROJECT_ROOT / "data"
POLICY_PATH: Path = DATA_DIR / "policy" / "travel_policy.md"
FARES_PATH: Path = DATA_DIR / "mock" / "fares.json"
EMPLOYEES_PATH: Path = DATA_DIR / "mock" / "employees.json"
BUDGETS_PATH: Path = DATA_DIR / "mock" / "budgets.json"
TRIP_HISTORY_SEED_PATH: Path = DATA_DIR / "mock" / "trip_history_seed.json"
EVAL_CASES_PATH: Path = DATA_DIR / "eval" / "eval_cases.json"

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s | %(name)-28s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)

# Warn if model name was defaulted
if not os.getenv("GEMINI_MODEL_NAME"):
    logging.getLogger("config").info(
        "GEMINI_MODEL_NAME not set; defaulting to '%s'", GEMINI_MODEL_NAME
    )

# Indian IATA codes for is_international detection
INDIAN_IATA = {"DEL", "BOM", "BLR", "HYD", "CCU", "GOI", "MAA", "JAI", "PNQ", "AMD"}

# City name to IATA mapping
CITY_TO_IATA: dict[str, str] = {
    "delhi": "DEL", "new delhi": "DEL",
    "mumbai": "BOM", "bombay": "BOM",
    "bangalore": "BLR", "bengaluru": "BLR",
    "hyderabad": "HYD",
    "kolkata": "CCU", "calcutta": "CCU",
    "goa": "GOI",
    "singapore": "SIN",
    "london": "LHR",
    "new york": "JFK", "new york jfk": "JFK", "jfk": "JFK",
    "chennai": "MAA", "madras": "MAA",
    "dubai": "DXB",
}
