"""
Snappi V3 – The Midnight Hunter & Self-Optimization Engine.

Dual-phase (Thorold, Ontario time):
  Phase 1 The Hunter (05:00–00:00 / Midnight): Live polling every 60s, api-sports.io, 30'/70' windows. Quota 7500; reset daily_calls and caches at 05:00.
  Phase 2 The Analyst (00:01–04:59): At 00:05 nightly_analysis(); self-optimization (Gemini + optimization_log + Confirm); at 00:30 send_daily_summary().

Every alert is logged to bet_history.json. Split sheets: halftime (28-min window) and fulltime (73-min window). Net profit = balance at day start vs balance at day end.
"""

import csv
import json
import logging
import re
import shutil
from itertools import groupby
from logging.handlers import RotatingFileHandler
import os
import threading
import time
import traceback
import requests
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
from urllib.parse import quote
from dotenv import load_dotenv, find_dotenv
import telebot

import forebet
import notifier
import pinnacle
import sheets_logger

# Script directory and paths (must be defined before load_dotenv / LOG_FILE)
_script_dir = os.path.dirname(os.path.abspath(__file__))

# Load .env from the same folder as main.py first, then find_dotenv() as fallback
load_dotenv(os.path.join(_script_dir, ".env"))
if not os.getenv("API_FOOTBALL_KEY"):
    _env_path = find_dotenv()
    if _env_path:
        load_dotenv(_env_path)
    else:
        load_dotenv()

REJECTIONS_CSV = os.path.join(_script_dir, "rejections.csv")
BET_HISTORY_JSON = os.path.join(_script_dir, "bet_history.json")
ARCHIVES_DIR = os.path.join(_script_dir, "archives")
API_CALLS_STATE_JSON = os.path.join(_script_dir, "api_calls_state.json")
LOG_FILE = os.path.join(_script_dir, "snappi.log")
LOG_MAX_BYTES = 5 * 1024 * 1024  # 5 MB before rotation (capped per upgrade)
LOG_BACKUP_COUNT = 1  # keep snappi.log.1
SENTINEL_SYSTEM_RESET = os.path.join(_script_dir, ".snappi_system_reset_done")

# Configure file logging with rotation so snappi.log doesn't grow forever
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        RotatingFileHandler(LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT, encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

# Thorold, Ontario (Eastern Time)
THOROLD_TZ = ZoneInfo("America/Toronto")
# Phase 1 The Hunter: 05:00 AM to 12:00 AM (Midnight)
HUNTER_START_HOUR = 5
HUNTER_END_HOUR = 24  # 05:00–23:59 = Hunter; 00:00 = Analyst
# Phase 2 Analyst: nightly at 00:05, daily summary at 00:30
ANALYST_NIGHTLY_HOUR = 0
ANALYST_NIGHTLY_MINUTE = 5
ANALYST_SUMMARY_HOUR = 0
ANALYST_SUMMARY_MINUTE = 30
# Loss = we predicted under/low pressure but final total goals >= this (full-time default)
UNDER_GOALS_LOSS_THRESHOLD = 3

# Dynamic unit sizing (Snappi's "engine" size)
UNIT_DENOM_DEFAULT = 4.0  # normal days
UNIT_DENOM_HEAVY = 6.0    # weekends or heavy-traffic (many live fixtures)
HEAVY_FIXTURE_THRESHOLD = 15


def _loss_threshold_from_target_line(target_line: str) -> int:
    """Parse Under X.5 from target line; return goals >= this means LOSS. Default UNDER_GOALS_LOSS_THRESHOLD."""
    if not (target_line and target_line.strip()):
        return UNDER_GOALS_LOSS_THRESHOLD
    m = re.search(r"under\s*(\d+(?:\.5)?)", target_line.strip(), re.I)
    if not m:
        return UNDER_GOALS_LOSS_THRESHOLD
    try:
        val = float(m.group(1))
        # Under 1.5 -> loss if total >= 2; Under 2.5 -> loss if total >= 3
        return int(val) + 1
    except ValueError:
        return UNDER_GOALS_LOSS_THRESHOLD

# --- Configuration (from .env); keys stored securely in .env ---
API_FOOTBALL_KEY = os.getenv("API_FOOTBALL_KEY")
FIXTURES_URL = "https://v3.football.api-sports.io/fixtures"
STATISTICS_URL = "https://v3.football.api-sports.io/fixtures/statistics"
EVENTS_URL = "https://v3.football.api-sports.io/fixtures/events"
ODDS_URL = "https://v3.football.api-sports.io/odds"

# Track last live fixture count so we can detect heavy-traffic sessions for unit sizing
_last_live_fixtures_count: int = 0


def _current_unit_denominator() -> float:
    """Return unit denominator based on day + traffic.

    Normal: balance / 4. Heavy (weekend or many live fixtures): balance / 6.
    """
    now = get_thorold_now()
    is_weekend = now.weekday() >= 5  # 5=Saturday, 6=Sunday
    heavy_traffic = _last_live_fixtures_count > HEAVY_FIXTURE_THRESHOLD
    return UNIT_DENOM_HEAVY if (is_weekend or heavy_traffic) else UNIT_DENOM_DEFAULT


def current_unit_dollars() -> float:
    """How many dollars is 1 unit right now, given the engine size."""
    if balance_dollars <= 0:
        return 0.0
    return balance_dollars / _current_unit_denominator()

# Base in-play link; we append a search query (home team name) so you land near the match.
# Bet365 does not expose a public "add to betslip" deep link.
BET365_BASE = "https://www.bet365.ca/#/AS/B1/"

# Request timeouts (slightly generous for Pi/slow networks; we retry on timeout)
REQUEST_TIMEOUT = 20
STATS_TIMEOUT = 15
# Poll interval (seconds) during Hunter. 120 = fewer API calls, stays under 7500/day more easily.
POLL_INTERVAL_SECONDS = 120

# --- Queue system ---
# Support 3-minute force-send: we can send with 1 match when timer or minute threshold hits
MIN_PARLAY_SIZE = 1
# If we collect this many matches without total odds >= TARGET_ODDS, send alert anyway
MAX_QUEUE_SIZE = 5
# Target combined odds (product of individual odds); alert when total >= this
TARGET_ODDS = 2.00
# Default odds per selection when API-Football doesn't provide odds (parlay math)
DEFAULT_ODDS = 1.25
# Odds cache TTL (seconds) — avoid repeated API calls for same fixture
ODDS_CACHE_TTL = 300  # 5 minutes
# League blocklist: matches from these leagues are never added to the queue (e.g. after weekly report says avoid).
# Comma-separated in .env as EXCLUDED_LEAGUES=Serie A,Liga MX or set in code.
_excluded_leagues_raw = os.getenv("EXCLUDED_LEAGUES", "").strip()
EXCLUDED_LEAGUES = [x.strip() for x in _excluded_leagues_raw.split(",") if x.strip()]
# V3.5: Queue 25-28, fire at 28; queue 70-73, fire at 73 (LOGIC.md)
WINDOW_1_MIN_START = 25
WINDOW_1_MIN_END = 28
WINDOW_2_MIN_START = 70
WINDOW_2_MIN_END = 73
# 30' window: low pressure = Total Shots only (DA removed)
MAX_SHOTS_30 = 5
# 70' window guards: Shots>25 or Corners>10 → RED (reject). Fouls>15 → narrative. Red card = veto.
SHOTS_70_RED = 25
CORNERS_70_RED = 10
FOULS_70_HIGH = 15

# Global dictionaries: persist flagged matches across the 120-second loop restarts.
# Key = fixture_id, Value = match entry (name, score, fixture_id, total_shots, total_corners, fouls, target_line, ...).
flagged_30: dict[int, dict] = {}
flagged_70: dict[int, dict] = {}

# Timers for the 3-minute value window: when did we add the first match to each queue?
# None when queue is empty; set to time.time() when first match is added; cleared when we send.
queue_30_started_at: float | None = None
queue_70_started_at: float | None = None

# Session counter for rejection logging (used in Session Summary every 10 min)
rejections_count: int = 0
# Last 20 rejection rows (from CSV) so the bot remembers after reboot; /rejections shows last 5
recent_rejections: list[list[str]] = []
# Alerts sent today (for /status remote command)
alerts_sent_today: int = 0
# Deduplication: one alert per (fixture_id, window) per usage day; cleared at 5 AM reset
sent_alerts: set[tuple[int, str]] = set()
# Deduplication: one rejection log per (fixture_id, window_minute) per usage day; cleared at 5 AM reset
logged_rejections: set[tuple[int, int]] = set()
# Remote pause: when True, Hunter skips fetch_live_fixtures() to save API credits
is_paused: bool = False
# Auto-resume: allow one auto-resume at start of 05:00 Hunter; reset when in Analyst
allow_auto_resume_next_hunter: bool = True
# API call counter (Thorold date); persisted in api_calls_state.json so it survives restarts
api_calls_today: int = 0
api_calls_date: date | None = None
API_DAILY_LIMIT = 7500  # Pro tier
# When we last sent a "quota exceeded" Telegram alert (one per day to avoid spam)
_last_429_alert_date: date | None = None
# Set when API returns 429 so /status can show "quota exceeded" until next restart
api_429_seen: bool = False
# From API response header x-ratelimit-requests-remaining (updated on each request)
api_ratelimit_remaining: int | None = None
# Last API response when fixtures returned 0 (for /livecheck diagnostics)
last_fixtures_zero_reason: str = ""
# So we log usage to Usage Stats sheet only once at 20:00
_last_usage_logged_date: date | None = None
# Last usage date when we cleared sent_alerts / logged_rejections (5 AM reset)
_last_cleared_usage_date: date | None = None
# V3.5: Balance (4 units). RED=0.5u, YELLOW=2u, GREEN=3u. Same color grouped (parlay); others singles.
BALANCE_JSON = os.path.join(_script_dir, "balance.json")
balance_dollars: float = 0.0
# Total profit (lifetime); persisted to file; updated after nightly_analysis
total_profit: float = 0.0
# Last heartbeat time (Thorold) for /status
last_heartbeat_time: datetime | None = None
# Daily profit from last nightly run (for 00:30 summary)
_last_nightly_profit: float = 0.0
_last_nightly_date: date | None = None
_last_daily_summary_date: date | None = None  # so we send 00:30 summary only once per day
_last_analyst_alert_date: date | None = None  # so we send "entering Analyst mode" once per transition
_last_weekly_report_date: date | None = None  # Sunday 8 AM weekly report sent for this date
WEEKLY_REPORT_HOUR = 8  # Thorold: send weekly Gemini breakdown Sunday 08:00
OPTIMIZATION_LOG = os.path.join(_script_dir, "optimization_log.txt")
TOTAL_PROFIT_JSON = os.path.join(_script_dir, "total_profit.json")
SOUL_MD = os.path.join(_script_dir, "soul.md")
MEMORY_JSON = os.path.join(_script_dir, "memory.json")
CHAT_SESSION_JSON = os.path.join(_script_dir, "chat_session.json")
DAY_START_BALANCE_JSON = os.path.join(_script_dir, "day_start_balance.json")
PENDING_SNAPS_JSON = os.path.join(_script_dir, "pending_snaps.json")
SNAP_COUNTER_JSON = os.path.join(_script_dir, "snap_counter.json")
TODAY_FIXTURES_CACHE_JSON = os.path.join(_script_dir, "today_fixtures_cache.json")
SNAP_RECIPIENTS_JSON = os.path.join(_script_dir, "snap_recipients.json")

# Pending snaps (legacy /accept flow). Kept for compatibility but no longer used to auto-deduct balance.
pending_snaps: dict[int, dict] = {}


def get_snap_recipient_ids() -> list[int]:
    """Primary (TELEGRAM_CHAT_ID) plus extra IDs from snap_recipients.json.
    Used ONLY for snap alerts and Sentry replies. All other messages (boot, daily summary, etc.) go to primary only."""
    primary = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    ids_ = []
    if primary:
        try:
            ids_.append(int(primary))
        except ValueError:
            pass
    extra: list[int] = []
    try:
        if os.path.isfile(SNAP_RECIPIENTS_JSON):
            with open(SNAP_RECIPIENTS_JSON, "r", encoding="utf-8") as f:
                data = json.load(f)
            for x in data.get("extra_chat_ids") or []:
                try:
                    i = int(x)
                    extra.append(i)
                except (ValueError, TypeError):
                    pass
        else:
            extra = [620814163]
            _save_snap_recipient_extras(extra)
    except (OSError, json.JSONDecodeError):
        extra = [620814163]
        _save_snap_recipient_extras(extra)
    for i in extra:
        if i not in ids_:
            ids_.append(i)
    return ids_


def _save_snap_recipient_extras(extra_ids: list[int]) -> None:
    """Persist extra snap recipient chat IDs (excluding primary TELEGRAM_CHAT_ID)."""
    try:
        with open(SNAP_RECIPIENTS_JSON, "w", encoding="utf-8") as f:
            json.dump({"extra_chat_ids": extra_ids}, f, indent=2)
    except OSError:
        pass

# In-memory cache of soul.md and memory.json (loaded at startup, memory updated via chat)
_soul_text: str = ""
_memory_data: dict = {}
_balance_at_day_start: float = 0.0


def _load_soul() -> str:
    """Load soul.md into memory. Returns the text or a fallback."""
    global _soul_text
    try:
        with open(SOUL_MD, "r", encoding="utf-8") as f:
            _soul_text = f.read().strip()
    except OSError:
        _soul_text = "You are Snappi, a witty and loyal betting assistant."
    return _soul_text


def _load_memory() -> dict:
    """Load memory.json into _memory_data. Returns the dict."""
    global _memory_data
    default_base = {"user": {}, "preferences": {}, "notes": [], "lessons": []}
    default_personality = {"observations": [], "voice_notes": [], "hamilton_preferences": {}}
    if not os.path.isfile(MEMORY_JSON):
        _memory_data = {**default_base, "personality": default_personality}
        _save_memory()
        return _memory_data
    try:
        with open(MEMORY_JSON, "r", encoding="utf-8") as f:
            _memory_data = json.load(f)
    except (OSError, json.JSONDecodeError):
        _memory_data = {**default_base, "personality": default_personality}
    if "personality" not in _memory_data:
        _memory_data["personality"] = default_personality
        _save_memory()
    return _memory_data


def _save_memory() -> None:
    """Persist _memory_data to memory.json."""
    try:
        with open(MEMORY_JSON, "w", encoding="utf-8") as f:
            json.dump(_memory_data, f, indent=2, ensure_ascii=False)
    except OSError:
        pass


# Max chat turns to send to Gemini (avoids context overflow; order preserved via timestamps).
CHAT_HISTORY_MAX_TURNS = int(os.getenv("CHAT_HISTORY_MAX_TURNS", "50"))


def _load_chat_session() -> list[dict]:
    """Load conversation history from chat_session.json. Returns list of {"role": ..., "text": ..., "ts": ...}."""
    if not os.path.isfile(CHAT_SESSION_JSON):
        return []
    try:
        with open(CHAT_SESSION_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        return []
    except (OSError, json.JSONDecodeError):
        return []


def _save_chat_session(history: list[dict]) -> None:
    """Persist conversation history. Cleared at 5 AM daily reset."""
    try:
        with open(CHAT_SESSION_JSON, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2, ensure_ascii=False)
    except OSError:
        pass


def _clear_chat_session() -> None:
    """Clear conversation history (called at 5 AM reset)."""
    try:
        with open(CHAT_SESSION_JSON, "w", encoding="utf-8") as f:
            json.dump([], f)
    except OSError:
        pass


def _append_snap_to_chat_log(entries: list[dict], window_name: str, snap_id: int | None = None) -> None:
    """Append a snap to the daily chat log so Hamilton can refer to it in conversation."""
    if not entries:
        return
    header = f"📸 Snap #{snap_id} ({window_name}):" if snap_id else f"📸 Snap sent ({window_name}):"
    lines = [header]
    for e in entries:
        name = e.get("name", "?")
        score = e.get("score", "? - ?")
        target = e.get("target_line", "?")
        lc = e.get("league_country") or e.get("league", "")
        line = f"• {name} ({score}) | Line: {target}"
        if lc:
            line += f" | {lc}"
        lines.append(line)
    text = "\n".join(lines)
    history = _load_chat_session()
    history.append({"role": "model", "text": text, "ts": datetime.now(THOROLD_TZ).isoformat()})
    _save_chat_session(history)


def _load_day_start_balance() -> float:
    """Load the day-start balance snapshot. Returns 0 if missing."""
    global _balance_at_day_start
    if not os.path.isfile(DAY_START_BALANCE_JSON):
        _balance_at_day_start = balance_dollars
        _save_day_start_balance()
        return _balance_at_day_start
    try:
        with open(DAY_START_BALANCE_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)
        _balance_at_day_start = float(data.get("balance", 0) or 0)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        _balance_at_day_start = balance_dollars
    return _balance_at_day_start


def _save_day_start_balance() -> None:
    """Persist day-start balance snapshot."""
    try:
        with open(DAY_START_BALANCE_JSON, "w", encoding="utf-8") as f:
            json.dump({"balance": _balance_at_day_start, "date": _get_usage_date().isoformat()}, f)
    except OSError:
        pass


def _get_next_snap_id() -> int:
    """Return next snap ID (resets at 5 AM usage day). Persist to file."""
    global pending_snaps
    usage_date = _get_usage_date()
    if not os.path.isfile(SNAP_COUNTER_JSON):
        next_id = 1
    else:
        try:
            with open(SNAP_COUNTER_JSON, "r", encoding="utf-8") as f:
                data = json.load(f)
            saved_date = data.get("date")
            if saved_date == usage_date.isoformat():
                next_id = int(data.get("next_id", 1))
            else:
                next_id = 1
        except (OSError, json.JSONDecodeError, ValueError, TypeError):
            next_id = 1
    try:
        with open(SNAP_COUNTER_JSON, "w", encoding="utf-8") as f:
            json.dump({"date": usage_date.isoformat(), "next_id": next_id + 1}, f)
    except OSError:
        pass
    return next_id


def _load_pending_snaps() -> None:
    """Load pending_snaps from file. Clear if from previous usage day."""
    global pending_snaps
    usage_date = _get_usage_date()
    if not os.path.isfile(PENDING_SNAPS_JSON):
        pending_snaps = {}
        return
    try:
        with open(PENDING_SNAPS_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)
        saved_date = data.get("date")
        if saved_date == usage_date.isoformat():
            pending_snaps = {int(k): v for k, v in (data.get("snaps") or {}).items()}
        else:
            pending_snaps = {}
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        pending_snaps = {}


def _save_pending_snaps() -> None:
    """Persist pending_snaps to file."""
    try:
        with open(PENDING_SNAPS_JSON, "w", encoding="utf-8") as f:
            json.dump({
                "date": _get_usage_date().isoformat(),
                "snaps": {str(k): v for k, v in pending_snaps.items()},
            }, f, indent=2)
    except OSError:
        pass


def _accept_snaps(snap_ids: list[int]) -> tuple[float, list[int]]:
    """
    Legacy hook for /accept. No longer deducts from balance.
    Returns (0.0, []) so existing commands remain harmless if used.
    """
    return (0.0, [])


TUNABLE_PARAMS = {
    "MAX_SHOTS_30": "30' window: reject if total shots exceed this",
    "SHOTS_70_RED": "70' window: reject if total shots exceed this",
    "CORNERS_70_RED": "70' window: reject if total corners exceed this",
    "FOULS_70_HIGH": "70' window: fouls above this trigger extra-time warning",
    "TARGET_ODDS": "Send alert when combined parlay odds reach this",
    "MAX_QUEUE_SIZE": "Max matches per window before force-send",
    "POLL_INTERVAL_SECONDS": "Seconds between live-fixture polls during Hunter",
}


def _build_chat_context() -> str:
    """Assemble a snapshot of Snappi's live state for Gemini context, including today's snaps and capabilities."""
    now_t = get_thorold_now()
    phase = "Hunter" if is_hunter_phase() else "Analyst"
    unit = current_unit_dollars()
    q30 = len(flagged_30)
    q70 = len(flagged_70)
    rej_last5 = []
    for r in recent_rejections[-5:]:
        if len(r) >= 4:
            rej_last5.append(f"  {r[1]} @ {r[2]}' - {r[3]}")
    rej_text = "\n".join(rej_last5) if rej_last5 else "  None"

    params_text = "\n".join(
        f"  {k} = {globals().get(k, '?')}  ({v})"
        for k, v in TUNABLE_PARAMS.items()
    )

    mem_summary = json.dumps(_memory_data, ensure_ascii=False, indent=2) if _memory_data else "{}"

    bet_history_text = "  None yet."
    try:
        if os.path.isfile(BET_HISTORY_JSON):
            with open(BET_HISTORY_JSON, "r", encoding="utf-8") as f:
                bh = json.load(f)
            if bh:
                lines = []
                for rec in bh:
                    teams = rec.get("teams", "?")
                    window = rec.get("window", "?")
                    score = rec.get("score", "?")
                    target = rec.get("target_line", "?")
                    ts = rec.get("timestamp", "?")
                    lines.append(f"  {teams} | {window} | Score: {score} | Line: {target} | {ts}")
                bet_history_text = "\n".join(lines)
    except (json.JSONDecodeError, OSError):
        pass

    capabilities_text = ""
    try:
        cap_path = os.path.join(_script_dir, "CAPABILITIES.md")
        if os.path.isfile(cap_path):
            with open(cap_path, "r", encoding="utf-8") as f:
                # Trim to avoid blowing up context; this is a cheat sheet, not the full manual.
                capabilities_text = f.read(4000)
    except OSError:
        capabilities_text = ""

    return (
        f"Current Time (Thorold): {now_t.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
        f"Phase: {phase} (Hunter 05:00-00:00, Analyst 00:01-04:59)\n"
        f"Paused: {is_paused}\n"
        f"Balance: ${balance_dollars:.2f} | 1 unit = ${unit:.2f}\n"
        f"Total Profit (lifetime): ${total_profit:.2f}\n"
        f"API Usage: {api_calls_today}/{API_DAILY_LIMIT}\n"
        f"Alerts Sent Today: {alerts_sent_today}\n"
        f"Queue (30'): {q30} match(es) | Queue (70'): {q70} match(es)\n"
        f"Rejections This Session: {rejections_count}\n"
        f"\nToday's Snaps (bet_history.json):\n{bet_history_text}\n"
        f"\nRecent Rejections:\n{rej_text}\n"
        f"\nCurrent Thresholds:\n{params_text}\n"
        f"\nMemory:\n{mem_summary}\n"
        f"\nCapabilities Cheat Sheet (from CAPABILITIES.md):\n{capabilities_text}\n"
    )


def _load_balance() -> float:
    """Load balance from BALANCE_JSON. Return 0 if missing/invalid."""
    global balance_dollars
    if not os.path.isfile(BALANCE_JSON):
        return 0.0
    try:
        with open(BALANCE_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)
        balance_dollars = float(data.get("balance", 0) or 0)
        return balance_dollars
    except (OSError, ValueError, TypeError):
        balance_dollars = 0.0
        return 0.0


def _save_balance() -> None:
    """Persist balance_dollars to BALANCE_JSON."""
    try:
        with open(BALANCE_JSON, "w", encoding="utf-8") as f:
            json.dump({"balance": balance_dollars}, f)
    except OSError:
        pass


def _get_usage_date() -> date:
    """Usage day resets at 05:00 Thorold: before 05:00 we're still in yesterday's day."""
    now = get_thorold_now()
    return now.date() if now.hour >= HUNTER_START_HOUR else (now.date() - timedelta(days=1))


def _load_api_calls_state() -> None:
    """Load api_calls_today and api_calls_date from file if it's for current usage day (05:00 reset)."""
    global api_calls_today, api_calls_date
    if not os.path.isfile(API_CALLS_STATE_JSON):
        api_calls_date = _get_usage_date()
        api_calls_today = 0
        _save_api_calls_state()
        return
    try:
        with open(API_CALLS_STATE_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)
        saved_date_str = data.get("date")
        if not saved_date_str:
            api_calls_date = _get_usage_date()
            api_calls_today = 0
            return
        current_usage = _get_usage_date()
        if saved_date_str == current_usage.isoformat():
            api_calls_date = date.fromisoformat(saved_date_str)
            api_calls_today = int(data.get("count", 0))
        else:
            api_calls_today = 0
            api_calls_date = current_usage
            _save_api_calls_state()
    except (OSError, ValueError, TypeError):
        api_calls_date = _get_usage_date()
        api_calls_today = 0


def _save_api_calls_state() -> None:
    """Write current api_calls_today and api_calls_date to file."""
    if api_calls_date is None:
        return
    try:
        with open(API_CALLS_STATE_JSON, "w", encoding="utf-8") as f:
            json.dump({"date": api_calls_date.isoformat(), "count": api_calls_today}, f)
    except OSError:
        pass


def init_persistent_data() -> None:
    """At startup, read the last 20 lines of rejections.csv into recent_rejections (and set rejections_count)."""
    global recent_rejections, rejections_count
    recent_rejections = []
    if not os.path.isfile(REJECTIONS_CSV):
        return
    try:
        with open(REJECTIONS_CSV, "r", encoding="utf-8", newline="") as f:
            rows = list(csv.reader(f))
    except OSError:
        return
    if len(rows) <= 1:
        return
    data = rows[1:]
    recent_rejections = data[-20:] if len(data) > 20 else data
    rejections_count = len(recent_rejections)  # approximate "rejected today" after reboot


def log_rejection(
    match_name: str,
    minute: int,
    reason: str,
    stats: dict,
    fixture_id: int | None = None,
    window_minute: int | None = None,
) -> None:
    """
    Append a row to rejections.csv when a match is scanned but fails the low-activity check.
    stats should contain total_shots, total_corners (sum of Home + Away).
    If fixture_id and window_minute are provided, log at most once per (fixture_id, window_minute) per day.
    """
    global rejections_count, recent_rejections, logged_rejections
    if fixture_id is not None and window_minute is not None:
        key = (fixture_id, window_minute)
        if key in logged_rejections:
            return
        logged_rejections.add(key)
    file_exists = os.path.isfile(REJECTIONS_CSV)
    row = [
        time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
        match_name,
        str(minute),
        reason,
        str(stats.get("total_shots", "")),
        str(stats.get("total_corners", "")),
    ]
    with open(REJECTIONS_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if not file_exists:
            w.writerow(["timestamp", "match_name", "minute", "reason", "total_shots", "total_corners"])
        w.writerow(row)
    recent_rejections.append(row)
    if len(recent_rejections) > 20:
        recent_rejections.pop(0)
    rejections_count += 1


def append_bet_history(entries: list[dict], window_name: str) -> None:
    """Append every alerted match to bet_history.json (fixture_id, teams, window, score, target_line)."""
    history = []
    if os.path.isfile(BET_HISTORY_JSON):
        try:
            with open(BET_HISTORY_JSON, "r", encoding="utf-8") as f:
                history = json.load(f)
        except (json.JSONDecodeError, OSError):
            history = []
    for e in entries:
        rec = {
            "fixture_id": e.get("fixture_id"),
            "teams": e.get("name", "?"),
            "window": window_name,
            "prediction": "Under / Low pressure",
            "timestamp": datetime.now(THOROLD_TZ).isoformat(),
            "score": e.get("score", "? - ?"),
            "total_shots": e.get("total_shots", ""),
            "target_line": e.get("target_line", ""),
        }
        if "73-Minute" in window_name or "70-" in window_name:
            rec["score_at_70"] = e.get("score", "? - ?")
        history.append(rec)
    with open(BET_HISTORY_JSON, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2)


def _count_api_call() -> None:
    """Increment API call counter; reset at 05:00 Thorold (usage day). Persist to file so it survives restarts."""
    global api_calls_today, api_calls_date
    usage_date = _get_usage_date()
    if api_calls_date is None or api_calls_date != usage_date:
        api_calls_today = 0
        api_calls_date = usage_date
    api_calls_today += 1
    _save_api_calls_state()


def get_headers() -> dict:
    """API-Sports (api-sports.io) uses x-apisports-key from .env API_FOOTBALL_KEY."""
    return {
        "x-apisports-key": API_FOOTBALL_KEY or "",
    }


def _update_ratelimit_from_response(resp: requests.Response) -> None:
    """Store x-ratelimit-requests-remaining from API response for /status verification."""
    global api_ratelimit_remaining
    try:
        val = resp.headers.get("x-ratelimit-requests-remaining") or resp.headers.get("X-RateLimit-Requests-Remaining")
        if val is not None:
            api_ratelimit_remaining = int(val)
    except (TypeError, ValueError):
        pass


def _handle_api_429(label: str) -> None:
    """Log and optionally send one Telegram alert per day when API returns 429 (quota exceeded)."""
    global _last_429_alert_date, api_429_seen
    api_429_seen = True
    logger.warning(
        "API quota exceeded (429 Too Many Requests) on %s. "
        "No data until quota resets. Check your plan at api-sports.io.",
        label,
    )
    today = get_thorold_now().date()
    if _last_429_alert_date != today:
        _last_429_alert_date = today
        try:
            notifier.send_simple_message(
                "⚠️ <b>Snappi: API quota exceeded (429)</b>\n\n"
                "Your api-sports.io request limit is used up. No live data until it resets (often midnight UTC).\n"
                "Check your usage at api-sports.io dashboard."
            )
        except Exception:
            pass


def _get_with_connection_retry(url: str, params: dict, timeout: int):
    """Retry requests.get on ConnectionError with exponential backoff (5s, 10, 20, 40, 60s max)."""
    backoff = 5
    while True:
        try:
            return requests.get(url, params=params, headers=get_headers(), timeout=timeout)
        except requests.exceptions.ConnectionError as e:
            print(f"[Snappi] Connection error, retry in {backoff}s: {e}")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)


def _get_with_timeout_retry(
    url: str, params: dict, timeout: int, max_attempts: int = 3, label: str = "request"
):
    """
    Call requests.get; retry on ConnectionError or Timeout up to max_attempts.
    Delay 2s between attempts. Returns response or None.
    """
    for attempt in range(1, max_attempts + 1):
        try:
            return requests.get(url, params=params, headers=get_headers(), timeout=timeout)
        except requests.exceptions.ConnectionError as e:
            logger.warning("%s: connection error (attempt %d/%d): %s", label, attempt, max_attempts, e)
            if attempt < max_attempts:
                time.sleep(2)
            else:
                return None
        except requests.exceptions.Timeout as e:
            logger.warning("%s: timeout (attempt %d/%d): %s", label, attempt, max_attempts, e)
            if attempt < max_attempts:
                time.sleep(2)
            else:
                return None
    return None


def fetch_live_fixtures() -> list[dict]:
    """
    Call API-Football live fixtures endpoint.
    Returns list of fixture objects from response['response'].
    Retries up to 3 times on timeout/connection error so one slow response doesn't wipe the cycle.
    """
    if not API_FOOTBALL_KEY:
        raise ValueError("API_FOOTBALL_KEY must be set in .env")
    global _last_live_fixtures_count
    try:
        _count_api_call()
        resp = _get_with_timeout_retry(
            FIXTURES_URL, {"live": "all"}, REQUEST_TIMEOUT, max_attempts=3, label="Fixtures"
        )
        if resp is None:
            logger.warning("Fixtures: all retries failed; 0 live matches this cycle (check /logs).")
            return []
        _update_ratelimit_from_response(resp)
        if resp.status_code == 429:
            _handle_api_429("Fixtures")
            return []
        resp.raise_for_status()
        data = resp.json()
        out = data.get("response") or []
        errors = data.get("errors") or data.get("message") or ""
        global last_fixtures_zero_reason, api_429_seen
        if not out:
            last_fixtures_zero_reason = str(errors) if errors else f"response keys: {list(data.keys())}"
            # API can return 200 with 0 fixtures and errors.requests = "request limit for the day"
            err_str = str(errors).lower()
            if "request limit" in err_str or "limit for the day" in err_str:
                api_429_seen = True
                logger.warning(
                    "Fixtures: Daily request limit reached (API returned 0). "
                    "Quota resets at dashboard.api-football.com. No live data until tomorrow."
                )
            else:
                logger.info(
                    "Fixtures: API returned 0 live matches. keys=%s errors=%s.",
                    list(data.keys()),
                    errors,
                )
        else:
            last_fixtures_zero_reason = ""
            logger.info("Fixtures: API returned %d live match(es).", len(out))
        _last_live_fixtures_count = len(out)
        return out
    except requests.exceptions.RequestException as e:
        logger.warning("Fixtures request failed: %s", e)
        return []


def fetch_fixture_statistics(fixture_id: int) -> dict | None:
    """
    Fetch statistics for one fixture (Total Shots, Dangerous Attacks, etc.).
    Returns a dict with total_shots, total_corners, red_cards, fouls (sum of both teams).
    or None if the request fails or data is missing. Retries up to 2 times on timeout/connection.
    """
    try:
        _count_api_call()
        resp = _get_with_timeout_retry(
            STATISTICS_URL,
            {"fixture": fixture_id},
            STATS_TIMEOUT,
            max_attempts=2,
            label=f"Stats(fixture={fixture_id})",
        )
        if resp is None:
            return None
        _update_ratelimit_from_response(resp)
        if resp.status_code == 429:
            _handle_api_429(f"Stats(fixture={fixture_id})")
            return None
        resp.raise_for_status()
        data = resp.json()
        teams_data = data.get("response") or []
        if not teams_data:
            return None
        # Stat summing: API-Football returns stats per team. Sum Home + Away. DA removed (LOGIC.md).
        total_shots = 0
        total_corners = 0
        red_cards = 0
        fouls = 0
        for team_block in teams_data:
            for stat in team_block.get("statistics") or []:
                stype = (stat.get("type") or "").lower()
                try:
                    val = int(stat.get("value") or 0)
                except (TypeError, ValueError):
                    val = 0
                if "shot" in stype and "goal" in stype:
                    total_shots += val
                if "total shot" in stype:
                    total_shots += val
                if "corner" in stype:
                    total_corners += val
                if "red" in stype and "card" in stype:
                    red_cards += val
                if "foul" in stype:
                    fouls += val
        return {
            "total_shots": total_shots,
            "total_corners": total_corners,
            "red_cards": red_cards,
            "fouls": fouls,
        }
    except requests.exceptions.RequestException as e:
        logger.debug("Statistics request failed for fixture %s: %s", fixture_id, e)
        return None


# Odds cache: {fixture_id: (odds_value, cached_at_timestamp)}
_odds_cache: dict[int, tuple[float, float]] = {}

# Today's fixtures schedule: { "date": "YYYY-MM-DD", "total": N, "by_hour": { 12: 5, 19: 12 }, "fetched_at": iso }
# Used for /status and /schedule so you can see busy times. Cache TTL 2 hours.
TODAY_SCHEDULE_CACHE_TTL = 7200  # seconds
_todays_fixtures_schedule: dict | None = None


def get_todays_fixtures_schedule() -> dict | None:
    """
    Return today's fixture count and breakdown by hour (Thorold time) so you can see busy times.
    Uses API GET /fixtures?date=YYYY-MM-DD. Cached 2 hours. Returns None on failure.
    """
    global _todays_fixtures_schedule
    now = get_thorold_now()
    today_iso = now.date().isoformat()
    now_ts = time.time()

    def _is_valid(cached: dict | None) -> bool:
        if not cached or cached.get("date") != today_iso:
            return False
        fetched_at = cached.get("fetched_at")
        if not fetched_at:
            return False
        try:
            dt = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
            age = now_ts - dt.timestamp()
            return age < TODAY_SCHEDULE_CACHE_TTL
        except (ValueError, TypeError):
            return False

    if _is_valid(_todays_fixtures_schedule):
        return _todays_fixtures_schedule
    if os.path.isfile(TODAY_FIXTURES_CACHE_JSON):
        try:
            with open(TODAY_FIXTURES_CACHE_JSON, "r", encoding="utf-8") as f:
                file_schedule = json.load(f)
            if _is_valid(file_schedule):
                bh = file_schedule.get("by_hour") or {}
                file_schedule["by_hour"] = {int(k): v for k, v in bh.items()}
                _todays_fixtures_schedule = file_schedule
                return file_schedule
        except (OSError, json.JSONDecodeError, ValueError):
            pass

    if not API_FOOTBALL_KEY:
        return None
    try:
        _count_api_call()
        resp = _get_with_timeout_retry(
            FIXTURES_URL,
            {"date": today_iso},
            REQUEST_TIMEOUT,
            max_attempts=2,
            label="Fixtures(date)",
        )
        if resp is None:
            return _todays_fixtures_schedule
        _update_ratelimit_from_response(resp)
        if resp.status_code == 429:
            _handle_api_429("Fixtures(date)")
            return _todays_fixtures_schedule
        resp.raise_for_status()
        data = resp.json()
        items = data.get("response") or []
        by_hour: dict[int, int] = {}
        for item in items:
            fix = item.get("fixture") or {}
            ts = fix.get("timestamp")
            if ts is not None:
                try:
                    dt = datetime.fromtimestamp(int(ts), tz=THOROLD_TZ)
                    h = dt.hour
                    by_hour[h] = by_hour.get(h, 0) + 1
                except (ValueError, TypeError, OSError):
                    pass
            else:
                date_str = fix.get("date") or ""
                if date_str:
                    try:
                        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00")).astimezone(THOROLD_TZ)
                        h = dt.hour
                        by_hour[h] = by_hour.get(h, 0) + 1
                    except (ValueError, TypeError):
                        pass
        schedule = {
            "date": today_iso,
            "total": len(items),
            "by_hour": by_hour,
            "fetched_at": datetime.now(THOROLD_TZ).isoformat(),
        }
        _todays_fixtures_schedule = schedule
        try:
            with open(TODAY_FIXTURES_CACHE_JSON, "w", encoding="utf-8") as f:
                json.dump(schedule, f, indent=2)
        except OSError:
            pass
        return schedule
    except requests.exceptions.RequestException as e:
        logger.debug("Today fixtures schedule request failed: %s", e)
        return _todays_fixtures_schedule


def fetch_fixture_odds(fixture_id: int, target_line: str) -> float | None:
    """
    Fetch live/pre-match odds for fixture from API-Football. Looks for Under X.5
    matching target_line (e.g. "Under 2.5"). Prefers Bet365. Cached 5 min.
    Returns decimal odds or None.
    """
    now = time.time()
    if fixture_id in _odds_cache:
        val, cached_at = _odds_cache[fixture_id]
        if now - cached_at < ODDS_CACHE_TTL:
            return val
        del _odds_cache[fixture_id]
    try:
        _count_api_call()
        resp = _get_with_timeout_retry(
            ODDS_URL,
            {"fixture": fixture_id},
            STATS_TIMEOUT,
            max_attempts=2,
            label=f"Odds(fixture={fixture_id})",
        )
        if resp is None:
            return None
        _update_ratelimit_from_response(resp)
        if resp.status_code == 429:
            _handle_api_429(f"Odds(fixture={fixture_id})")
            return None
        resp.raise_for_status()
        data = resp.json()
        items = data.get("response") or []
        if not items:
            return None
        best_odds: float | None = None
        best_from_bet365 = False
        # target_line is "Under 1.5", "Under 2.5", etc. Extract line for matching.
        target_lower = (target_line or "").strip().lower()
        line_match = re.search(r"under\s*(\d+(?:\.5)?)", target_lower)
        target_num = line_match.group(1) if line_match else ""
        for item in items:
            for bm in item.get("bookmakers") or []:
                bm_name = (bm.get("name") or "").lower()
                is_bet365 = "bet365" in bm_name or "bet 365" in bm_name
                for bet in bm.get("bets") or []:
                    bet_name = (bet.get("name") or "").lower()
                    if "over" not in bet_name and "under" not in bet_name:
                        continue
                    if "goal" not in bet_name and "total" not in bet_name:
                        continue
                    for v in bet.get("values") or []:
                        val_str = (v.get("value") or "").strip().lower()
                        if not val_str.startswith("under"):
                            continue
                        # Match line: "under 2.5" or handicap/total field
                        handicap = str(v.get("handicap") or v.get("total") or "")
                        if target_num and target_num not in val_str and target_num not in handicap:
                            continue
                        try:
                            odd_val = float(v.get("odd") or v.get("price") or 0)
                        except (TypeError, ValueError):
                            continue
                        if odd_val <= 0:
                            continue
                        if best_odds is None or (is_bet365 and not best_from_bet365) or (is_bet365 == best_from_bet365 and odd_val > (best_odds or 0)):
                            best_odds = odd_val
                            best_from_bet365 = is_bet365
        if best_odds is not None:
            _odds_cache[fixture_id] = (best_odds, now)
        return best_odds
    except requests.exceptions.RequestException as e:
        logger.debug("Odds request failed for fixture %s: %s", fixture_id, e)
        return None


def get_elapsed(match_obj: dict) -> int | None:
    """Get current minute from fixture status. Returns None if missing."""
    try:
        fixture = match_obj.get("fixture") or {}
        status = fixture.get("status") or {}
        return status.get("elapsed")
    except (TypeError, AttributeError):
        return None


def get_goals(match_obj: dict) -> tuple[int, int]:
    """Return (home_goals, away_goals). Uses 0 if missing."""
    try:
        goals = match_obj.get("goals") or {}
        h = goals.get("home")
        a = goals.get("away")
        return (int(h) if h is not None else 0, int(a) if a is not None else 0)
    except (TypeError, ValueError):
        return (0, 0)


def get_team_names(match_obj: dict) -> tuple[str, str]:
    """Return (home_name, away_name) from teams object."""
    try:
        teams = match_obj.get("teams") or {}
        home = (teams.get("home") or {}).get("name") or "?"
        away = (teams.get("away") or {}).get("name") or "?"
        return (home, away)
    except (TypeError, AttributeError):
        return ("?", "?")


def get_league(match_obj: dict) -> str:
    """Return league name from match object (API: league.name or league.country)."""
    try:
        league = match_obj.get("league") or {}
        return (league.get("name") or league.get("country") or "").strip()
    except (TypeError, AttributeError):
        return ""


def get_league_and_country(match_obj: dict) -> tuple[str, str]:
    """Return (league_name, country) from match object. Either may be empty."""
    try:
        league = match_obj.get("league") or {}
        name = (league.get("name") or "").strip()
        country = (league.get("country") or "").strip()
        return (name, country)
    except (TypeError, AttributeError):
        return ("", "")


def _format_league_country(league_name: str, country: str) -> str:
    """Format league and country for display. e.g. 'Premier League (England)' or 'Serie A'."""
    if league_name and country and league_name != country:
        return f"{league_name} ({country})"
    return league_name or country or ""


def target_line_from_score(home_goals: int, away_goals: int) -> str:
    """Line = Current total goals + 1.5. e.g. 0-0 → Under 1.5, 1-1 → Under 3.5."""
    total = home_goals + away_goals
    line = total + 1.5
    return f"Under {line:.1f}"


def _is_league_excluded(league_name: str) -> bool:
    """True if league_name matches any EXCLUDED_LEAGUES entry (case-insensitive, substring)."""
    if not league_name or not EXCLUDED_LEAGUES:
        return False
    ln = (league_name or "").strip().lower()
    for exc in EXCLUDED_LEAGUES:
        if (exc or "").strip().lower() in ln:
            return True
    return False


def build_match_entry(
    match_obj: dict,
    total_shots: int = 0,
    total_corners: int = 0,
    red_cards: int = 0,
    fouls: int = 0,
    bet365_selection_id: str | None = None,
    odds: float | None = None,
) -> dict:
    """Build match dict for queue and notifier. target_line = score + 1.5. DA removed."""
    home, away = get_team_names(match_obj)
    h, a = get_goals(match_obj)
    league_name, country = get_league_and_country(match_obj)
    league_display = _format_league_country(league_name, country)
    return {
        "fixture_id": (match_obj.get("fixture") or {}).get("id"),
        "name": f"{home} vs {away}",
        "home": home,
        "away": away,
        "score": f"{h} - {a}",
        "total_shots": total_shots,
        "total_corners": total_corners,
        "red_cards": red_cards,
        "fouls": fouls,
        "target_line": target_line_from_score(h, a),
        "bet365_selection_id": bet365_selection_id,
        "odds": odds if odds is not None else DEFAULT_ODDS,
        "league": get_league(match_obj),
        "league_name": league_name,
        "country": country,
        "league_country": league_display,
    }


def _search_term_for_match(match: dict) -> str:
    """Home team name for search query (Bet365/MGM)."""
    home = (match.get("home") or "").strip()
    if home:
        return home
    name = (match.get("name") or "").strip()
    if name and " vs " in name:
        return name.split(" vs ")[0].strip()
    return name or "?"


def build_bet365_link_for_match(match: dict) -> str:
    """Bet365 in-play link with search for this match (home team)."""
    base = BET365_BASE.rstrip("/")
    q = _search_term_for_match(match)
    if q and q != "?":
        return base + "?q=" + quote(q)
    return base


def build_alert_button_rows(entries: list[dict]) -> list[list[tuple[str, str]]]:
    """Single button to open Bet365 app (one link per alert; search in links is unreliable)."""
    return [[("Open Bet365", BET365_BASE.rstrip("/"))]]


def total_parlay_odds(entries: list[dict]) -> float:
    """Multiply individual odds to get estimated combined parlay odds."""
    product = 1.0
    for e in entries:
        product *= e.get("odds") or DEFAULT_ODDS
    return product


def _run_sentry_reply(
    entries: list[dict],
    window_name: str,
    chat_id: int,
    reply_to_message_id: int,
    batch_ts: str,
    snap_id: int,
) -> None:
    """
    Background: fetch events, ask Gemini Sentry for one RED/YELLOW/GREEN per match + narrative.
    Reply to the alert with per-match colours; update each sheet row's Sentry_Colour.
    Stake by color (RED=0.5u, YELLOW=2u, GREEN=3u). Same color → parlay; others singles. Fouls > 15 → High Extra Time Risk.
    Balance is no longer auto-deducted here; /accept is legacy only.
    """
    global pending_snaps
    if not entries or not chat_id or not reply_to_message_id:
        return
    events_by_fid: dict[int, list[str]] = {}
    for e in entries:
        fid = e.get("fixture_id")
        if fid:
            evts = fetch_fixture_events(fid)
            events_by_fid[fid] = events_after_70(evts)
    labels, narrative = notifier.ask_gemini_sentry(entries, events_by_fid)
    if not labels:
        labels = ["YELLOW"] * len(entries)
    high_extra_time = any((e.get("fouls") or 0) > 15 for e in entries)
    unit_dollars = current_unit_dollars()
    # Hamilton's rules: GREEN=3u, YELLOW=2u, RED=0.5u. Group same color → parlay; others as singles.
    unit_map = {"RED": 0.5, "YELLOW": 2, "GREEN": 3}
    color_groups: dict[str, list[dict]] = {}
    for e, lab in zip(entries, labels):
        c = (lab or "YELLOW").strip().upper()
        if c not in ("RED", "YELLOW", "GREEN"):
            c = "YELLOW"
        color_groups.setdefault(c, []).append(e)
    total_stake_dollars = sum(unit_map.get(c, 2) * unit_dollars for c in color_groups)
    units_display = sum(unit_map.get(c, 2) for c in color_groups)  # total units across groups
    recipient_ids = get_snap_recipient_ids()
    extras = [x for x in recipient_ids if x != chat_id]
    notifier.send_sentry_reply(
        chat_id, reply_to_message_id, labels, narrative, total_stake_dollars, units_display, entries,
        high_extra_time=high_extra_time, snap_id=snap_id, also_send_to_chat_ids=extras,
    )
    try:
        pending = sheets_logger.get_pending_sheet_rows()
        candidates = [r for r in pending if r.get("window") == window_name and r.get("timestamp") == batch_ts]
        if not candidates and pending and batch_ts:
            candidates = [
                r for r in pending
                if r.get("window") == window_name and (r.get("timestamp") or "").startswith(batch_ts[:19])
            ]
        if len(candidates) == len(entries):
            by_fid = {r["fixture_id"]: r for r in candidates}
            for i, e in enumerate(entries):
                fid = e.get("fixture_id")
                row = by_fid.get(fid)
                if row and i < len(labels):
                    c = (labels[i] or "YELLOW").strip().upper()
                    if c not in ("RED", "YELLOW", "GREEN"):
                        c = "YELLOW"
                    row_units = unit_map.get(c, 2)
                    row_stake = unit_dollars * row_units
                    sn = row.get("sheet_name", sheets_logger._sheet_for_window(window_name))
                    sheets_logger.update_sentry_label(
                        row["row_index"], labels[i], sheet_name=sn,
                        units=row_units, stake_dollars=row_stake,
                        narrative=narrative,
                    )
    except Exception as e:
        logger.error("Sentry sheet update failed: %s", e)

    # Auto-place on Pinnacle: group by color — parlays for 2+ same color, singles otherwise.
    if os.getenv("AUTO_PLACE_PINNACLE", "").lower() in ("1", "true", "yes") and pinnacle.is_configured():
        try:
            results = pinnacle.place_bets_by_color_groups(color_groups, unit_dollars, unit_map)
            for desc, res in results:
                if res and res.get("status") == "ACCEPTED":
                    logger.info("Pinnacle: %s — betId %s", desc, res.get("betId"))
                elif res and res.get("status") == "PROCESSED_WITH_ERROR":
                    logger.warning("Pinnacle: %s — %s", desc, res.get("errorCode", "?"))
        except Exception as ex:
            logger.warning("Pinnacle place bets: %s", ex)


def _enrich_entries_with_forebet(entries: list[dict]) -> None:
    """
    Attach Forebet prediction to each entry in place (forebet_summary, forebet_under_over, forebet_predicted_score).
    Uses cached predictions; one fetch for all entries.
    """
    if not entries:
        return
    try:
        predictions = forebet.fetch_forebet_predictions()
    except Exception as e:
        logger.debug("Forebet fetch for enrichment failed: %s", e)
        return
    if not predictions:
        logger.debug("Forebet enrichment skipped: no predictions available.")
        return
    today_iso = _get_usage_date().isoformat()
    for e in entries:
        home = e.get("home") or ""
        away = e.get("away") or ""
        if not home or not away:
            continue
        league = e.get("league") or e.get("league_country") or ""
        fb = forebet.get_forebet_for_match(home, away, match_date=today_iso, league=league, predictions=predictions)
        if not fb:
            logger.debug(
                "Forebet: no match found for entry",
                extra={
                    "home": home,
                    "away": away,
                    "league": league,
                    "match_date": today_iso,
                },
            )
            continue
        pred_score = (fb.get("predictedScore") or "").strip().replace(" ", "-") or None
        under_over = (fb.get("underOverPrediction") or "").strip() or None
        prob_under = fb.get("probability_under_percent")
        prob_over = fb.get("probability_over_percent")
        if isinstance(prob_under, str):
            try:
                prob_under = int(prob_under)
            except ValueError:
                prob_under = None
        if isinstance(prob_over, str):
            try:
                prob_over = int(prob_over)
            except ValueError:
                prob_over = None
        parts = []
        if pred_score:
            parts.append(pred_score)
        if under_over:
            pct = prob_over if under_over == "Over" else prob_under
            if pct is not None:
                parts.append(f"{under_over} 2.5 ({pct}%)")
            else:
                parts.append(f"{under_over} 2.5")
        e["forebet_summary"] = "Forebet: " + ", ".join(parts) if parts else None
        e["forebet_under_over"] = under_over
        e["forebet_predicted_score"] = pred_score
        e["forebet_prob_under"] = prob_under
        e["forebet_prob_over"] = prob_over


def send_queue_alert(active_queue: dict[int, dict], window_name: str) -> bool:
    """
    Send an alert immediately with whatever is in the queue (1–5 matches).
    Used for the 3-minute MASTER timer and for 36'/74' safety nets.
    Ensures Pressure Stats (Shots/Attacks) and dynamic label (SOLO/DOUBLE/PARLAY) are correct.
    Only sends for matches not already alerted in this window (deduplication). Clears the queue. Returns True.
    """
    global alerts_sent_today, sent_alerts
    entries = [e for e in list(active_queue.values()) if (e.get("fixture_id"), window_name) not in sent_alerts]
    if not entries:
        active_queue.clear()
        return False
    for e in entries:
        sent_alerts.add((e.get("fixture_id"), window_name))
    _enrich_entries_with_forebet(entries)
    snap_id = _get_next_snap_id()
    button_rows = build_alert_button_rows(entries)
    unit_dollars = current_unit_dollars()
    msg_id, chat_id = notifier.send_snappi_alert(
        window_name, entries, button_rows, unit_dollars, snap_id=snap_id,
        recipient_chat_ids=get_snap_recipient_ids(),
    )
    alerts_sent_today += 1
    append_bet_history(entries, window_name)
    _append_snap_to_chat_log(entries, window_name, snap_id)
    batch_ts = datetime.now(THOROLD_TZ).isoformat()
    try:
        for e in entries:
            ok = sheets_logger.log_bet_to_sheet(
                e, window_name, e.get("league", ""), batch_timestamp=batch_ts, snap_id=snap_id
            )
            if not ok:
                logger.error("Sheet logging failed for %s (%s)", e.get("name", "?"), window_name)
        sheets_logger.trim_rejections(REJECTIONS_CSV)
    except Exception as e:
        logger.error("Sheet logging exception in send_queue_alert: %s", e)
    active_queue.clear()
    if msg_id and chat_id:
        try:
            threading.Thread(
                target=_run_sentry_reply,
                args=(entries, window_name, chat_id, msg_id, batch_ts, snap_id),
                daemon=True,
            ).start()
        except Exception:
            pass
    return True


def check_and_send_alert(
    active_queue: dict[int, dict],
    window_name: str,
    force_send: bool = False,
) -> bool:
    """
    If active_queue has at least MIN_PARLAY_SIZE (1):
      - If force_send (e.g. queue hit 5), send and clear.
      - Else if total odds >= TARGET_ODDS or size >= MAX_QUEUE_SIZE, send and clear.
    Only sends for matches not already alerted in this window (deduplication).
    Used for queue-full (5) and target-odds triggers. Timer/safety use send_queue_alert.
    Returns True if an alert was sent (caller should clear the window timer).
    """
    global alerts_sent_today, sent_alerts
    entries = list(active_queue.values())
    n = len(entries)
    if n < MIN_PARLAY_SIZE:
        return False
    entries = [e for e in entries if (e.get("fixture_id"), window_name) not in sent_alerts]
    if not entries:
        return False
    total_odds = total_parlay_odds(entries)
    if force_send or total_odds >= TARGET_ODDS or len(entries) >= MAX_QUEUE_SIZE:
        for e in entries:
            sent_alerts.add((e.get("fixture_id"), window_name))
        _enrich_entries_with_forebet(entries)
        snap_id = _get_next_snap_id()
        unit_dollars = current_unit_dollars()
        batch_ts = datetime.now(THOROLD_TZ).isoformat()
        msg_id, chat_id = notifier.send_snappi_alert(
            window_name, entries, build_alert_button_rows(entries), unit_dollars, snap_id=snap_id,
            recipient_chat_ids=get_snap_recipient_ids(),
        )
        alerts_sent_today += 1
        append_bet_history(entries, window_name)
        _append_snap_to_chat_log(entries, window_name, snap_id)
        try:
            for e in entries:
                ok = sheets_logger.log_bet_to_sheet(
                    e, window_name, e.get("league", ""), batch_timestamp=batch_ts, snap_id=snap_id
                )
                if not ok:
                    logger.error("Sheet logging failed for %s (%s)", e.get("name", "?"), window_name)
            sheets_logger.trim_rejections(REJECTIONS_CSV)
        except Exception as e:
            logger.error("Sheet logging exception in check_and_send_alert: %s", e)
        active_queue.clear()
        if msg_id and chat_id:
            try:
                threading.Thread(
                    target=_run_sentry_reply,
                    args=(entries, window_name, chat_id, msg_id, batch_ts, snap_id),
                    daemon=True,
                ).start()
            except Exception:
                pass
        return True
    return False


def score_is_low(home: int, away: int) -> bool:
    """Consider score 'low' for 70' window: total goals <= 2."""
    return home + away <= 2


def score_ok_for_30(home: int, away: int) -> bool:
    """30' scan: only 0-0 or 1-0 (either side)."""
    return (home == 0 and away == 0) or (home == 1 and away == 0) or (home == 0 and away == 1)


def process_live_matches() -> None:
    """
    V3.5 (LOGIC.md): Queue window 25-28, fire at 28; queue 70-73, fire at 73.
    Add matches that pass guards during the window; when any match in queue reaches fire minute, send alert.
    """
    global flagged_30, flagged_70, queue_30_started_at, queue_70_started_at

    check_pending_ft_resolution()

    matches = fetch_live_fixtures()
    if not matches:
        return

    elapsed_by_fixture: dict[int, int] = {}
    for match_obj in matches:
        fid = (match_obj.get("fixture") or {}).get("id")
        el = get_elapsed(match_obj)
        if fid is not None and el is not None:
            elapsed_by_fixture[fid] = el

    # --- Fire at 28: if any match in 30' queue has elapsed >= 28, send now ---
    if flagged_30:
        if any(elapsed_by_fixture.get(fid, 0) >= WINDOW_1_MIN_END for fid in flagged_30):
            send_queue_alert(flagged_30, "28-Minute Scan")
            queue_30_started_at = None
        else:
            if check_and_send_alert(flagged_30, "28-Minute Scan"):
                queue_30_started_at = None

    # --- Fire at 73: if any match in 70' queue has elapsed >= 73, send now ---
    if flagged_70:
        if any(elapsed_by_fixture.get(fid, 0) >= WINDOW_2_MIN_END for fid in flagged_70):
            send_queue_alert(flagged_70, "73-Minute Scan")
            queue_70_started_at = None
        else:
            if check_and_send_alert(flagged_70, "73-Minute Scan"):
                queue_70_started_at = None

    now = time.time()

    # --- Scan 1 (25-28): add to queue; start timer when first added ---
    for match_obj in matches:
        elapsed = get_elapsed(match_obj)
        if elapsed is None:
            continue
        home_goals, away_goals = get_goals(match_obj)
        fixture_id = (match_obj.get("fixture") or {}).get("id")
        if not fixture_id:
            continue

        if WINDOW_1_MIN_START <= elapsed <= WINDOW_1_MIN_END and score_ok_for_30(home_goals, away_goals):
            stats = fetch_fixture_statistics(fixture_id)
            if stats is not None:
                total_shots = stats.get("total_shots", 99)
                if total_shots > MAX_SHOTS_30:
                    match_name = f"{get_team_names(match_obj)[0]} vs {get_team_names(match_obj)[1]}"
                    log_rejection(
                        match_name, elapsed, f"High Shots (> {MAX_SHOTS_30})", stats,
                        fixture_id=fixture_id, window_minute=28,
                    )
                else:
                    target_line = target_line_from_score(home_goals, away_goals)
                    odds = fetch_fixture_odds(fixture_id, target_line)
                    entry = build_match_entry(
                        match_obj,
                        total_shots=total_shots,
                        total_corners=stats.get("total_corners", 0),
                        red_cards=stats.get("red_cards", 0),
                        fouls=stats.get("fouls", 0),
                        odds=odds,
                    )
                    if (
                        (fixture_id, "28-Minute Scan") not in sent_alerts
                        and fixture_id not in flagged_30
                        and len(flagged_30) < MAX_QUEUE_SIZE
                        and not _is_league_excluded(entry.get("league_name") or entry.get("league") or "")
                    ):
                        flagged_30[fixture_id] = entry
                        if queue_30_started_at is None:
                            queue_30_started_at = now
                    if check_and_send_alert(flagged_30, "28-Minute Scan"):
                        queue_30_started_at = None

        # --- Scan 2 (70-73): low score. Veto: Red. Reject: Shots>25, Corners>10. Fouls>15 → narrative in Sentry ---
        if WINDOW_2_MIN_START <= elapsed <= WINDOW_2_MIN_END and score_is_low(home_goals, away_goals):
            stats = fetch_fixture_statistics(fixture_id)
            if stats is not None:
                total_shots_70 = stats.get("total_shots", 0)
                corners = stats.get("total_corners", 0)
                red_cards_70 = stats.get("red_cards", 0)
                fouls_70 = stats.get("fouls", 0)
                match_name = f"{get_team_names(match_obj)[0]} vs {get_team_names(match_obj)[1]}"
                if red_cards_70 > 0:
                    log_rejection(
                        match_name, elapsed, "Red Card (veto)", stats,
                        fixture_id=fixture_id, window_minute=73,
                    )
                elif total_shots_70 > SHOTS_70_RED:
                    log_rejection(
                        match_name, elapsed, f"Shots > {SHOTS_70_RED}", stats,
                        fixture_id=fixture_id, window_minute=73,
                    )
                elif corners > CORNERS_70_RED:
                    log_rejection(
                        match_name, elapsed, f"Corners > {CORNERS_70_RED}", stats,
                        fixture_id=fixture_id, window_minute=73,
                    )
                else:
                    target_line = target_line_from_score(home_goals, away_goals)
                    odds = fetch_fixture_odds(fixture_id, target_line)
                    entry = build_match_entry(
                        match_obj,
                        total_shots=total_shots_70,
                        total_corners=corners,
                        red_cards=red_cards_70,
                        fouls=fouls_70,
                        odds=odds,
                    )
                    if (
                        (fixture_id, "73-Minute Scan") not in sent_alerts
                        and fixture_id not in flagged_70
                        and len(flagged_70) < MAX_QUEUE_SIZE
                        and not _is_league_excluded(entry.get("league_name") or entry.get("league") or "")
                    ):
                        flagged_70[fixture_id] = entry
                        if queue_70_started_at is None:
                            queue_70_started_at = now
                    if check_and_send_alert(flagged_70, "73-Minute Scan"):
                        queue_70_started_at = None


SESSION_SUMMARY_INTERVAL = 600  # 10 minutes
HEARTBEAT_INTERVAL = 3600  # 1 hour during Analyst phase
FT_CHECK_INTERVAL = 120  # Check pending fixtures for FT every 2 min
_last_ft_check_at: float = 0.0


def check_pending_ft_resolution() -> None:
    """Real-time: get PENDING rows from sheet, if fixture is FT update row (Final Score, Status, Result)."""
    global _last_ft_check_at
    now = time.time()
    if now - _last_ft_check_at < FT_CHECK_INTERVAL:
        return
    _last_ft_check_at = now
    try:
        pending = sheets_logger.get_pending_sheet_rows()
    except Exception:
        return
    for row in pending:
        fid = row.get("fixture_id")
        row_index = row.get("row_index")
        target_line = (row.get("target_line") or "").strip()
        sn = row.get("sheet_name", sheets_logger.FULLTIME_SHEET)
        if not fid or not row_index:
            continue
        result = fetch_fixture_result(fid)
        if not result:
            continue
        status = result.get("status", "")
        use_ht = (sn == sheets_logger.HALFTIME_SHEET)
        # Halftime bets: resolve at HT (or FT). Fulltime bets: resolve only at FT.
        if use_ht:
            if status not in ("HT", "FT"):
                continue
            if result.get("home_ht") is None or result.get("away_ht") is None:
                continue
        else:
            if status != "FT":
                continue
        if use_ht and result.get("home_ht") is not None and result.get("away_ht") is not None:
            h, a = result["home_ht"], result["away_ht"]
            final_score = f"{h} - {a} (HT)"
        else:
            h, a = result["home_goals"], result["away_goals"]
            final_score = f"{h} - {a}"
        total_goals = h + a
        threshold = _loss_threshold_from_target_line(target_line)
        if total_goals >= threshold:
            res = "LOSS"
        else:
            res = "WIN"
        sheets_logger.update_row_on_ft(row_index, final_score, res, sheet_name=sn)
        logger.info("FT resolution: fixture %s -> %s %s (sheet: %s)", fid, final_score, res, sn)
        # Proactive result notification so Hamilton knows immediately
        teams = row.get("teams", "?")
        emoji = "✅" if res == "WIN" else "❌"
        try:
            notifier.send_simple_message(
                f"{emoji} <b>Snap result:</b> {teams}\n"
                f"→ {final_score} | <b>{res}</b>"
            )
        except Exception:
            pass


def get_thorold_now() -> datetime:
    """Current time in Thorold, Ontario (America/Toronto)."""
    return datetime.now(THOROLD_TZ)


def is_hunter_phase() -> bool:
    """True if current time is 05:00–midnight (00:00) Thorold (The Hunter). Hours 5–23 = Hunter; 0–4 = Analyst."""
    now = get_thorold_now()
    return HUNTER_START_HOUR <= now.hour < HUNTER_END_HOUR


def fetch_fixture_result(fixture_id: int) -> dict | None:
    """Fetch fixture by ID from api-sports.io; return dict with home, away, full_time_goals or None."""
    try:
        _count_api_call()
        resp = _get_with_connection_retry(FIXTURES_URL, {"id": fixture_id}, REQUEST_TIMEOUT)
        if resp is None:
            return None
        _update_ratelimit_from_response(resp)
        if resp.status_code == 429:
            _handle_api_429(f"Fixture result {fixture_id}")
            return None
        resp.raise_for_status()
        data = resp.json()
        items = data.get("response") or []
        if not items:
            return None
        obj = items[0]
        fixture = obj.get("fixture") or {}
        status = (fixture.get("status") or {}).get("short") or ""
        goals = obj.get("goals") or {}
        home = goals.get("home")
        away = goals.get("away")
        if home is None and "fulltime" in goals:
            ft = goals["fulltime"] or {}
            home = ft.get("home")
            away = ft.get("away")
        ht_home, ht_away = None, None
        ht = goals.get("halftime") if isinstance(goals.get("halftime"), dict) else None
        if ht:
            ht_home, ht_away = ht.get("home"), ht.get("away")
        teams = obj.get("teams") or {}
        home_name = (teams.get("home") or {}).get("name") or "?"
        away_name = (teams.get("away") or {}).get("name") or "?"
        return {
            "status": status,
            "home_name": home_name,
            "away_name": away_name,
            "home_goals": int(home) if home is not None else 0,
            "away_goals": int(away) if away is not None else 0,
            "home_ht": int(ht_home) if ht_home is not None else None,
            "away_ht": int(ht_away) if ht_away is not None else None,
        }
    except Exception as e:
        print(f"[Snappi] Fetch fixture {fixture_id} failed: {e}")
        return None


def fetch_fixture_events(fixture_id: int) -> list[dict]:
    """Fetch fixture events from api-sports.io; return list of events (time, type, detail)."""
    try:
        _count_api_call()
        resp = _get_with_connection_retry(EVENTS_URL, {"fixture": fixture_id}, STATS_TIMEOUT)
        if resp is None:
            return []
        _update_ratelimit_from_response(resp)
        if resp.status_code == 429:
            _handle_api_429(f"Events(fixture={fixture_id})")
            return []
        resp.raise_for_status()
        data = resp.json()
        return data.get("response") or []
    except Exception as e:
        print(f"[Snappi] Fetch events for fixture {fixture_id} failed: {e}")
        return []


def events_after_70(events: list[dict]) -> list[str]:
    """Extract Goals and Red Cards that happened after the 70th minute. Return list of short descriptions."""
    out = []
    for ev in events:
        time_obj = ev.get("time") or {}
        elapsed = time_obj.get("elapsed") if isinstance(time_obj, dict) else None
        if elapsed is None:
            try:
                elapsed = int(ev.get("time", 0))
            except (TypeError, ValueError):
                continue
        if int(elapsed) <= 70:
            continue
        etype = (ev.get("type") or "").strip()
        detail = (ev.get("detail") or "").strip()
        if etype and "goal" in etype.lower():
            out.append(f"{elapsed}' Goal ({detail or 'Goal'})")
        if etype and "card" in etype.lower() and "red" in detail.lower():
            out.append(f"{elapsed}' Red Card")
    return out


def _load_total_profit() -> float:
    """Load total profit from file."""
    if not os.path.isfile(TOTAL_PROFIT_JSON):
        return 0.0
    try:
        with open(TOTAL_PROFIT_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)
        return float(data.get("total_profit", 0.0))
    except (OSError, ValueError, TypeError):
        return 0.0


def _save_total_profit(value: float) -> None:
    """Persist total profit to file."""
    try:
        with open(TOTAL_PROFIT_JSON, "w", encoding="utf-8") as f:
            json.dump({"total_profit": value}, f)
    except OSError:
        pass


def _append_optimization_log(line: str) -> None:
    """Append one line to optimization_log.txt with timestamp."""
    try:
        ts = datetime.now(THOROLD_TZ).strftime("%Y-%m-%d %H:%M:%S")
        with open(OPTIMIZATION_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {line}\n")
    except OSError:
        pass


def _build_snap_rows_text(rows: list[dict]) -> str:
    """Build a plain-text summary of sheet rows for Gemini (Match | Window | League | Odds | Forebet | Result | Stake)."""
    lines = []
    for r in rows:
        match = (r.get("Match") or "").strip()
        window = (r.get("Window") or "").strip()
        league = (r.get("League") or "").strip()
        odds = (r.get("Odds") or "").strip()
        forebet = (r.get("Forebet_Summary") or "").strip()
        result = (r.get("Result") or "").strip()
        status = (r.get("Status") or "").strip()
        stake = (r.get("Stake_Dollars") or "").strip()
        score = (r.get("Score") or "").strip()
        final = (r.get("Final Score") or "").strip()
        lines.append(f"{match} | {window} | {league} | Odds {odds} | {forebet or '-'} | {result or status} | Stake ${stake} | Score {score} -> {final or '-'}")
    return "\n".join(lines) if lines else ""


def run_weekly_report() -> None:
    """Sunday 8 AM: get last 7 days of sheet rows, ask Gemini for weekly breakdown, send to Telegram and append to optimization_log."""
    global _last_weekly_report_date
    now = get_thorold_now()
    end_date = now.date()
    start_date = end_date - timedelta(days=6)
    start_iso = start_date.isoformat()
    end_iso = end_date.isoformat()
    try:
        rows = sheets_logger.get_rows_for_date_range(start_iso, end_iso)
    except Exception as e:
        logger.error("Weekly report: get_rows_for_date_range failed: %s", e)
        notifier.send_simple_message(f"📊 <b>Weekly report</b> failed: could not read sheets ({e!s}).")
        return
    rows_text = _build_snap_rows_text(rows)
    if not rows_text.strip():
        notifier.send_simple_message(
            f"📊 <b>Weekly report</b> ({start_iso} to {end_iso})\n\nNo snaps in this period."
        )
        _last_weekly_report_date = now.date()
        return
    try:
        breakdown = notifier.ask_gemini_weekly_breakdown(rows_text, start_iso, end_iso)
    except Exception as e:
        logger.error("Weekly report: Gemini failed: %s", e)
        notifier.send_simple_message(f"📊 <b>Weekly report</b> failed: {e!s}")
        return
    report_title = f"📊 <b>Weekly report</b> ({start_iso} to {end_iso})"
    msg = f"{report_title}\n\n{_html_escape(breakdown)}"
    if len(msg) > 4000:
        msg = msg[:3970] + "\n...(truncated)"
    try:
        notifier.send_simple_message(msg)
    except Exception:
        pass
    _append_optimization_log(f"Weekly report ({start_iso} to {end_iso}):\n{breakdown}")
    _last_weekly_report_date = now.date()
    print(f"[Snappi] Weekly report sent ({start_iso} to {end_iso}, {len(rows)} rows).")


def nightly_analysis() -> None:
    """
    The Analyst: run at 00:05 Thorold. Resolve PENDING rows (WIN/LOSS by final score),
    calculate net profit as balance delta, run personality reflection, then self-optimization.
    """
    global total_profit, _last_nightly_profit, _last_nightly_date, _last_daily_summary_date
    total_profit = _load_total_profit()
    try:
        pending = sheets_logger.get_pending_sheet_rows()
    except Exception as e:
        logger.error("Nightly analysis: failed to get pending rows: %s", e)
        pending = []

    wins = 0
    losses = 0
    loss_details: list[tuple[str, str]] = []
    updates: list[dict] = []

    if pending:
        def parlay_key(r):
            return (r.get("timestamp", ""), r.get("window", ""))
        pending_sorted = sorted(pending, key=parlay_key)
        groups = [list(g) for _, g in groupby(pending_sorted, key=parlay_key)]

        for group in groups:
            if len(group) == 1:
                row = group[0]
                fid = row["fixture_id"]
                teams_str = row["teams"]
                target_line = (row.get("target_line") or "").strip()
                sn = row.get("sheet_name", sheets_logger.FULLTIME_SHEET)
                result = fetch_fixture_result(fid)
                if result is None or result.get("status") != "FT":
                    continue
                use_ht = (sn == sheets_logger.HALFTIME_SHEET)
                if use_ht and result.get("home_ht") is not None and result.get("away_ht") is not None:
                    h, a = result["home_ht"], result["away_ht"]
                    final_score_str = f"{h}-{a} (HT)"
                else:
                    h, a = result["home_goals"], result["away_goals"]
                    final_score_str = f"{h}-{a}"
                total_goals = h + a
                threshold = _loss_threshold_from_target_line(target_line)
                if total_goals >= threshold:
                    outcome = "LOSS"
                    losses += 1
                    score_at_70 = row.get("score_at_70", "? - ?")
                    events_raw = fetch_fixture_events(fid)
                    after_70 = events_after_70(events_raw)
                    event_list_str = "; ".join(after_70) if after_70 else "None"
                    gemini = notifier.ask_gemini_loss(
                        result["home_name"],
                        result["away_name"],
                        score_at_70,
                        final_score_str,
                        event_list_str,
                    )
                    loss_details.append((teams_str, gemini))
                else:
                    outcome = "WIN"
                    wins += 1
                    gemini = ""
                updates.append({
                    "row_index": row["row_index"],
                    "outcome": outcome,
                    "gemini_analysis": gemini,
                    "gemini_label": "",
                    "final_score": final_score_str,
                    "sheet_name": sn,
                })
                continue

            # Parlay: all legs must win
            parlay_loss = False
            loss_fixture_result = None
            loss_fixture_row = None
            per_row_final_score: dict[int, str] = {}
            for row in group:
                target_line = (row.get("target_line") or "").strip()
                sn = row.get("sheet_name", sheets_logger.FULLTIME_SHEET)
                result = fetch_fixture_result(row["fixture_id"])
                if result is None or result.get("status") != "FT":
                    continue
                use_ht = (sn == sheets_logger.HALFTIME_SHEET)
                if use_ht and result.get("home_ht") is not None and result.get("away_ht") is not None:
                    h, a = result["home_ht"], result["away_ht"]
                    per_row_final_score[row["row_index"]] = f"{h}-{a} (HT)"
                else:
                    h, a = result["home_goals"], result["away_goals"]
                    per_row_final_score[row["row_index"]] = f"{h}-{a}"
                total_goals = h + a
                threshold = _loss_threshold_from_target_line(target_line)
                if total_goals >= threshold:
                    parlay_loss = True
                    loss_fixture_result = result
                    loss_fixture_row = row
                    break
            if parlay_loss:
                outcome = "LOSS"
                losses += 1
                if loss_fixture_result and loss_fixture_row:
                    events_raw = fetch_fixture_events(loss_fixture_row["fixture_id"])
                    after_70 = events_after_70(events_raw)
                    event_list_str = "; ".join(after_70) if after_70 else "None"
                    lr_sn = loss_fixture_row.get("sheet_name", sheets_logger.FULLTIME_SHEET)
                    lr_use_ht = (lr_sn == sheets_logger.HALFTIME_SHEET)
                    if lr_use_ht and loss_fixture_result.get("home_ht") is not None and loss_fixture_result.get("away_ht") is not None:
                        fs = f"{loss_fixture_result['home_ht']}-{loss_fixture_result['away_ht']} (HT)"
                    else:
                        fs = f"{loss_fixture_result['home_goals']}-{loss_fixture_result['away_goals']}"
                    gemini = notifier.ask_gemini_loss(
                        loss_fixture_result["home_name"],
                        loss_fixture_result["away_name"],
                        loss_fixture_row.get("score_at_70", "? - ?"),
                        fs,
                        event_list_str,
                    )
                    loss_details.append((f"Parlay ({len(group)} legs)", gemini))
                else:
                    gemini = ""
            else:
                outcome = "WIN"
                wins += 1
                gemini = ""
            for i, row in enumerate(group):
                updates.append({
                    "row_index": row["row_index"],
                    "outcome": outcome,
                    "gemini_analysis": gemini if i == 0 else "",
                    "gemini_label": "",
                    "final_score": per_row_final_score.get(row["row_index"], ""),
                    "sheet_name": row.get("sheet_name", sheets_logger.FULLTIME_SHEET),
                })

        try:
            sheets_logger.update_nightly_results(updates)
        except Exception as e:
            logger.error("Nightly analysis: update_nightly_results failed: %s", e)

    # Backfill Gemini analysis for all today's losses that still lack an explanation
    try:
        today_rows = sheets_logger.get_todays_rows()
    except Exception as e:
        logger.error("Nightly analysis: get_todays_rows failed: %s", e)
        today_rows = []

    if today_rows:
        backfill_updates: list[dict] = []
        for r in today_rows:
            status = (r.get("Status") or "").strip().upper()
            result_str = (r.get("Result") or "").strip().upper()
            existing_analysis = (r.get("Gemini_Analysis") or "").strip()
            if status != "FINISHED" or result_str != "LOSS" or existing_analysis:
                continue
            fid_s = (r.get("Fixtures_ID") or "").strip()
            if not fid_s:
                continue
            try:
                fid = int(fid_s)
            except ValueError:
                continue
            try:
                fixture_result = fetch_fixture_result(fid)
            except Exception as e:
                logger.error("Nightly analysis: fetch_fixture_result backfill failed for %s: %s", fid_s, e)
                continue
            if not fixture_result or fixture_result.get("status") != "FT":
                continue
            sheet_name = r.get("sheet_name", sheets_logger.FULLTIME_SHEET)
            use_ht = (sheet_name == sheets_logger.HALFTIME_SHEET)
            if use_ht and fixture_result.get("home_ht") is not None and fixture_result.get("away_ht") is not None:
                fs = f"{fixture_result['home_ht']}-{fixture_result['away_ht']} (HT)"
            else:
                fs = f"{fixture_result['home_goals']}-{fixture_result['away_goals']}"
            score_at_70 = (r.get("Score") or "").strip() or "? - ?"
            try:
                events_raw = fetch_fixture_events(fid)
                after_70 = events_after_70(events_raw)
                event_list_str = "; ".join(after_70) if after_70 else "None"
            except Exception as e:
                logger.error("Nightly analysis: fetch_fixture_events backfill failed for %s: %s", fid_s, e)
                event_list_str = "None"
            try:
                gemini_reason = notifier.ask_gemini_loss(
                    fixture_result["home_name"],
                    fixture_result["away_name"],
                    score_at_70,
                    fs,
                    event_list_str,
                )
            except Exception as e:
                logger.error("Nightly analysis: ask_gemini_loss backfill failed for %s: %s", fid_s, e)
                continue
            backfill_updates.append({
                "row_index": r.get("row_index"),
                "outcome": result_str,
                "gemini_analysis": gemini_reason or "",
                "gemini_label": (r.get("Gemini_Label") or "").strip(),
                "final_score": fs,
                "sheet_name": sheet_name,
            })
        if backfill_updates:
            try:
                sheets_logger.update_nightly_results(backfill_updates)
            except Exception as e:
                logger.error("Nightly analysis: update_nightly_results backfill failed: %s", e)

    # Net profit = current balance - day start balance
    day_profit = balance_dollars - _balance_at_day_start
    total_profit += day_profit
    _save_total_profit(total_profit)
    _last_nightly_profit = day_profit
    _last_nightly_date = get_thorold_now().date()
    notifier.send_nightly_summary(wins, losses, loss_details, _balance_at_day_start, balance_dollars)
    notifier.send_daily_summary(total_profit, _last_nightly_date.isoformat())
    _last_daily_summary_date = _last_nightly_date

    # Personality reflection: extract observations from today's chat before it's cleared
    chat_history = _load_chat_session()
    if chat_history:
        personality = _memory_data.get("personality", {})
        new_obs = notifier.reflect_on_personality(chat_history, personality)
        if new_obs:
            if "personality" not in _memory_data:
                _memory_data["personality"] = {"observations": [], "voice_notes": [], "hamilton_preferences": {}}
            _memory_data["personality"]["observations"].extend(new_obs)
            _save_memory()
            logger.info("Personality reflection: added %d observations", len(new_obs))

    _run_self_optimization(loss_details)
    _archive_rejections_and_clear()


def _run_self_optimization(loss_details: list[tuple[str, str]]) -> None:
    """Build rejections/loss summary, ask Gemini for suggestion, append to optimization_log, send to Telegram with Confirm button."""
    rejections_text = ""
    if recent_rejections:
        rejections_text = "\n".join(
            f"{r[1]} @ {r[2]}' — {r[3]}" for r in recent_rejections[-30:] if len(r) >= 4
        )
    loss_summary = "\n".join(f"{m}: {r}" for m, r in loss_details) if loss_details else "None."
    suggestion = notifier.ask_gemini_optimization(rejections_text, loss_summary)
    _append_optimization_log(suggestion)
    notifier.send_optimization_suggestion(suggestion)


def _archive_rejections_and_clear() -> None:
    """Create archives/ if needed, copy rejections.csv to archives/rejections_[DATE].csv, truncate rejections, clear recent_rejections."""
    global recent_rejections
    try:
        os.makedirs(ARCHIVES_DIR, exist_ok=True)
    except OSError:
        return
    if not os.path.isfile(REJECTIONS_CSV):
        recent_rejections = []
        return
    dt = get_thorold_now().date()
    date_str = f"{dt.year}_{dt.month:02d}_{dt.day:02d}"
    archive_path = os.path.join(ARCHIVES_DIR, f"rejections_{date_str}.csv")
    try:
        shutil.copy2(REJECTIONS_CSV, archive_path)
    except OSError:
        return
    try:
        with open(REJECTIONS_CSV, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["timestamp", "match_name", "minute", "reason", "total_shots", "total_corners"])
    except OSError:
        return
    recent_rejections = []


def wipe_bet_history_and_rejections() -> None:
    """Go live: start with a clean slate for bet_history and rejections."""
    with open(BET_HISTORY_JSON, "w", encoding="utf-8") as f:
        json.dump([], f)
    with open(REJECTIONS_CSV, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "match_name", "minute", "reason", "total_shots", "total_corners"])
    recent_rejections.clear()
    print("[Snappi] Wiped bet_history.json and rejections.csv for clean slate.")


def _html_escape(t: str) -> str:
    """Escape for Telegram HTML."""
    if not t:
        return ""
    return str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _do_restart() -> str | None:
    """
    Restart Snappi: try systemctl (user then sudo), else schedule self-kill so systemd restarts.
    Returns a short message for the chat tool, or None when called from reconnect logic.
    """
    import subprocess as _sp
    try:
        r = _sp.run(
            ["systemctl", "--user", "restart", "snappi"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if r.returncode == 0:
            return "Restarting Snappi via systemd. I'll be back in a few seconds."
        r2 = _sp.run(
            ["sudo", "systemctl", "restart", "snappi"],
            capture_output=True,
            text=True,
            timeout=8,
        )
        if r2.returncode == 0:
            return "Restarting Snappi via systemd. I'll be back in a few seconds."
    except (FileNotFoundError, _sp.TimeoutExpired, PermissionError):
        pass
    except Exception as e:
        logger.warning("_do_restart systemctl failed: %s", e)
    try:
        _sp.Popen(
            ["bash", "-c", f"sleep 3 && kill {os.getpid()}"],
            start_new_session=True,
        )
        return "Snappi will restart in 3 seconds. Systemd will bring me back up."
    except Exception as e:
        return f"Could not restart: {e!s}. Run: sudo systemctl restart snappi"


def _execute_tool(name: str, args: dict) -> str:
    """Execute a Gemini function call tool. Returns result string."""
    import subprocess as _sp

    global is_paused, balance_dollars, _memory_data
    global MAX_SHOTS_30, SHOTS_70_RED, CORNERS_70_RED, FOULS_70_HIGH, TARGET_ODDS, MAX_QUEUE_SIZE, POLL_INTERVAL_SECONDS

    logger.info("Tool call: %s(%s)", name, json.dumps(args, default=str)[:200])

    if name == "shell_exec":
        command = args.get("command", "")
        cwd = args.get("cwd", None) or None
        timeout = int(args.get("timeout", 120))
        if not command:
            return "Error: no command provided"
        try:
            result = _sp.run(
                command, shell=True, capture_output=True, text=True,
                timeout=timeout, cwd=cwd,
            )
            output = ""
            if result.stdout:
                output += result.stdout
            if result.stderr:
                output += ("\n" if output else "") + result.stderr
            if result.returncode != 0:
                output += f"\n[exit code: {result.returncode}]"
            return output[:8000] if output else "(no output)"
        except _sp.TimeoutExpired:
            return f"Error: command timed out after {timeout} seconds"
        except Exception as e:
            return f"Error: {e}"

    elif name == "read_file":
        path = args.get("path", "")
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            return content[:8000] if content else "(empty file)"
        except Exception as e:
            return f"Error reading {path}: {e}"

    elif name == "write_file":
        path = args.get("path", "")
        content = args.get("content", "")
        try:
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            return f"Written {len(content)} bytes to {path}"
        except Exception as e:
            return f"Error writing {path}: {e}"

    elif name == "edit_file":
        path = args.get("path", "")
        old_text = args.get("old_text", "")
        new_text = args.get("new_text", "")
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            if old_text not in content:
                return f"Error: old_text not found in {path}"
            content = content.replace(old_text, new_text, 1)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            return f"Edited {path} successfully"
        except Exception as e:
            return f"Error editing {path}: {e}"

    elif name == "list_files":
        directory = args.get("directory", "")
        try:
            entries = sorted(os.listdir(directory))
            lines = []
            for entry in entries:
                full = os.path.join(directory, entry)
                if os.path.isdir(full):
                    lines.append(f"{entry}/")
                else:
                    size = os.path.getsize(full)
                    lines.append(f"{entry}  ({size} bytes)")
            return "\n".join(lines) if lines else "(empty directory)"
        except Exception as e:
            return f"Error listing {directory}: {e}"

    elif name == "restart_snappi":
        logger.info("Restart requested via chat")
        return _do_restart() or "Restart requested."

    elif name == "add_snap_recipient":
        chat_id = args.get("chat_id")
        if chat_id is None:
            return "Error: chat_id required (Telegram chat ID number)."
        try:
            cid = int(chat_id)
        except (ValueError, TypeError):
            return "Error: chat_id must be a number."
        ids_ = get_snap_recipient_ids()
        if cid in ids_:
            return f"{cid} is already a snap recipient."
        primary = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()
        try:
            primary_int = int(primary) if primary else None
        except ValueError:
            primary_int = None
        extras = [x for x in ids_ if primary_int is None or x != primary_int]
        extras.append(cid)
        _save_snap_recipient_extras(extras)
        return f"Added {cid} to snap recipients. They will receive new snaps and Sentry verdicts."

    elif name == "list_snap_recipients":
        ids_ = get_snap_recipient_ids()
        primary = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()
        try:
            primary_int = int(primary) if primary else None
        except ValueError:
            primary_int = None
        lines = []
        if primary_int is not None:
            lines.append(f"Primary: {primary_int}")
        extras = [x for x in ids_ if primary_int is None or x != primary_int]
        if extras:
            lines.append("Extra: " + ", ".join(str(x) for x in extras))
        return "\n".join(lines) if lines else "No recipients configured."

    elif name == "pause_hunting":
        is_paused = True
        return "Paused live monitoring."

    elif name == "resume_hunting":
        is_paused = False
        return "Resumed live monitoring."

    elif name == "update_balance":
        try:
            amount = float(args.get("amount", 0))
            if amount < 0:
                amount = 0.0
            balance_dollars = amount
            _balance_at_day_start = amount
            _save_balance()
            _save_day_start_balance()
            unit = current_unit_dollars()
            return f"Balance set to ${balance_dollars:.2f} (day start recorded). 1 unit = ${unit:.2f}."
        except (ValueError, TypeError):
            return "Error: invalid amount"

    elif name == "set_param":
        param = (args.get("param") or "").strip()
        if param not in TUNABLE_PARAMS:
            return f"Unknown parameter: {param}. Valid: {', '.join(TUNABLE_PARAMS.keys())}"
        try:
            new_val = args.get("value")
            if param in ("TARGET_ODDS",):
                new_val = float(new_val)
            else:
                new_val = int(new_val)
            globals()[param] = new_val
            _append_optimization_log(f"Chat: {param} changed to {new_val}")
            return f"{param} set to {new_val}."
        except (ValueError, TypeError):
            return f"Error: invalid value for {param}"

    elif name == "save_memory":
        key = (args.get("key") or "").strip()
        value = args.get("value", "")
        if not key:
            return "Error: no key provided"
        parts = key.split(".")
        if len(parts) == 1:
            if parts[0] in ("notes", "lessons"):
                if not isinstance(_memory_data.get(parts[0]), list):
                    _memory_data[parts[0]] = []
                _memory_data[parts[0]].append(value)
            else:
                _memory_data[parts[0]] = value
        elif len(parts) == 2:
            if parts[0] not in _memory_data or not isinstance(_memory_data[parts[0]], dict):
                _memory_data[parts[0]] = {}
            _memory_data[parts[0]][parts[1]] = value
        _save_memory()
        return f"Saved to memory: {key} = {value}"

    elif name == "get_todays_snaps":
        try:
            rows = sheets_logger.get_todays_rows()
            if not rows:
                return "No snaps logged today yet."
            lines = []
            for r in rows:
                match = r.get("Match", "?")
                window = r.get("Window", "?")
                status = r.get("Status", "?")
                result_str = r.get("Result", "")
                score = r.get("Score", "?")
                final = r.get("Final Score", "")
                sentry = r.get("Sentry_Colour", "")
                line = f"{match} | {window} | Score: {score}"
                if final:
                    line += f" -> {final}"
                line += f" | {status}"
                if result_str:
                    line += f" ({result_str})"
                if sentry:
                    line += f" [{sentry}]"
                lines.append(line)
            return f"Today's snaps ({len(rows)} total):\n" + "\n".join(lines)
        except Exception as e:
            return f"Error fetching today's snaps: {e}"

    elif name == "picoclaw":
        task = args.get("task", "")
        if not task:
            return "Error: no task provided"
        try:
            result = _sp.run(
                ["picoclaw", "agent", "-m", task],
                capture_output=True, text=True, timeout=120,
            )
            output = ""
            if result.stdout:
                output += result.stdout
            if result.stderr:
                output += ("\n" if output else "") + result.stderr
            return output[:4000] if output else "(no output from PicoClaw)"
        except _sp.TimeoutExpired:
            return "PicoClaw task timed out after 120 seconds"
        except FileNotFoundError:
            return "Error: picoclaw binary not found at /usr/local/bin/picoclaw"
        except Exception as e:
            return f"PicoClaw error: {e}"

    return f"Unknown tool: {name}"


def run_telegram_listener() -> None:
    """
    Run Telegram bot command handlers in a separate thread so infinity_polling does not block the 180s loop.
    Supports: /status, /schedule, /daily, /pending, /accept, /rejections, /heartbeat, /livecheck, /pause, /resume, /logs, /clearlogs. All replies use parse_mode='HTML'.
    """
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        return
    bot = telebot.TeleBot(token)

    @bot.message_handler(commands=["status"])
    def cmd_status(message: telebot.types.Message) -> None:
        global last_heartbeat_time
        last_heartbeat_time = get_thorold_now()
        status_word = "Paused" if is_paused else "Active"
        hb = last_heartbeat_time.strftime("%Y-%m-%d %H:%M:%S") if last_heartbeat_time else "—"
        unit = current_unit_dollars()
        text = (
            f"💰 Total Profit: ${total_profit:.2f}\n"
            f"💵 Balance: ${balance_dollars:.2f} (1 unit = ${unit:.2f})\n"
            f"📡 Status: {status_word}\n"
            f"📊 Usage: {api_calls_today}/7500\n"
            f"🕒 Last Heartbeat: {_html_escape(hb)}"
        )
        schedule = get_todays_fixtures_schedule()
        if schedule:
            total = schedule.get("total", 0)
            by_hour = schedule.get("by_hour") or {}
            peak = sorted(by_hour.items(), key=lambda x: -x[1])[:3]
            peak_str = ", ".join(f"{h}:00 ({c})" for h, c in peak) if peak else "—"
            text += f"\n\n📅 <b>Today:</b> {total} fixtures total"
            text += f"\n📈 Peak times: {peak_str}"
            text += f"\n🔴 Live now: {_last_live_fixtures_count} (from last poll)"
        if api_429_seen:
            text += "\n\n⚠️ Daily request limit reached. No live data until quota resets (see dashboard.api-football.com)."
        bot.reply_to(message, text, parse_mode="HTML")

    @bot.message_handler(commands=["schedule"])
    def cmd_schedule(message: telebot.types.Message) -> None:
        """Show today's fixtures by hour so you can plan when to be active."""
        schedule = get_todays_fixtures_schedule()
        if not schedule:
            bot.reply_to(
                message,
                "Couldn't load today's schedule (API or cache). Try again in a few minutes.",
                parse_mode="HTML",
            )
            return
        total = schedule.get("total", 0)
        by_hour = schedule.get("by_hour") or {}
        if not by_hour:
            bot.reply_to(message, f"📅 Today: {total} fixtures (no kickoff times in data).", parse_mode="HTML")
            return
        lines = [f"📅 <b>Today: {total} fixtures</b>", ""]
        for h in sorted(by_hour.keys()):
            c = by_hour[h]
            lines.append(f"  {h:02d}:00 — {c} match{'es' if c != 1 else ''}")
        lines.append("")
        lines.append(f"🔴 Live now: {_last_live_fixtures_count} (from last poll)")
        bot.reply_to(message, "\n".join(lines), parse_mode="HTML")

    @bot.message_handler(commands=["daily"])
    def cmd_daily(message: telebot.types.Message) -> None:
        """On-demand daily-so-far report: today's snaps summarized by Gemini."""
        try:
            rows = sheets_logger.get_todays_rows()
        except Exception as e:
            bot.reply_to(message, f"Could not read sheets: {_html_escape(str(e))}", parse_mode="HTML")
            return
        rows_text = _build_snap_rows_text(rows)
        if not rows_text.strip():
            bot.reply_to(message, "📋 <b>Daily so far</b>: No snaps today yet.", parse_mode="HTML")
            return
        bot.reply_to(message, "⏳ Building daily report…", parse_mode="HTML")
        try:
            breakdown = notifier.ask_gemini_daily_breakdown(rows_text)
        except Exception as e:
            bot.reply_to(message, f"Daily report failed: {_html_escape(str(e))}", parse_mode="HTML")
            return
        msg = f"📋 <b>Daily so far</b>\n\n{_html_escape(breakdown)}"
        if len(msg) > 4000:
            msg = msg[:3970] + "\n...(truncated)"
        bot.reply_to(message, msg, parse_mode="HTML")

    @bot.message_handler(commands=["pause"])
    def cmd_pause(message: telebot.types.Message) -> None:
        global is_paused
        is_paused = True
        bot.reply_to(
            message,
            "⏸️ <b>Snappi Paused.</b> Live monitoring has stopped to save API credits.",
            parse_mode="HTML",
        )

    @bot.message_handler(commands=["resume"])
    def cmd_resume(message: telebot.types.Message) -> None:
        global is_paused
        is_paused = False
        bot.reply_to(
            message,
            "▶️ <b>Snappi Resumed.</b> Hunting for low-pressure matches...",
            parse_mode="HTML",
        )

    @bot.message_handler(commands=["updatebalance"])
    def cmd_updatebalance(message: telebot.types.Message) -> None:
        """Set betting balance (e.g. /updatebalance 100). Also records as day-start balance for net profit tracking."""
        global balance_dollars, _balance_at_day_start
        parts = (message.text or "").strip().split()
        if len(parts) < 2:
            bot.reply_to(
                message,
                "Usage: <code>/updatebalance 100</code> (set your current betting balance so Snappi can suggest stakes).",
                parse_mode="HTML",
            )
            return
        try:
            balance_dollars = float(parts[1])
            if balance_dollars < 0:
                balance_dollars = 0.0
            _balance_at_day_start = balance_dollars
            _save_balance()
            _save_day_start_balance()
            unit = current_unit_dollars()
            bot.reply_to(
                message,
                f"✅ Balance set to ${balance_dollars:.2f} (day start recorded).\n"
                f"1 unit = ${unit:.2f} (🔴0.5u 🟡2u 🟢3u; same color = parlay).",
                parse_mode="HTML",
            )
        except ValueError:
            bot.reply_to(message, "❌ Use a number, e.g. <code>/updatebalance 100</code>", parse_mode="HTML")

    @bot.message_handler(commands=["accept", "pending"])
    def cmd_accept_pending_legacy(message: telebot.types.Message) -> None:
        """Legacy commands kept for compatibility; they no longer affect balance or stakes."""
        bot.reply_to(
            message,
            "The /accept and /pending flow is now legacy only — Snappi no longer waits for manual acceptance or deducts stakes. "
            "Use Sentry's colours and stake line as guidance, and future automation (e.g. Pinnacle) will handle execution.",
            parse_mode="HTML",
        )

    @bot.callback_query_handler(func=lambda c: c.data == "confirm_optimization")
    def cb_confirm_optimization(callback: telebot.types.CallbackQuery) -> None:
        bot.answer_callback_query(callback.id)
        bot.send_message(
            callback.message.chat.id,
            "✅ Update logged. Check optimization_log.txt; apply changes manually if desired.",
            parse_mode="HTML",
        )

    @bot.message_handler(commands=["rejections"])
    def cmd_rejections(message: telebot.types.Message) -> None:
        # Use in-memory recent_rejections (last 20 after reboot) so /rejections reflects the day
        data = recent_rejections if recent_rejections else []
        try:
            if not data and os.path.isfile(REJECTIONS_CSV):
                with open(REJECTIONS_CSV, "r", encoding="utf-8", newline="") as f:
                    rows = list(csv.reader(f))
                data = rows[1:] if len(rows) > 1 else []
        except OSError:
            pass
        if not data:
            bot.reply_to(message, "<i>No rejections yet.</i>", parse_mode="HTML")
            return
        last5 = data[-5:] if len(data) >= 5 else data
        lines = ["<b>Last 5 rejections:</b>"]
        for r in last5:
            if len(r) >= 4:
                lines.append(_html_escape(f"{r[1]} @ {r[2]}' — {r[3]}"))
            else:
                lines.append(_html_escape(", ".join(r)))
        bot.reply_to(message, "\n".join(lines), parse_mode="HTML")

    @bot.message_handler(commands=["heartbeat"])
    def cmd_heartbeat(message: telebot.types.Message) -> None:
        now_str = get_thorold_now().strftime("%Y-%m-%d %H:%M:%S %Z")
        text = f"Snappi is active.\n{_html_escape(now_str)}"
        bot.reply_to(message, text, parse_mode="HTML")

    @bot.message_handler(commands=["livecheck"])
    def cmd_livecheck(message: telebot.types.Message) -> None:
        """One-off request: show Thorold time, Hunter phase, pause state, and live fixture count from API."""
        try:
            now_t = get_thorold_now()
            hunter = is_hunter_phase()
            matches = fetch_live_fixtures()
            n = len(matches)
            leagues = set()
            for m in matches:
                league_obj = m.get("league") or {}
                name = league_obj.get("name") or league_obj.get("country") or "?"
                if name and name != "?":
                    leagues.add(str(name))
            league_list = ", ".join(sorted(leagues)[:15]) if leagues else "—"
            if n > 15:
                league_list += " …"
            msg = (
                f"🕒 <b>Thorold:</b> {now_t.strftime('%Y-%m-%d %H:%M %Z')}\n"
                f"📡 <b>Hunter phase:</b> {hunter} (active 05:00–00:00)\n"
                f"⏸️ <b>Paused:</b> {is_paused}\n"
                f"<b>Live fixtures (API):</b> {n}\n"
                f"<b>Leagues:</b> {_html_escape(league_list)}\n\n"
            )
            if n == 0:
                msg += (
                    "API returned 0 live matches. "
                    "Check: 1) API key is valid and has <i>live</i> access (dashboard.api-football.com), "
                    "2) Your plan includes live fixtures, 3) Leagues are in their live feed.\n"
                )
                if last_fixtures_zero_reason:
                    msg += f"<i>Last API hint:</i> {_html_escape(last_fixtures_zero_reason[:200])}"
            else:
                msg += "Snappi scans these for 30'/70' windows."
            bot.reply_to(message, msg, parse_mode="HTML")
        except Exception as e:
            bot.reply_to(message, f"<i>Livecheck failed: {_html_escape(str(e))}</i>", parse_mode="HTML")

    @bot.message_handler(commands=["logs"])
    def cmd_logs(message: telebot.types.Message) -> None:
        if not os.path.isfile(LOG_FILE):
            bot.reply_to(message, "No logs found yet. Snappi is fresh!", parse_mode="HTML")
            return
        try:
            with open(LOG_FILE, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except OSError:
            bot.reply_to(message, "<i>Could not read log file.</i>", parse_mode="HTML")
            return
        last_lines = lines[-20:] if len(lines) >= 20 else lines
        content = "".join(last_lines).strip()
        if not content:
            bot.reply_to(message, "No logs found yet. Snappi is fresh!", parse_mode="HTML")
            return
        # Telegram limit 4096; keep under and escape for HTML
        max_len = 4000
        if len(content) > max_len:
            content = content[-max_len:] + "\n...(truncated)"
        pre_content = _html_escape(content)
        text = f"<pre>{pre_content}</pre>"
        bot.reply_to(message, text, parse_mode="HTML")

    @bot.message_handler(commands=["clearlogs"])
    def cmd_clearlogs(message: telebot.types.Message) -> None:
        try:
            with open(LOG_FILE, "w", encoding="utf-8") as f:
                f.write("")
        except OSError:
            bot.reply_to(message, "<i>Could not clear log file.</i>", parse_mode="HTML")
            return
        bot.reply_to(message, "🗑 Logs cleared. <i>snappi.log</i> emptied.", parse_mode="HTML")

    @bot.message_handler(func=lambda m: m.text and not (m.text or "").startswith("/"))
    def handle_natural_language(message: telebot.types.Message) -> None:
        """Route any non-command text through Gemini with full context, tool use, and conversation history."""
        user_text = (message.text or "").strip()
        if not user_text:
            return
        try:
            context = _build_chat_context()
            soul = _soul_text or _load_soul()
            chat_history = _load_chat_session()
            now_ts = datetime.now(THOROLD_TZ).isoformat()
            result = notifier.chat_with_gemini(
                user_text, soul, context,
                tool_executor=_execute_tool,
                chat_history=chat_history,
                user_ts=now_ts,
                max_turns=CHAT_HISTORY_MAX_TURNS,
            )
            reply_text = result.get("reply", "...")
            tool_log = result.get("tool_log") or []
            updated_history = result.get("history") or chat_history
            _save_chat_session(updated_history)
            if tool_log:
                actions_str = "\n".join(f"🔧 {t}" for t in tool_log)
                reply_text += f"\n\n{actions_str}"
            if len(reply_text) > 4000:
                reply_text = reply_text[:4000] + "…"
            bot.reply_to(message, _html_escape(reply_text), parse_mode="HTML")
        except Exception as e:
            bot.reply_to(message, f"Something broke: {_html_escape(str(e)[:200])}", parse_mode="HTML")

    # Robust retry loop for connection drops (e.g. Windows 10054, network flickers)
    backoff = 5
    while True:
        try:
            bot.infinity_polling(timeout=20, long_polling_timeout=10)
        except (requests.exceptions.ConnectionError, OSError) as e:
            backoff = min(backoff * 2, 60)
            print(f"[Snappi] Telegram polling connection error, retry in {backoff}s: {e}")
            time.sleep(backoff)
            continue
        except Exception as e:
            backoff = min(backoff * 2, 60)
            print(f"[Snappi] Telegram polling error, retry in {backoff}s: {e}")
            time.sleep(backoff)
            continue
        break


def _one_time_system_reset() -> None:
    """Once per upgrade: clear snappi.log and rejections.csv (fresh start)."""
    if os.path.isfile(SENTINEL_SYSTEM_RESET):
        return
    try:
        with open(LOG_FILE, "w", encoding="utf-8") as f:
            f.write("")
        with open(REJECTIONS_CSV, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["timestamp", "match_name", "minute", "reason", "total_shots", "total_corners"])
        Path(SENTINEL_SYSTEM_RESET).touch()
    except OSError:
        pass


def run() -> None:
    """
    V3 Dual-phase loop (Thorold):
      Phase 1 The Hunter (05:00–00:00): Poll live every 60s; at 05:00 reset daily_calls, sent_alerts, logged_rejections.
      Phase 2 The Analyst (00:01–04:59): At 00:05 nightly_analysis() (outcomes, profit/loss, self-optimization); at 00:30 send_daily_summary().
    """
    global is_paused, allow_auto_resume_next_hunter, _last_usage_logged_date, _last_cleared_usage_date
    global sent_alerts, logged_rejections, last_heartbeat_time, recent_rejections, rejections_count, total_profit
    global _last_analyst_alert_date, _last_daily_summary_date, _balance_at_day_start, _todays_fixtures_schedule
    # Immediate cleanup: clear snappi.log and rejections.csv (starting fresh)
    try:
        with open(LOG_FILE, "w", encoding="utf-8") as f:
            f.write("")
        with open(REJECTIONS_CSV, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["timestamp", "match_name", "minute", "reason", "total_shots", "total_corners"])
    except OSError:
        pass
    recent_rejections.clear()
    rejections_count = 0
    _one_time_system_reset()
    _load_api_calls_state()  # restore API call count (resets at 05:00 Thorold)
    _load_pending_snaps()    # load pending snaps (cleared if from previous usage day)
    init_persistent_data()   # load last 20 rejections (after clear, may be empty)
    total_profit = _load_total_profit()
    _load_balance()
    _load_soul()
    _load_memory()
    _load_day_start_balance()
    # Boot notification: Snappi-generated greeting so it's not always the same
    try:
        greeting = notifier.get_boot_greeting()
        if not greeting:
            greeting = "Snappi is online."
        notifier.send_simple_message("🟢 " + _html_escape(greeting))
    except Exception:
        pass
    t = threading.Thread(target=run_telegram_listener, daemon=True)
    t.start()
    now_t = get_thorold_now()
    hunter = is_hunter_phase()
    print(
        f"[Snappi] V3 Started. Thorold now: {now_t.strftime('%Y-%m-%d %H:%M %Z')} | "
        f"Hunter phase: {hunter} (05:00–00:00). Poll every {POLL_INTERVAL_SECONDS}s."
    )
    last_summary_at = time.time()
    last_analysis_date: datetime | None = None
    last_heartbeat_at: float | None = None

    while True:
        now_t = get_thorold_now()

        if is_hunter_phase():
            # Nightly 5 AM reset: clear dedupe sets so we're ready for the new day's matches
            usage_date = _get_usage_date()
            if _last_cleared_usage_date != usage_date:
                sent_alerts.clear()
                logged_rejections.clear()
                pending_snaps.clear()
                _save_pending_snaps()
                _odds_cache.clear()
                _todays_fixtures_schedule = None
                _last_cleared_usage_date = usage_date
                _clear_chat_session()
                with open(BET_HISTORY_JSON, "w", encoding="utf-8") as f:
                    json.dump([], f)
                _balance_at_day_start = balance_dollars
                _save_day_start_balance()
            # Auto-resume once at the start of the 05:00 Hunter session
            if now_t.hour == HUNTER_START_HOUR and is_paused and allow_auto_resume_next_hunter:
                is_paused = False
                allow_auto_resume_next_hunter = False
                notifier.send_simple_message(
                    "🌅 <b>Good Morning!</b> Snappi has auto-resumed for the new hunting session."
                )
            # Sunday 8 AM — weekly Gemini breakdown (once per Sunday)
            if (
                now_t.weekday() == 6
                and now_t.hour == WEEKLY_REPORT_HOUR
                and now_t.minute < 5
                and _last_weekly_report_date != now_t.date()
            ):
                try:
                    run_weekly_report()
                except Exception as e:
                    logger.error("Weekly report failed: %s", e)

            if is_paused:
                print("Bot is PAUSED. Skipping poll...")
                time.sleep(POLL_INTERVAL_SECONDS)
                continue
            try:
                process_live_matches()
                now_ts = time.time()
                if (now_ts - last_summary_at) >= SESSION_SUMMARY_INTERVAL:
                    queue_total = len(flagged_30) + len(flagged_70)
                    print(
                        f"[Snappi] Session Summary: Current Queue: {queue_total} | "
                        f"Matches Rejected This Session: {rejections_count}"
                    )
                    last_summary_at = now_ts
            except ValueError as e:
                print(f"[Snappi] Config error: {e}")
            except Exception as e:
                tb = traceback.format_exc()
                logger.exception("Hunter phase error")
                # Send traceback to Telegram so you know immediately if API broke
                msg = f"⚠️ <b>Snappi Hunter Error</b>\n\n<pre>{_html_escape(tb[-4000:] if len(tb) > 4000 else tb)}</pre>"
                notifier.send_simple_message(msg)
                print(f"[Snappi] Unexpected error: {e}")
            time.sleep(POLL_INTERVAL_SECONDS)
            continue

        # Phase 2: The Analyst — allow auto-resume again next time Hunter starts at 05:00
        allow_auto_resume_next_hunter = True

        # 00:01 — alert when entering Analyst mode (once per night)
        if (
            now_t.hour == 0
            and now_t.minute >= 1
            and (_last_analyst_alert_date is None or _last_analyst_alert_date != now_t.date())
        ):
            try:
                notifier.send_simple_message(
                    "📊 <b>Snappi entering Analyst mode.</b> Nightly analysis at 00:05; summary at 00:30."
                )
                _last_analyst_alert_date = now_t.date()
            except Exception:
                pass

        # 00:05 — nightly_analysis once per day (set date first so we don't re-run every minute)
        if (
            now_t.hour == ANALYST_NIGHTLY_HOUR
            and now_t.minute >= ANALYST_NIGHTLY_MINUTE
            and (last_analysis_date is None or last_analysis_date != now_t.date())
        ):
            last_analysis_date = now_t.date()
            try:
                if _last_usage_logged_date != _get_usage_date():
                    _last_usage_logged_date = _get_usage_date()
                    sheets_logger.log_daily_usage_to_sheet(_get_usage_date().isoformat(), api_calls_today)
                print("[Snappi] Running nightly analysis (00:05)...")
                nightly_analysis()
            except Exception as e:
                print(f"[Snappi] Nightly analysis failed: {e}")

        # 00:30 — send_daily_summary with finalized financial totals (once per day)
        if (
            now_t.hour == ANALYST_SUMMARY_HOUR
            and now_t.minute >= ANALYST_SUMMARY_MINUTE
            and _last_nightly_date is not None
            and _last_daily_summary_date != now_t.date()
        ):
            try:
                notifier.send_daily_summary(_last_nightly_profit, _last_nightly_date.isoformat())
                _last_daily_summary_date = now_t.date()
            except Exception:
                pass

        # Phase 2: The Analyst — heartbeat every hour; update last_heartbeat_time for /status
        now_ts = time.time()
        if last_heartbeat_at is None or (now_ts - last_heartbeat_at) >= HEARTBEAT_INTERVAL:
            last_heartbeat_time = get_thorold_now()
            print(
                f"Snappi Analyst is idling... System Time: {last_heartbeat_time.strftime('%Y-%m-%d %H:%M:%S')} ({THOROLD_TZ}). "
                "Next hunt starts at 05:00."
            )
            last_heartbeat_at = now_ts
        # Idle until 05:00 (check every 60s)
        time.sleep(60)


if __name__ == "__main__":
    run()
