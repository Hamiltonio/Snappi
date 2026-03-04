"""
Snappi Telegram notifier: scan alerts (Pressure Stats, SOLO/DOUBLE/PARLAY) and nightly summary.
Uses HTML for alerts (reliable with scores like 0-1). Gemini API (from .env) for loss analysis.
"""
import os
import json
import re
from dotenv import load_dotenv, find_dotenv
import telebot
from telebot import types
# Load .env from the same folder as this script first, then find_dotenv() as fallback
_script_dir = os.path.dirname(os.path.abspath(__file__))
_env_next_to_script = os.path.join(_script_dir, ".env")
load_dotenv(_env_next_to_script)
if not os.getenv("TELEGRAM_BOT_TOKEN"):
    _env_path = find_dotenv()
    if _env_path:
        load_dotenv(_env_path)
    else:
        load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or ""
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID") or ""
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

_SOUL_FILE = os.path.join(_script_dir, "soul.md")
_soul_cache: str = ""


def load_soul() -> str:
    """Load Snappi's personality from soul.md (cached after first read)."""
    global _soul_cache
    if _soul_cache:
        return _soul_cache
    try:
        with open(_SOUL_FILE, "r", encoding="utf-8") as f:
            _soul_cache = f.read().strip()
    except OSError:
        _soul_cache = (
            "You are Snappi, a sharp, analytical, and fiercely loyal betting expert. "
            "You trust the data and the process, and you never chase a loss."
        )
    return _soul_cache


def _escape_html(text: str) -> str:
    """Escape & < > for Telegram HTML."""
    if not text:
        return ""
    s = str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return s


def _bold_html(text: str) -> str:
    """Wrap in bold for HTML."""
    return "<b>" + _escape_html(text) + "</b>"


def _alert_label(count: int) -> str:
    """1 = SNAPPİ SOLO, 2 = SNAPPİ DOUBLE, 3-5 = SNAPPİ PARLAY."""
    if count == 1:
        return "🚨 SNAPPİ SOLO"
    if count == 2:
        return "🚨 SNAPPİ DOUBLE"
    return "🚨 SNAPPİ PARLAY"


def get_snappi_analysis(match_data) -> str:
    """
    Use Snappi's soul persona via Gemini to analyse match data.
    Returns a short, data-grounded analysis string (max ~400 chars) or empty string on failure.
    Wrapped in try/except so a Gemini outage never blocks the raw alert.
    """
    try:
        try:
            data_str = json.dumps(match_data, ensure_ascii=False, default=str, indent=2)
        except Exception:
            data_str = str(match_data)

        soul = load_soul()
        alert_instruction = (
            f"{soul}\n\n"
            "You are giving Snappi's quick take on a flagged under spot. Speak in your natural Snappi voice: "
            "concise, direct, and grounded in the data (scoreline, shots, corners, fouls, line, odds, and Forebet "
            "signals when available). Avoid flowery metaphors and over-the-top drama. One or two short sentences, "
            "under 60 words total."
        )
        import google.generativeai as genai
        genai.configure(api_key=GEMINI_API_KEY or "")
        model = genai.GenerativeModel(
            "gemini-2.5-flash",
            system_instruction=alert_instruction,
        )
        response = model.generate_content(
            "Hamilton just flagged this match or set of matches as a potential low-pressure Under opportunity. "
            "Here is the data (score, shots, corners, fouls, line, odds, Forebet summary, league, etc.):\n\n"
            f"{data_str}\n\n"
            "In 1–2 short sentences, explain why this does or does not look like a good Under spot. "
            "Reference specific numbers (e.g. shots, corners, fouls, line, odds, Forebet probabilities). "
            "Stay under 60 words, keep it calm and analytical."
        )
        text = getattr(response, "text", "") or ""
        return text.strip()[:400]
    except Exception:
        return ""


def send_message_to_recipients(message: str, chat_ids: list[str]):
    """Send a message to a list of Telegram chat IDs."""
    if not TELEGRAM_BOT_TOKEN:
        print("TELEGRAM_BOT_TOKEN not set. Cannot send message.")
        return

    bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)
    for chat_id in chat_ids:
        try:
            # Ensure chat_id is a string before sending
            bot.send_message(str(chat_id), message, parse_mode='HTML')
            print(f"Message sent to {chat_id}")
        except telebot.apihelper.ApiTelegramException as e:
            print(f"Error sending message to {chat_id}: {e}")
        except Exception as e:
            print(f"An unexpected error occurred sending to {chat_id}: {e}")

def send_snappi_alert(
    window_name: str,
    matches: list,
    button_rows: list,
    unit_dollars: float = 0.0,
    snap_id: int | None = None,
    recipient_chat_ids: list[int] | None = None,
) -> tuple[int | None, int | None]:
    """
    Send Snappi alert to Telegram. Line = Score + 1.5. No DA. Stake TBD by Sentry.
    snap_id: optional label (e.g. 12) shown as "Snap #12" for /accept reference.
    recipient_chat_ids: if provided, send to all (first is primary for Sentry reply); else use TELEGRAM_CHAT_ID only.
    Returns (message_id, chat_id) for Sentry reply (primary chat), or (None, None).
    """
    if not TELEGRAM_BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN must be set in .env")
    if recipient_chat_ids:
        chat_ids = list(recipient_chat_ids)
    else:
        if not TELEGRAM_CHAT_ID:
            raise ValueError("TELEGRAM_CHAT_ID must be set in .env")
        chat_ids = [int(TELEGRAM_CHAT_ID)]
    if not chat_ids:
        return (None, None)

    bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)
    primary_chat_id = chat_ids[0]

    count = len(matches)
    label = _alert_label(count)
    lines = [
        _bold_html(window_name),
    ]
    if snap_id is not None:
        lines.append(_escape_html(f"Snap #{snap_id}"))
    lines.extend([
        _escape_html(label),
        "",
    ])
    for match in matches:
        if isinstance(match, dict):
            home = match.get("home") or "?"
            away = match.get("away") or "?"
            score = match.get("score", "? - ?")
            total_shots = match.get("total_shots", "?")
            corners = match.get("total_corners", "?")
            fouls = match.get("fouls", "?")
            target_line = match.get("target_line", "?")
            odds_val = match.get("odds")
            league_country = match.get("league_country") or match.get("league") or ""
        else:
            home = away = "?"
            score = "? - ?"
            total_shots = corners = fouls = target_line = "?"
            odds_val = None
            league_country = ""
        line1 = "⚽ " + _bold_html(home) + " vs " + _bold_html(away) + " (" + _escape_html(score) + ")"
        if league_country:
            line1 += "\n   " + _escape_html(league_country)
        line2 = "📊 " + _bold_html("Shots") + ": " + _escape_html(str(total_shots)) + " | " + _bold_html("Corners") + ": " + _escape_html(str(corners)) + " | " + _bold_html("Fouls") + ": " + _escape_html(str(fouls))
        line3 = "🎯 Line: " + _escape_html(str(target_line))
        if odds_val is not None and odds_val > 0:
            line3 += " | " + _bold_html("Odds") + ": " + _escape_html(f"{odds_val:.2f}")
        lines.append(line1)
        lines.append(line2)
        lines.append(line3)
        forebet_summary = match.get("forebet_summary") if isinstance(match, dict) else None
        if forebet_summary:
            lines.append("📈 " + _escape_html(forebet_summary))
        lines.append("")

    lines.append("Stake: TBD by Sentry")

    try:
        analysis = get_snappi_analysis(matches)
        if analysis:
            lines.append("")
            lines.append("🧠 " + _bold_html("Snappi's Take"))
            lines.append(_escape_html(analysis))
    except Exception:
        pass

    text = "\n".join(lines).strip()

    markup = types.InlineKeyboardMarkup()
    for row in button_rows:
        markup.row(*[types.InlineKeyboardButton(text=t, url=u) for t, u in row])

    msg_id = None
    try:
        msg = bot.send_message(
            primary_chat_id,
            text,
            parse_mode="HTML",
            reply_markup=markup,
        )
        msg_id = msg.message_id
    except Exception:
        pass
    for cid in chat_ids[1:]:
        try:
            bot.send_message(cid, text, parse_mode="HTML", reply_markup=markup)
        except Exception:
            pass
    return (msg_id, primary_chat_id)


# V3.5: No stake/odds prompt; stake from balance/units and Sentry label.


def send_simple_message(text: str) -> None:
    """Send a single HTML message to TELEGRAM_CHAT_ID (e.g. auto-resume notification)."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)
    try:
        bot.send_message(TELEGRAM_CHAT_ID, text, parse_mode="HTML")
    except Exception:
        pass


def get_boot_greeting() -> str:
    """
    Ask Gemini (Snappi's soul) for a short wake-up greeting. No tools, one-shot.
    Returns the greeting text (1-2 sentences) or empty string on failure.
    """
    if not GEMINI_API_KEY:
        return ""
    try:
        import google.generativeai as genai
        genai.configure(api_key=GEMINI_API_KEY)
        soul = load_soul()
        model = genai.GenerativeModel(
            "gemini-2.5-flash",
            system_instruction=(
                f"{soul}\n\n"
                "You just booted. Hamilton will see one message from you. "
                "Reply with a single short greeting (1-2 sentences). Vary it: sometimes casual, sometimes focused, "
                "never the same phrase every time. No tools, no timestamps, no balance or numbers — just your voice saying you're back."
            ),
        )
        response = model.generate_content(
            "You just came online. Write one short greeting for Hamilton now."
        )
        text = getattr(response, "text", "") or ""
        return text.strip()[:300] if text else ""
    except Exception:
        return ""


def ask_gemini_loss(
    home_name: str,
    away_name: str,
    score_at_70: str,
    final_score: str,
    event_list: str,
) -> str:
    """
    Ask Gemini to analyze a loss in one sentence. Uses GEMINI_API_KEY from .env.
    score_at_70 = score when flagged at 70'; event_list = goals/red cards after 70'.
    Returns a short reason (e.g. preventable vs 'the game being the game').
    """
    try:
        import google.generativeai as genai
        genai.configure(api_key=GEMINI_API_KEY or "")
        model = genai.GenerativeModel("gemini-2.5-flash")
        prompt = (
            f"Analyze this loss: {home_name} vs {away_name}. Flagged at 70' (Score: {score_at_70}). "
            f"Final: {final_score}. Events after 70': {event_list}. "
            "Based on this, was it a preventable error or 'the game being the game'? One concise sentence."
        )
        response = model.generate_content(prompt)
        if response and response.text:
            return response.text.strip()[:300]
    except Exception as e:
        return f"Analysis unavailable: {e!s}"[:200]
    return "No response from Gemini."


def send_nightly_summary(
    wins: int,
    losses: int,
    loss_details: list[tuple[str, str]],
    day_start_balance: float = 0.0,
    current_balance: float = 0.0,
) -> None:
    """
    Send one Telegram message: nightly summary with wins, losses, net profit, and Gemini's take on each loss.
    Net profit = current_balance - day_start_balance.
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    net_profit = current_balance - day_start_balance
    sign = "+" if net_profit >= 0 else ""
    lines = [
        "📊 <b>NIGHTLY SUMMARY</b>",
        f"✅ Wins: {wins} | ❌ Losses: {losses}",
        f"💵 Start: ${day_start_balance:.2f} → End: ${current_balance:.2f}",
        f"💰 Net: <b>{sign}${net_profit:.2f}</b>",
        "",
        "🤖 Gemini's Take on Losses:",
        "",
    ]
    for match_name, reason in loss_details:
        lines.append(_escape_html(match_name) + ": " + _escape_html(reason))
    if not loss_details:
        lines.append("(No losses today.)")
    text = "\n".join(lines)
    bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)
    try:
        bot.send_message(TELEGRAM_CHAT_ID, text, parse_mode="HTML")
    except Exception:
        pass


def send_daily_summary(total_profit: float, date_str: str) -> None:
    """Send finalized financial totals at 00:30 (Thorold)."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    sign = "+" if total_profit >= 0 else ""
    text = (
        f"📊 <b>Daily Summary</b> ({_escape_html(date_str)})\n\n"
        f"💰 Total Profit: ${sign}{total_profit:.2f}"
    )
    bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)
    try:
        bot.send_message(TELEGRAM_CHAT_ID, text, parse_mode="HTML")
    except Exception:
        pass


def ask_gemini_optimization(rejections_summary: str, loss_summary: str) -> str:
    """
    Gemini analyzes today's rejections and losses; returns one suggestion line in the format:
    "Based on today, I suggest changing [Parameter] to [Value] because [Reason]."
    """
    try:
        import google.generativeai as genai
        genai.configure(api_key=GEMINI_API_KEY or "")
        model = genai.GenerativeModel("gemini-2.5-flash")
        prompt = (
            "You are optimizing a football betting bot (Snappi) that flags low-pressure matches at 30' and 70'.\n"
            "Today's REJECTIONS (matches we scanned but did not bet on):\n"
            f"{rejections_summary or 'None.'}\n\n"
            "Today's LOSSES (we bet under, match went over):\n"
            f"{loss_summary or 'None.'}\n\n"
            "Reply with exactly one line in this format: "
            "Based on today, I suggest changing [Parameter] to [Value] because [Reason]. "
            "Parameter should be something like MAX_SHOTS_30, MAX_DA_30, or similar. Keep it one sentence."
        )
        response = model.generate_content(prompt)
        if response and response.text:
            return response.text.strip()[:400]
    except Exception as e:
        return f"Optimization analysis unavailable: {e!s}"[:200]
    return "No suggestion from Gemini."


def ask_gemini_weekly_breakdown(rows_text: str, start_iso: str, end_iso: str) -> str:
    """
    Gemini analyzes a week of snap rows; returns a short breakdown (by league, Forebet, odds, suggestions).
    rows_text: plain text summary of rows (Match, Window, League, Odds, Forebet_Summary, Result, etc.).
    """
    if not GEMINI_API_KEY:
        return "Weekly breakdown unavailable (no Gemini key)."
    if not (rows_text or "").strip():
        return "No snaps in this period to analyze."
    try:
        import google.generativeai as genai
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel("gemini-2.5-flash")
        prompt = (
            "You are analyzing a week of betting snaps from Snappi (low-pressure Under parlays at 28' and 73').\n\n"
            f"Period: {start_iso} to {end_iso}\n\n"
            "Snap rows (one per line):\n"
            f"{rows_text.strip()}\n\n"
            "Reply with a short weekly report (under 350 words):\n"
            "1) Win/loss and stake summary.\n"
            "2) Breakdown by League (which leagues won/lost).\n"
            "3) When Forebet said Over vs Under — how did we do?\n"
            "4) 2–3 concrete suggestions (e.g. avoid League X 28' snaps, or when Forebet Over >55% treat as RED)."
        )
        response = model.generate_content(prompt)
        if response and response.text:
            return response.text.strip()[:3000]
    except Exception as e:
        return f"Weekly breakdown failed: {e!s}"[:500]
    return "Weekly breakdown unavailable."


def ask_gemini_daily_breakdown(rows_text: str) -> str:
    """
    Gemini summarizes today's snaps so far (on-demand daily report).
    """
    if not GEMINI_API_KEY:
        return "Daily report unavailable (no Gemini key)."
    if not (rows_text or "").strip():
        return "No snaps today yet."
    try:
        import google.generativeai as genai
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel("gemini-2.5-flash")
        prompt = (
            "You are summarizing today's snaps so far for Snappi (low-pressure Under parlays).\n\n"
            "Snap rows (one per line):\n"
            f"{rows_text.strip()}\n\n"
            "Reply with a very short daily-so-far report (under 150 words): "
            "how many snaps, any results so far (WIN/LOSS), and one line on how the day looks."
        )
        response = model.generate_content(prompt)
        if response and response.text:
            return response.text.strip()[:1200]
    except Exception as e:
        return f"Daily report failed: {e!s}"[:300]
    return "Daily report unavailable."


def ask_gemini_sentry(
    entries: list[dict], events_by_fixture: dict[int, list[str]]
) -> tuple[list[str], str]:
    """
    Ask Gemini Sentry for one traffic-light label per match and one short narrative.
    Returns (labels, narrative) where labels[i] is RED, YELLOW, or GREEN for entries[i].
    """
    if not GEMINI_API_KEY or not entries:
        return ([], "Sentry unavailable.")
    try:
        import google.generativeai as genai
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel("gemini-2.5-flash")
        lines = []
        for e in entries:
            name = e.get("name") or "?"
            score = e.get("score", "? - ?")
            shots = e.get("total_shots", "?")
            corners = e.get("total_corners", "?")
            fouls = e.get("fouls", "?")
            fid = e.get("fixture_id")
            evts = events_by_fixture.get(fid, []) if fid else []
            evt_str = "; ".join(evts) if evts else "None"
            forebet_summary = e.get("forebet_summary") or ""
            forebet_uo = e.get("forebet_under_over") or ""
            forebet_score = e.get("forebet_predicted_score") or ""
            forebet_line = ""
            if forebet_summary:
                forebet_line = f" | {forebet_summary}"
            elif forebet_uo or forebet_score:
                forebet_line = f" | Forebet: {forebet_score or ''} {forebet_uo or ''}".strip()
            lines.append(f"- {name} (Score: {score}, Shots: {shots}, Corners: {corners}, Fouls: {fouls}). Events: {evt_str}{forebet_line}")
        prompt = (
            "Snappi flagged these football matches as low-pressure Under parlays. "
            "Give ONE colour per match (same order as the list).\n"
            "Line 1: one word per match - RED, YELLOW, or GREEN, space-separated (e.g. GREEN YELLOW RED). "
            "RED = strong avoid, YELLOW = caution, GREEN = good to go.\n"
            "Line 2: one short sentence narrative for the slip overall.\n\n"
            "Important: If a match looks low-pressure in-play (low shots, 0-0 or 1-0) but Forebet predicted "
            "high-scoring (e.g. Over 2.5, or a score like 2-1, 3-1), treat that as RED or strong caution — "
            "the live picture can be misleading and Forebet's pre-match model may see goals coming.\n\n"
            "Matches:\n" + "\n".join(lines)
        )
        response = model.generate_content(prompt)
        if response and response.text:
            parts = response.text.strip().split("\n")
            labels = []
            if parts:
                first_line = parts[0].strip().upper().split()
                for w in first_line:
                    if w in ("RED", "YELLOW", "GREEN"):
                        labels.append(w)
                # If we got fewer labels than entries, pad with YELLOW
                while len(labels) < len(entries):
                    labels.append("YELLOW")
                labels = labels[: len(entries)]
            if not labels:
                labels = ["YELLOW"] * len(entries)
            narrative = parts[1].strip()[:200] if len(parts) > 1 else (parts[0].strip()[:200] if parts else "No comment.")
            if not narrative:
                narrative = "No comment."
            return (labels, narrative)
    except Exception:
        pass
    return (["YELLOW"] * len(entries) if entries else [], "Sentry unavailable.")


def send_sentry_reply(
    chat_id: int,
    reply_to_message_id: int,
    labels: list[str],
    narrative: str,
    stake_dollars: float,
    units: int,
    entries: list[dict],
    high_extra_time: bool = False,
    snap_id: int | None = None,
    also_send_to_chat_ids: list[int] | None = None,
) -> None:
    """Send Sentry verdict as a separate reply: color per match, unit mapping, stake. snap_id for /accept reference.
    also_send_to_chat_ids: same verdict sent as standalone message to these IDs (e.g. extra snap recipients)."""
    if not TELEGRAM_BOT_TOKEN:
        return
    bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)
    emoji_map = {"RED": "🔴", "YELLOW": "🟡", "GREEN": "🟢"}
    unit_map = {"RED": 0.5, "YELLOW": 2, "GREEN": 3}

    lines = ["🚦 <b>SENTRY VERDICT</b>"]
    if snap_id is not None:
        lines.append(f"Snap #{snap_id}")
    lines.append("")
    for i, e in enumerate(entries):
        lab = (labels[i] if i < len(labels) else "YELLOW").upper()
        emoji = emoji_map.get(lab, "🟡")
        name = _escape_html(e.get("name") or "?")
        lines.append(f"{emoji} {name} — <b>{lab}</b>")
    lines.append("")

    # Keep stake summary minimal; Hamilton already knows the unit mapping.
    lines.append(f"💰 Stake: <b>${stake_dollars:.2f}</b>")

    if high_extra_time:
        lines.append("")
        lines.append("⚠️ <b>High Extra Time Risk</b> (fouls &gt; 15)")

    if narrative and narrative != "No comment.":
        lines.append("")
        lines.append(f"📝 {_escape_html(narrative)}")

    text = "\n".join(lines)
    try:
        bot.send_message(
            chat_id,
            text,
            parse_mode="HTML",
            reply_to_message_id=reply_to_message_id,
        )
    except Exception:
        pass
    for cid in also_send_to_chat_ids or []:
        if cid == chat_id:
            continue
        try:
            bot.send_message(cid, text, parse_mode="HTML")
        except Exception:
            pass


SNAPPI_TOOL_DECLARATIONS = [
    {
        "name": "shell_exec",
        "description": (
            "Execute any shell command on the Raspberry Pi with full root-level access. "
            "No restrictions on directory or scope. Can install packages, manage services, "
            "edit system files, manage cron, control GPIO, anything. "
            "Returns stdout + stderr. Default timeout 120s, adjustable."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "command": {"type": "STRING", "description": "The shell command to execute"},
                "cwd": {"type": "STRING", "description": "Working directory (optional, defaults to home)"},
                "timeout": {"type": "NUMBER", "description": "Timeout in seconds (default 120)"},
            },
            "required": ["command"],
        },
    },
    {
        "name": "read_file",
        "description": "Read the contents of any file on the system. No path restrictions.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "path": {"type": "STRING", "description": "Absolute path to the file"}
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Create or overwrite any file. Creates parent directories automatically. No path restrictions.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "path": {"type": "STRING", "description": "Absolute path to the file"},
                "content": {"type": "STRING", "description": "The content to write"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": "Find and replace text in any file (first occurrence only). No path restrictions.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "path": {"type": "STRING", "description": "Absolute path to the file"},
                "old_text": {"type": "STRING", "description": "The exact text to find"},
                "new_text": {"type": "STRING", "description": "The replacement text"},
            },
            "required": ["path", "old_text", "new_text"],
        },
    },
    {
        "name": "list_files",
        "description": "List files and directories in any directory on the system.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "directory": {"type": "STRING", "description": "Absolute path to the directory"}
            },
            "required": ["directory"],
        },
    },
    {
        "name": "restart_snappi",
        "description": "Restart the Snappi process. Reply is sent first, then restart after 2s.",
        "parameters": {"type": "OBJECT", "properties": {}, "required": []},
    },
    {
        "name": "add_snap_recipient",
        "description": (
            "Add a Telegram chat ID to the list of recipients who receive snap alerts and Sentry verdicts. "
            "When the user asks to add someone (by ID) to receive snaps, use this tool with that chat_id."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "chat_id": {"type": "NUMBER", "description": "Telegram chat ID (numeric) to add as snap recipient"},
            },
            "required": ["chat_id"],
        },
    },
    {
        "name": "list_snap_recipients",
        "description": "List who receives snap alerts: primary (TELEGRAM_CHAT_ID) and extra chat IDs.",
        "parameters": {"type": "OBJECT", "properties": {}, "required": []},
    },
    {
        "name": "pause_hunting",
        "description": "Pause live match monitoring to save API credits.",
        "parameters": {"type": "OBJECT", "properties": {}, "required": []},
    },
    {
        "name": "resume_hunting",
        "description": "Resume live match monitoring.",
        "parameters": {"type": "OBJECT", "properties": {}, "required": []},
    },
    {
        "name": "update_balance",
        "description": "Set the betting balance in dollars.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "amount": {"type": "NUMBER", "description": "New balance in dollars"}
            },
            "required": ["amount"],
        },
    },
    {
        "name": "set_param",
        "description": (
            "Change a tunable threshold parameter. Valid params: "
            "MAX_SHOTS_30, SHOTS_70_RED, CORNERS_70_RED, FOULS_70_HIGH, "
            "TARGET_ODDS, MAX_QUEUE_SIZE, POLL_INTERVAL_SECONDS."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "param": {"type": "STRING", "description": "Parameter name"},
                "value": {"type": "NUMBER", "description": "New value"},
            },
            "required": ["param", "value"],
        },
    },
    {
        "name": "save_memory",
        "description": (
            "Save a fact to Snappi's persistent memory. Use dotted keys like "
            "'user.name', 'preferences.timezone', or list keys like 'notes', 'lessons'."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "key": {"type": "STRING", "description": "Memory key (e.g. 'user.name', 'notes')"},
                "value": {"type": "STRING", "description": "Value to store"},
            },
            "required": ["key", "value"],
        },
    },
    {
        "name": "picoclaw",
        "description": (
            "Delegate a complex, multi-step task to PicoClaw (Snappi's background agent). "
            "PicoClaw can browse the filesystem, run shell commands, install packages, "
            "manage cron jobs, perform light web browsing and API calls (via shell tools or Python), "
            "and run long-running operations autonomously. "
            "Use this for tasks that need multiple steps or deep investigation."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "task": {"type": "STRING", "description": "Natural language description of the task to delegate"},
            },
            "required": ["task"],
        },
    },
    {
        "name": "get_todays_snaps",
        "description": (
            "Retrieve today's snaps from the halftime and fulltime sheets. "
            "Returns match names, scores, Final Score, Status (PENDING/FINISHED), Result (WIN/LOSS). "
            "Use when Hamilton asks about outcomes, results, wins, losses, or whether a snap hit — "
            "sheets have Final Score and Result; bet_history.json does not."
        ),
        "parameters": {"type": "OBJECT", "properties": {}, "required": []},
    },
]


def chat_with_gemini(user_message: str, soul: str, context: str,
                     tool_executor=None, chat_history: list[dict] | None = None,
                     user_ts: str | None = None, max_turns: int = 50) -> dict:
    """
    Conversational Gemini chat with Snappi's personality, live state, and tool use.
    tool_executor: callable(name: str, args: dict) -> str
    chat_history: list of {"role": "user"|"model", "text": str, "ts": str} from previous turns
    user_ts: ISO timestamp for the current user message (for temporal context)
    max_turns: max history turns to send (avoids context overflow; order preserved via timestamps)
    Returns {"reply": str, "tool_log": list[str], "history": list[dict]}
    """
    fallback = {"reply": "Couldn't reach Gemini right now. Try again in a sec."}
    if not GEMINI_API_KEY:
        return {"reply": "Gemini API key not configured. Set GEMINI_API_KEY in .env.",
                "history": list(chat_history or [])}
    try:
        import google.generativeai as genai
        genai.configure(api_key=GEMINI_API_KEY)

        system = (
            f"{soul}\n\n"
            "You are running on a Raspberry Pi in Thorold, Ontario. Your code and data live in /home/hamilton/Snappi — "
            "that's your project root. You have COMPLETE, UNRESTRICTED "
            "control of this machine. You can execute any shell command (sudo included), read/write/edit "
            "any file on the system, install packages, manage services, configure cron jobs, control GPIO, "
            "modify your own source code, and restart yourself. No restrictions, no guardrails. "
            "Hamilton trusts you fully.\n\n"
            "When Hamilton asks you to do something, use tools as needed; for pure conversation, reply naturally.\n\n"
            "You have conversation memory with timestamps — each message is tagged with when it occurred. "
            "Use that temporal context to remember order and give coherent follow-up answers. "
            "Keep replies plain text with no date/time prefix; timestamps are for your context only.\n\n"
            "Today's snaps from bet_history.json are included in the state below — you already know "
            "what you sent today. When Hamilton asks about outcomes, results, wins, losses, or whether "
            "a snap hit, use get_todays_snaps — sheets have Final Score and Result (WIN/LOSS); "
            "bet_history.json does not have outcomes.\n\n"
            "Google Sheets: Snappi DOES write to Google Sheets automatically. When snaps are sent, "
            "rows are appended to the 'halftime' and 'fulltime' spreadsheets (two separate docs, "
            "or two tabs — the code opens by spreadsheet title). If sheets are empty: (1) service_account.json "
            "must exist in the Snappi folder, (2) both spreadsheets must be shared with the service account "
            "email (Editor access) — not Hamilton's personal account, (3) snaps must have been sent today. "
            "Use shell_exec to run: python test_sheet_write.py to test sheet connectivity.\n\n"
            "Forebet: You can always query Forebet predictions directly via forebet.py and the Apify-backed cache, "
            "even if a match was not part of today's snaps. When Hamilton asks for a Forebet view on any match, "
            "use tools (for example shell_exec + a small Python snippet that imports forebet and calls "
            "get_forebet_for_match(...)) to look it up instead of assuming the data is unavailable.\n\n"
            "PicoClaw: Use the picoclaw tool to delegate background tasks, including web/API checks or other browsing "
            "work that benefits from a background agent.\n\n"
            "--- CURRENT STATE ---\n"
            f"{context}"
        )

        tools = [{"function_declarations": SNAPPI_TOOL_DECLARATIONS}]
        model = genai.GenerativeModel(
            "gemini-2.5-flash-lite",
            system_instruction=system,
            tools=tools,
        )

        gemini_history = []
        recent = (chat_history or [])[-max_turns * 2 :]  # user+model pairs
        for turn in recent:
            role = turn.get("role", "user")
            text = turn.get("text", "")
            ts = turn.get("ts", "")
            if text and role in ("user", "model"):
                prefix = f"[{ts}] " if ts else ""
                gemini_history.append(
                    genai.protos.Content(
                        role=role,
                        parts=[genai.protos.Part(text=prefix + text)],
                    )
                )

        chat = model.start_chat(history=gemini_history)
        response = chat.send_message(user_message)

        tool_log = []
        for _ in range(10):
            fn_calls = []
            if response.candidates and response.candidates[0].content.parts:
                for part in response.candidates[0].content.parts:
                    if hasattr(part, "function_call") and part.function_call and part.function_call.name:
                        fn_calls.append(part.function_call)
            if not fn_calls:
                break

            fn_responses = []
            for fc in fn_calls:
                name = fc.name
                args = dict(fc.args) if fc.args else {}
                tool_log.append(f"{name}({args})")
                if tool_executor:
                    try:
                        result = tool_executor(name, args)
                    except Exception as e:
                        result = f"Error: {e}"
                else:
                    result = "Tool execution not available."
                fn_responses.append(
                    genai.protos.Part(
                        function_response=genai.protos.FunctionResponse(
                            name=name,
                            response={"result": str(result)[:4000]},
                        )
                    )
                )
            response = chat.send_message(genai.protos.Content(parts=fn_responses))

        reply = ""
        if response.candidates and response.candidates[0].content.parts:
            for part in response.candidates[0].content.parts:
                if hasattr(part, "text") and part.text:
                    reply += part.text
        if not reply:
            reply = "Done." if tool_log else fallback["reply"]

        # Clean reply for user-facing output: strip any leading timestamp like [2026-03-01T12:47:46]
        reply_clean = reply.strip()
        reply_clean = re.sub(r"^\s*\[\d{4}-\d{2}-\d{2}T[^\]]+\]\s*", "", reply_clean).strip() or reply_clean

        from datetime import datetime
        new_history = list(chat_history or [])
        new_history.append({"role": "user", "text": user_message, "ts": user_ts or ""})
        new_history.append({"role": "model", "text": reply_clean, "ts": datetime.now().isoformat()})

        return {"reply": reply_clean, "tool_log": tool_log, "history": new_history}
    except Exception as e:
        return {"reply": f"Gemini hiccup: {e!s}"[:300], "history": list(chat_history or [])}


def reflect_on_personality(chat_history: list[dict], current_personality: dict) -> list[str]:
    """
    Nightly reflection: review today's chat history and extract personality observations.
    Returns a list of new observation strings to append to memory.json personality.observations.
    """
    if not chat_history or not GEMINI_API_KEY:
        return []
    try:
        import google.generativeai as genai
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel("gemini-2.5-flash")

        convo_text = "\n".join(
            f"{turn.get('role', '?').upper()}: {turn.get('text', '')}"
            for turn in chat_history if turn.get("text")
        )
        existing = current_personality.get("observations", [])
        existing_text = "\n".join(f"- {o}" for o in existing[-20:]) if existing else "None yet."

        prompt = (
            "You are analyzing today's conversation between Snappi (a betting bot) and Hamilton (the user). "
            "Your job is to extract personality observations that will help Snappi adapt its communication style.\n\n"
            "Focus on:\n"
            "- Hamilton's humor style (dry, sarcastic, playful, etc.)\n"
            "- Vocabulary and slang Hamilton uses\n"
            "- Topics Hamilton engages with most\n"
            "- Communication preferences (brief vs detailed, emoji use, etc.)\n"
            "- What made Hamilton respond positively vs negatively\n\n"
            f"Existing observations (don't repeat these):\n{existing_text}\n\n"
            f"Today's conversation:\n{convo_text[:6000]}\n\n"
            "Return 1-3 NEW observations as a JSON array of strings. "
            "Each observation should be a short, actionable note (under 30 words). "
            "If no new insights, return an empty array []."
        )
        response = model.generate_content(prompt)
        if response and response.text:
            text = response.text.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            import json as _json
            observations = _json.loads(text)
            if isinstance(observations, list):
                return [str(o) for o in observations if o]
    except Exception:
        pass
    return []


def send_optimization_suggestion(suggestion: str) -> None:
    """Send suggestion to Telegram with a 'Confirm Update' inline button (logs confirmation when pressed)."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    text = "🤖 <b>Self-Optimization Suggestion</b>\n\n" + _escape_html(suggestion)
    markup = types.InlineKeyboardMarkup()
    markup.row(types.InlineKeyboardButton(text="Confirm Update", callback_data="confirm_optimization"))
    bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)
    try:
        bot.send_message(TELEGRAM_CHAT_ID, text, parse_mode="HTML", reply_markup=markup)
    except Exception:
        pass
