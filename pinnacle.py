"""
Pinnacle betting API integration for Snappi.
Places straight bets (Total Goals Over/Under) when snaps are sent.
Uses HTTP Basic auth: PINNACLE_USERNAME and PINNACLE_PASSWORD (max 10 chars) in .env.
API base: https://api.pinnacle.com/
"""
import json
import logging
import os
import uuid
from pathlib import Path

import requests
from dotenv import load_dotenv

_script_dir = Path(__file__).resolve().parent
load_dotenv(_script_dir / ".env")

logger = logging.getLogger(__name__)

PINNACLE_USERNAME = (os.getenv("PINNACLE_USERNAME") or "").strip()
PINNACLE_PASSWORD = (os.getenv("PINNACLE_PASSWORD") or "").strip()
PINNACLE_SPORT_ID = 29  # Soccer
BASE_URL = "https://api.pinnacle.com"


def _auth_headers() -> dict:
    """HTTP Basic auth. Pinnacle password max 10 chars."""
    if not PINNACLE_USERNAME or not PINNACLE_PASSWORD:
        return {}
    import base64
    creds = f"{PINNACLE_USERNAME}:{PINNACLE_PASSWORD}"
    b64 = base64.b64encode(creds.encode()).decode()
    return {
        "Authorization": f"Basic {b64}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def is_configured() -> bool:
    """True if Pinnacle credentials are set."""
    return bool(PINNACLE_USERNAME and PINNACLE_PASSWORD)


def get_fixtures(sport_id: int = PINNACLE_SPORT_ID, league_ids: list[int] | None = None) -> list[dict]:
    """Fetch fixtures for sport. Returns list of leagues, each with events (id, home, away)."""
    if not is_configured():
        return []
    url = f"{BASE_URL}/v1/fixtures"
    params = {"sportId": sport_id}
    if league_ids:
        params["leagueIds"] = league_ids
    try:
        r = requests.get(url, params=params, headers=_auth_headers(), timeout=30)
        if r.status_code != 200:
            logger.warning("Pinnacle fixtures: %s %s", r.status_code, r.text[:200])
            return []
        data = r.json()
        return data.get("league", [])
    except Exception as e:
        logger.warning("Pinnacle fixtures error: %s", e)
        return []


def get_odds(sport_id: int = PINNACLE_SPORT_ID, league_ids: list[int] | None = None) -> list[dict]:
    """Fetch odds for sport. Returns leagues with events and periods (Total Points lines)."""
    if not is_configured():
        return []
    url = f"{BASE_URL}/v1/odds"
    params = {"sportId": sport_id}
    if league_ids:
        params["leagueIds"] = league_ids
    try:
        r = requests.get(url, params=params, headers=_auth_headers(), timeout=30)
        if r.status_code != 200:
            logger.warning("Pinnacle odds: %s %s", r.status_code, r.text[:200])
            return []
        return r.json().get("league", [])
    except Exception as e:
        logger.warning("Pinnacle odds error: %s", e)
        return []


def _normalize_team(s: str) -> str:
    """Lowercase, strip, collapse spaces for fuzzy match."""
    return " ".join((s or "").lower().split())


def _fuzzy_match(a: str, b: str) -> bool:
    """Simple match: normalized equality or one contains the other."""
    na, nb = _normalize_team(a), _normalize_team(b)
    if na == nb:
        return True
    if na in nb or nb in na:
        return True
    a_words = set(na.split())
    b_words = set(nb.split())
    overlap = len(a_words & b_words) / max(len(a_words), len(b_words), 1)
    return overlap >= 0.6


def find_total_goals_line(
    home: str, away: str, side: str = "UNDER", target_line: str = "2.5"
) -> dict | None:
    """
    Find Pinnacle event and Total Goals line for a match.
    side: OVER or UNDER. target_line: e.g. "2.5" for Under 2.5.
    Returns {"eventId", "lineId", "sportId", "periodNumber", "betType": "TOTAL_POINTS", "side"} or None.
    Pinnacle odds: league->events->periods->totals (points, over/under prices; lineId may be in period).
    """
    if not is_configured():
        return None
    leagues = get_odds()
    for league in leagues:
        events = league.get("event", []) or league.get("events", []) or []
        for event in events:
            e_home = (event.get("homeTeam") or event.get("home") or "").strip()
            e_away = (event.get("awayTeam") or event.get("away") or "").strip()
            if not _fuzzy_match(home, e_home) or not _fuzzy_match(away, e_away):
                continue
            event_id = event.get("id")
            if not event_id:
                continue
            periods = event.get("period", []) or event.get("periods", []) or []
            for period in periods:
                if period.get("number", 0) != 0:  # Full game
                    continue
                totals = period.get("total", []) or period.get("totals", []) or []
                for total in totals:
                    pts = str(total.get("points", ""))
                    if target_line and target_line not in pts:
                        continue
                    line_id = total.get("lineId")
                    if not line_id:
                        continue
                    return {
                        "eventId": event_id,
                        "lineId": line_id,
                        "sportId": PINNACLE_SPORT_ID,
                        "periodNumber": 0,
                        "betType": "TOTAL_POINTS",
                        "side": side.upper(),
                    }
    return None


def place_straight_bet(
    sport_id: int,
    event_id: int,
    line_id: int,
    stake: float,
    bet_type: str = "TOTAL_POINTS",
    side: str = "UNDER",
    period_number: int = 0,
    unique_request_id: str | None = None,
) -> dict | None:
    """
    Place a straight bet on Pinnacle.
    Returns {"status": "ACCEPTED"|"PENDING_ACCEPTANCE"|"PROCESSED_WITH_ERROR", "betId", "errorCode", ...} or None.
    """
    if not is_configured():
        logger.warning("Pinnacle: credentials not configured")
        return None
    uid = unique_request_id or str(uuid.uuid4())
    payload = {
        "uniqueRequestId": uid,
        "acceptBetterLine": True,
        "stake": round(stake, 2),
        "winRiskStake": "RISK",
        "lineId": line_id,
        "sportId": sport_id,
        "eventId": event_id,
        "periodNumber": period_number,
        "betType": bet_type,
        "side": side,
    }
    try:
        r = requests.post(
            f"{BASE_URL}/v4/bets/straight",
            json=payload,
            headers=_auth_headers(),
            timeout=15,
        )
        data = r.json() if r.text else {}
        if r.status_code != 200:
            logger.warning("Pinnacle place bet: %s %s", r.status_code, data)
            return {"status": "PROCESSED_WITH_ERROR", "errorCode": data.get("code", "HTTP_ERROR")}
        return data
    except Exception as e:
        logger.warning("Pinnacle place bet error: %s", e)
        return None


def _extract_line_number(target_line: str) -> str:
    """Extract numeric line from 'Under 2.5' or 'Under 1.5' etc. Default 2.5."""
    import re
    if not target_line:
        return "2.5"
    m = re.search(r"(\d+\.?\d*)", str(target_line))
    return m.group(1) if m else "2.5"


def place_bet_for_snap_entry(
    entry: dict, stake_dollars: float, target_line: str | None = None
) -> dict | None:
    """
    Place Under bet for a single snap entry. Matches by home/away, finds Total Goals line.
    target_line: e.g. "2.5" or "Under 2.5" — extracted from entry if not provided.
    Returns Pinnacle response or None if no match / not configured.
    """
    home = entry.get("home") or ""
    away = entry.get("away") or ""
    if not home or not away:
        name = entry.get("name") or ""
        if isinstance(name, str) and " vs " in name:
            parts = name.split(" vs ", 1)
            home, away = parts[0].strip(), parts[1].strip()
    if not home or not away:
        return None
    line_num = target_line or _extract_line_number(entry.get("target_line", ""))
    line_info = find_total_goals_line(home, away, side="UNDER", target_line=line_num)
    if not line_info:
        logger.info("Pinnacle: no matching line for %s vs %s", home, away)
        return None
    return place_straight_bet(
        sport_id=line_info["sportId"],
        event_id=line_info["eventId"],
        line_id=line_info["lineId"],
        stake=stake_dollars,
        bet_type=line_info["betType"],
        side=line_info["side"],
        period_number=line_info["periodNumber"],
    )


def place_parlay_bet(entries: list[dict], risk_amount: float) -> tuple[str, dict | None]:
    """
    Place a parlay (Under Total Goals) for multiple entries.
    Returns (description, response_dict). response has status ACCEPTED or PROCESSED_WITH_ERROR.
    """
    if not is_configured() or not entries:
        return ("parlay (no entries)", None)
    legs = []
    for e in entries:
        home = e.get("home") or ""
        away = e.get("away") or ""
        if not home or not away and isinstance(e.get("name"), str):
            parts = (e.get("name") or "").split(" vs ", 1)
            if len(parts) >= 2:
                home, away = parts[0].strip(), parts[1].strip()
        if not home or not away:
            continue
        line_num = _extract_line_number(e.get("target_line", ""))
        line_info = find_total_goals_line(home, away, side="UNDER", target_line=line_num)
        if not line_info:
            logger.info("Pinnacle parlay: skip %s vs %s (no line)", home, away)
            continue
        legs.append({
            "uniqueLegId": str(uuid.uuid4()).upper(),
            "lineId": line_info["lineId"],
            "sportId": line_info["sportId"],
            "eventId": line_info["eventId"],
            "periodNumber": line_info["periodNumber"],
            "legBetType": "TOTAL_POINTS",
            "side": "UNDER",
        })
    if len(legs) < 2:
        return ("parlay (<2 valid legs)", None)
    payload = {
        "uniqueRequestId": str(uuid.uuid4()),
        "acceptBetterLine": True,
        "riskAmount": round(risk_amount, 2),
        "legs": legs,
        "roundRobinOptions": ["Parlay"],
    }
    try:
        r = requests.post(
            f"{BASE_URL}/v4/bets/parlay",
            json=payload,
            headers=_auth_headers(),
            timeout=20,
        )
        data = r.json() if r.text else {}
        if r.status_code != 200:
            logger.warning("Pinnacle parlay: %s %s", r.status_code, data)
            return (f"parlay {len(legs)} legs", {"status": "PROCESSED_WITH_ERROR", "errorCode": data.get("code", "HTTP_ERROR")})
        return (f"parlay {len(legs)} legs", data)
    except Exception as e:
        logger.warning("Pinnacle parlay error: %s", e)
        return (f"parlay {len(legs)} legs", None)


def place_bets_by_color_groups(
    color_groups: dict[str, list[dict]],
    unit_dollars: float,
    unit_map: dict[str, float],
) -> list[tuple[str, dict | None]]:
    """
    Place bets by Hamilton's rules: group same color; parlay if 2+, else single.
    color_groups: e.g. {"GREEN": [e1,e2,e3], "YELLOW": [e4]}
    unit_map: e.g. {"RED": 0.5, "YELLOW": 2, "GREEN": 3}
    Returns list of (description, response) for each bet placed.
    """
    out = []
    for color, group_entries in color_groups.items():
        if not group_entries:
            continue
        units = unit_map.get(color, 2)
        stake = unit_dollars * units
        if len(group_entries) >= 2:
            desc, res = place_parlay_bet(group_entries, stake)
            out.append((f"{color} {desc} (${stake:.2f})", res))
        else:
            res = place_bet_for_snap_entry(group_entries[0], stake)
            out.append((f"{color} single (${stake:.2f})", res))
    return out
