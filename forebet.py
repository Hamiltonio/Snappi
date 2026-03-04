"""
Forebet predictions via Apify. Fetches Under/Over, probabilities, and match data.
Cache results to avoid repeated Apify calls.
"""
import json
import logging
import os
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

_script_dir = Path(__file__).resolve().parent
load_dotenv(_script_dir / ".env")

logger = logging.getLogger(__name__)

APIFY_TOKEN = os.getenv("APIFY_TOKEN", "").strip()
# Actor: locos08/forebet-predictions-scraper (pay-per-event). Override with APIFY_FOREBET_ACTOR in .env
APIFY_FOREBET_ACTOR = os.getenv("APIFY_FOREBET_ACTOR", "locos08/forebet-predictions-scraper").strip()
FOREBET_CACHE_PATH = _script_dir / "forebet_cache.json"
FOREBET_CACHE_TTL_HOURS = 6


def _load_cache() -> tuple[list[dict], datetime | None]:
    """Load cache. Returns (items, cached_at) or ([], None)."""
    if not FOREBET_CACHE_PATH.exists():
        return [], None
    try:
        with open(FOREBET_CACHE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        items = data.get("items", [])
        ts = data.get("cached_at")
        cached_at = datetime.fromisoformat(ts) if ts else None
        return items, cached_at
    except (json.JSONDecodeError, OSError, ValueError):
        return [], None


def _save_cache(items: list[dict]) -> None:
    """Persist cache."""
    try:
        with open(FOREBET_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump({"items": items, "cached_at": datetime.now().isoformat()}, f, indent=2)
    except OSError:
        pass


def _cache_stale(cached_at: datetime | None) -> bool:
    """True if cache is older than TTL."""
    if cached_at is None:
        return True
    from datetime import timedelta
    return (datetime.now() - cached_at).total_seconds() > FOREBET_CACHE_TTL_HOURS * 3600


def fetch_forebet_predictions(force_refresh: bool = False) -> list[dict]:
    """
    Fetch Forebet predictions from Apify. Uses cache unless force_refresh or cache stale.
    Returns list of match dicts with home, away, underOverPrediction, probability_under_percent, etc.
    """
    if not APIFY_TOKEN:
        logger.warning("APIFY_TOKEN not set; Forebet disabled")
        return []

    cached, cached_at = _load_cache()
    if not force_refresh and cached and not _cache_stale(cached_at):
        return cached

    try:
        from apify_client import ApifyClient
        client = ApifyClient(APIFY_TOKEN)
        run = client.actor(APIFY_FOREBET_ACTOR).call()
        if run is None:
            logger.error("Forebet Apify run failed")
            return cached if cached else []
        dataset_id = run.get("defaultDatasetId")
        if not dataset_id:
            logger.error("Forebet Apify: no dataset ID")
            return cached if cached else []
        items = list(client.dataset(dataset_id).iterate_items())
        _save_cache(items)
        logger.info("Forebet: fetched %d matches from Apify", len(items))
        return items
    except Exception as e:
        logger.warning("Forebet Apify error: %s", e)
        return cached if cached else []


def _normalize_team(s: str) -> str:
    """Lowercase, strip, collapse spaces."""
    return " ".join((s or "").lower().split())


def _fuzzy_match(a: str, b: str) -> bool:
    """Simple match: normalized equality or one contains the other."""
    na, nb = _normalize_team(a), _normalize_team(b)
    if na == nb:
        return True
    if na in nb or nb in na:
        return True
    # Handle "Man City" vs "Manchester City" etc.
    a_words = set(na.split())
    b_words = set(nb.split())
    overlap = len(a_words & b_words) / max(len(a_words), len(b_words), 1)
    return overlap >= 0.6


def get_forebet_for_match(
    home: str,
    away: str,
    match_date: str | None = None,
    league: str | None = None,
    predictions: list[dict] | None = None,
) -> dict | None:
    """
    Find Forebet prediction for a match. Fuzzy match on home/away, optionally narrowed by date/league.
    match_date: YYYY-MM-DD (optional, helps narrow).
    league: optional league name (e.g. "Serie A (Italy)") to help disambiguate popular teams.
    predictions: pre-fetched list; if None, uses cache or fetches.
    Returns dict with underOverPrediction, probability_under_percent, etc. or None.
    """
    items = predictions if predictions is not None else fetch_forebet_predictions()
    if not items:
        return None

    league_norm = _normalize_team(league or "") if league else ""
    best_match: dict | None = None

    for m in items:
        fh = (m.get("home") or "").strip()
        fa = (m.get("away") or "").strip()
        if not (_fuzzy_match(home, fh) and _fuzzy_match(away, fa)):
            continue
        if match_date:
            md = (m.get("matchDate") or "").strip()
            if md and md != match_date:
                continue
        if league_norm:
            fl = (m.get("leagueName") or "").strip()
            if fl:
                if _fuzzy_match(league_norm, fl):
                    # Strong match: correct teams, date, and league
                    return m
                # Keep as a weaker candidate if we don't find anything better
                if best_match is None:
                    best_match = m
                continue
        # If no league filter or league is unknown, accept the first good team+date match
        return m

    return best_match

