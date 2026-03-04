"""
Google Sheets logger for Snappi: split across two sheets ('Halftime' for 28-min window,
'Fulltime' for 73-min window). Uses service_account.json for auth.
Column order: Timestamp, Match, Window, Shots, Corners, Fouls, Score, Target Line,
Final Score, Status, Result, Gemini_Label, Sentry_Colour, Gemini_Analysis, Fixtures_ID,
League, Odds, Forebet_Summary, Snap_ID, Units, Stake_Dollars.
Extra columns support feedback-loop analysis (win rate by league, odds, Forebet, time).
"""
import csv
import json
import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_script_dir = os.path.dirname(os.path.abspath(__file__))
# Sheet titles (Google Drive spreadsheet names). Defaults match typical Google naming (capitalized).
# Override in .env with HALFTIME_SHEET_TITLE / FULLTIME_SHEET_TITLE if yours differ.
HALFTIME_SHEET = (os.getenv("HALFTIME_SHEET_TITLE") or "Halftime").strip()
FULLTIME_SHEET = (os.getenv("FULLTIME_SHEET_TITLE") or "Fulltime").strip()
SERVICE_ACCOUNT_JSON = os.path.join(_script_dir, "service_account.json")
THOROLD_TZ = ZoneInfo("America/Toronto")

HEADER_ROW = [
    "Timestamp",
    "Match",
    "Window",
    "Shots",
    "Corners",
    "Fouls",
    "Score",
    "Target Line",
    "Final Score",
    "Status",
    "Result",
    "Gemini_Label",
    "Sentry_Colour",
    "Gemini_Analysis",
    "Fixtures_ID",
    "League",
    "Odds",
    "Forebet_Summary",
    "Snap_ID",
    "Units",
    "Stake_Dollars",
    "Sentry_Narrative",
]
COL_STATUS = 9
COL_RESULT = 10
COL_GEMINI_LABEL = 11
COL_SENTRY_COLOUR = 12
COL_GEMINI_ANALYSIS = 13
COL_FIXTURE_ID = 14
COL_FINAL_SCORE = 8
COL_LEAGUE = 15
COL_ODDS = 16
COL_FOREBET_SUMMARY = 17
COL_SNAP_ID = 18
COL_UNITS = 19
COL_STAKE_DOLLARS = 20
COL_SENTRY_NARRATIVE = 21
REJECTIONS_KEEP_LAST = 10


def _sheet_for_window(window: str) -> str:
    """28-minute window → halftime sheet; everything else → fulltime."""
    if "28" in window:
        return HALFTIME_SHEET
    return FULLTIME_SHEET


def _get_client():
    if not os.path.isfile(SERVICE_ACCOUNT_JSON):
        logger.error("Sheets: service_account.json not found at %s", SERVICE_ACCOUNT_JSON)
        return None
    try:
        from google.oauth2.service_account import Credentials
        import gspread
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_JSON, scopes=scopes)
        return gspread.authorize(creds)
    except Exception as e:
        logger.error("Sheets: failed to authorize gspread client: %s", e)
        return None


def _ensure_header(worksheet) -> None:
    """Ensure row 1 is the canonical header."""
    try:
        all_ = worksheet.get_all_values()
        if not all_:
            end_col = chr(ord("A") + len(HEADER_ROW) - 1)
            worksheet.update(values=[HEADER_ROW], range_name=f"A1:{end_col}1", value_input_option="USER_ENTERED")
            return
        current_header = all_[0]
        if current_header[: len(HEADER_ROW)] != HEADER_ROW:
            end_col = chr(ord("A") + len(HEADER_ROW) - 1)
            worksheet.update(values=[HEADER_ROW], range_name=f"A1:{end_col}1", value_input_option="USER_ENTERED")
    except Exception as e:
        logger.error("Sheets: _ensure_header failed: %s", e)


def _header_indices(header: list) -> dict:
    """Return dict of col name -> 0-based index."""
    return {h: i for i, h in enumerate(header) if h}


def log_bet_to_sheet(
    fixture_data: dict,
    window: str,
    league: str = "",
    batch_timestamp: str | None = None,
    snap_id: int | None = None,
) -> bool:
    """Append one bet row to the appropriate sheet. Status = PENDING. Logs League, Odds, Forebet, Snap_ID for analysis."""
    sheet_name = _sheet_for_window(window)
    gc = _get_client()
    if not gc:
        logger.error("Sheets: log_bet_to_sheet failed — no gspread client")
        return False
    try:
        sh = gc.open(sheet_name)
        worksheet = sh.sheet1
    except Exception as e:
        logger.error("Sheets: could not open '%s': %s", sheet_name, e)
        return False
    _ensure_header(worksheet)
    timestamp = batch_timestamp if batch_timestamp else datetime.now(THOROLD_TZ).isoformat()
    match_name = fixture_data.get("name") or fixture_data.get("teams") or "?"
    score = fixture_data.get("score", "? - ?")
    target_line = fixture_data.get("target_line", "")
    shots = fixture_data.get("total_shots", "")
    corners = fixture_data.get("total_corners", "")
    fouls = fixture_data.get("fouls", "")
    fid = fixture_data.get("fixture_id", "")
    odds_val = fixture_data.get("odds")
    odds_str = f"{odds_val:.2f}" if isinstance(odds_val, (int, float)) and odds_val else (str(odds_val) if odds_val else "")
    forebet_summary = (fixture_data.get("forebet_summary") or "").strip()
    row = [
        timestamp,
        match_name,
        window,
        str(shots),
        str(corners),
        str(fouls),
        score,
        target_line,
        "",          # Final Score
        "PENDING",
        "",          # Result
        "",          # Gemini_Label
        "",          # Sentry_Colour
        "",          # Gemini_Analysis
        str(fid),
        (league or "").strip(),
        odds_str,
        forebet_summary,
        str(snap_id) if snap_id is not None else "",
        "",          # Units (filled when Sentry replies)
        "",          # Stake_Dollars (filled when Sentry replies)
        "",          # Sentry_Narrative (filled when Sentry replies)
    ]
    try:
        worksheet.append_row(row, value_input_option="USER_ENTERED", table_range="A1")
        logger.info("Sheets: logged bet for %s (%s) → %s", match_name, window, sheet_name)
        return True
    except Exception as e:
        logger.error("Sheets: append_row failed for %s: %s", match_name, e)
        return False


def get_pending_sheet_rows():
    """
    Return list of dicts for each row with Status = 'PENDING' or 'RECYCLED' across both sheets.
    Each dict includes a 'sheet_name' field so callers know which sheet owns the row.
    """
    gc = _get_client()
    if not gc:
        logger.error("Sheets: get_pending_sheet_rows failed — no gspread client")
        return []
    out = []
    for sheet_name in (HALFTIME_SHEET, FULLTIME_SHEET):
        try:
            sh = gc.open(sheet_name)
            worksheet = sh.sheet1
            _ensure_header(worksheet)
            all_ = worksheet.get_all_values()
        except Exception as e:
            logger.error("Sheets: get_pending_sheet_rows failed for %s: %s", sheet_name, e)
            continue
        if len(all_) < 2:
            continue
        header = all_[0]
        idx = _header_indices(header)
        need = ["Status", "Fixtures_ID", "Match", "Score", "Target Line", "Window", "Timestamp"]
        if not all(k in idx for k in need):
            continue
        min_len = max(idx[k] for k in need) + 1
        for i in range(1, len(all_)):
            row = all_[i]
            if len(row) <= min_len:
                continue
            status = (row[idx["Status"]].strip().upper() if row[idx["Status"]] else "") if "Status" in idx else ""
            if status not in ("PENDING", "RECYCLED"):
                continue
            fid_s = row[idx["Fixtures_ID"]].strip() if idx["Fixtures_ID"] < len(row) else ""
            if not fid_s:
                continue
            try:
                fid_int = int(fid_s)
            except ValueError:
                continue
            out.append({
                "row_index": i + 1,
                "fixture_id": fid_int,
                "teams": row[idx["Match"]] if idx["Match"] < len(row) else "?",
                "score": row[idx["Score"]].strip() if idx["Score"] < len(row) else "? - ?",
                "score_at_70": row[idx["Score"]].strip() if idx["Score"] < len(row) else "? - ?",
                "target_line": row[idx["Target Line"]].strip() if idx["Target Line"] < len(row) else "",
                "timestamp": row[idx["Timestamp"]].strip() if idx["Timestamp"] < len(row) else "",
                "window": row[idx["Window"]].strip() if idx["Window"] < len(row) else "",
                "sheet_name": sheet_name,
            })
    return out


def clear_sheet_data(keep_headers: bool = True) -> bool:
    """Clear all data rows from both halftime and fulltime sheets."""
    gc = _get_client()
    if not gc:
        return False
    ok = True
    for sheet_name in (HALFTIME_SHEET, FULLTIME_SHEET):
        try:
            sh = gc.open(sheet_name)
            worksheet = sh.sheet1
            _ensure_header(worksheet)
            end_col = chr(ord("A") + len(HEADER_ROW) - 1)
            worksheet.batch_clear([f"A2:{end_col}1000"])
        except Exception as e:
            logger.error("Sheets: clear_sheet_data failed for %s: %s", sheet_name, e)
            ok = False
    return ok


def update_row_on_ft(
    row_index: int,
    final_score: str,
    result: str,
    sheet_name: str = FULLTIME_SHEET,
    gemini_label: str = "",
    gemini_analysis: str = "",
) -> bool:
    """Update a single row when fixture is FT: Final Score, Status=FINISHED, Result."""
    gc = _get_client()
    if not gc:
        logger.error("Sheets: update_row_on_ft failed — no gspread client")
        return False
    try:
        sh = gc.open(sheet_name)
        worksheet = sh.sheet1
        worksheet.update_cell(row_index, COL_FINAL_SCORE + 1, final_score)
        worksheet.update_cell(row_index, COL_STATUS + 1, "FINISHED")
        worksheet.update_cell(row_index, COL_RESULT + 1, result)
        worksheet.update_cell(row_index, COL_GEMINI_LABEL + 1, gemini_label)
        worksheet.update_cell(row_index, COL_SENTRY_COLOUR + 1, gemini_label)
        worksheet.update_cell(row_index, COL_GEMINI_ANALYSIS + 1, gemini_analysis)
        logger.info("Sheets: updated row %d on %s -> %s %s", row_index, sheet_name, final_score, result)
        return True
    except Exception as e:
        logger.error("Sheets: update_row_on_ft failed for row %d on %s: %s", row_index, sheet_name, e)
        return False


def update_sentry_label(
    row_index: int,
    label: str,
    sheet_name: str = FULLTIME_SHEET,
    units: int | None = None,
    stake_dollars: float | None = None,
    narrative: str | None = None,
) -> bool:
    """Set Gemini_Label, Sentry_Colour, optionally Units / Stake_Dollars / Sentry_Narrative for a row."""
    gc = _get_client()
    if not gc:
        logger.error("Sheets: update_sentry_label failed — no gspread client")
        return False
    try:
        sh = gc.open(sheet_name)
        worksheet = sh.sheet1
        worksheet.update_cell(row_index, COL_GEMINI_LABEL + 1, label)
        worksheet.update_cell(row_index, COL_SENTRY_COLOUR + 1, label)
        if units is not None:
            worksheet.update_cell(row_index, COL_UNITS + 1, units)
        if stake_dollars is not None:
            worksheet.update_cell(row_index, COL_STAKE_DOLLARS + 1, round(stake_dollars, 2))
        if narrative is not None and narrative.strip():
            worksheet.update_cell(row_index, COL_SENTRY_NARRATIVE + 1, narrative.strip()[:500])
        return True
    except Exception as e:
        logger.error("Sheets: update_sentry_label failed for row %d on %s: %s", row_index, sheet_name, e)
        return False


def update_sheet_row(
    worksheet,
    row_index: int,
    outcome: str,
    gemini_analysis: str = "",
    gemini_label: str = "",
    final_score: str | None = None,
) -> bool:
    """Update Final Score (optional), Status, Result, Gemini_Label, Sentry_Colour, Gemini_Analysis (1-based row)."""
    try:
        if final_score is not None:
            worksheet.update_cell(row_index, COL_FINAL_SCORE + 1, final_score)
        worksheet.update_cell(row_index, COL_STATUS + 1, "FINISHED")
        worksheet.update_cell(row_index, COL_RESULT + 1, outcome)
        worksheet.update_cell(row_index, COL_GEMINI_LABEL + 1, gemini_label)
        worksheet.update_cell(row_index, COL_SENTRY_COLOUR + 1, gemini_label)
        worksheet.update_cell(row_index, COL_GEMINI_ANALYSIS + 1, gemini_analysis)
        return True
    except Exception as e:
        logger.error("Sheets: update_sheet_row failed for row %d: %s", row_index, e)
        return False


def update_nightly_results(updates: list[dict]) -> bool:
    """Apply results grouped by sheet_name.

    updates: list of
        {
            row_index,
            outcome,
            gemini_analysis,
            gemini_label,
            sheet_name,
            final_score (optional),
        }
    """
    if not updates:
        return True
    gc = _get_client()
    if not gc:
        logger.error("Sheets: update_nightly_results failed — no gspread client")
        return False
    by_sheet: dict[str, list[dict]] = {}
    for u in updates:
        sn = u.get("sheet_name", FULLTIME_SHEET)
        by_sheet.setdefault(sn, []).append(u)
    for sheet_name, sheet_updates in by_sheet.items():
        try:
            sh = gc.open(sheet_name)
            worksheet = sh.sheet1
        except Exception as e:
            logger.error("Sheets: update_nightly_results could not open %s: %s", sheet_name, e)
            return False
        for u in sheet_updates:
            ok = update_sheet_row(
                worksheet,
                u["row_index"],
                u.get("outcome", ""),
                u.get("gemini_analysis", ""),
                u.get("gemini_label", ""),
                u.get("final_score"),
            )
            if not ok:
                return False
    return True


def trim_rejections(rejections_path: str) -> None:
    """Keep only the last REJECTIONS_KEEP_LAST data rows in rejections.csv."""
    if not os.path.isfile(rejections_path) or REJECTIONS_KEEP_LAST <= 0:
        return
    rows = []
    with open(rejections_path, "r", encoding="utf-8", newline="") as f:
        rows = list(csv.reader(f))
    if not rows or len(rows) <= 1:
        return
    header, data = rows[0], rows[1:]
    last = data[-REJECTIONS_KEEP_LAST:] if len(data) > REJECTIONS_KEEP_LAST else data
    with open(rejections_path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(last)


def _get_rows_from_sheets_where(
    gc,
    date_predicate,
) -> list[dict]:
    """Shared helper: return rows from both sheets where timestamp date satisfies date_predicate(YYYY-MM-DD)."""
    out = []
    for sheet_name in (HALFTIME_SHEET, FULLTIME_SHEET):
        try:
            sh = gc.open(sheet_name)
            worksheet = sh.sheet1
            all_ = worksheet.get_all_values()
        except Exception as e:
            logger.error("Sheets: get rows failed for %s: %s", sheet_name, e)
            continue
        if len(all_) < 2:
            continue
        header = all_[0]
        for i in range(1, len(all_)):
            row = all_[i]
            timestamp = (row[0] or "").strip()
            date_str = timestamp[:10] if len(timestamp) >= 10 else ""
            if not date_str or not date_predicate(date_str):
                continue
            entry = {}
            for j, h in enumerate(header):
                if j < len(row):
                    entry[h] = row[j]
            entry["row_index"] = i + 1
            entry["sheet_name"] = sheet_name
            out.append(entry)
    return out


def get_todays_rows() -> list[dict]:
    """Return all rows from both sheets where Timestamp starts with today's date."""
    gc = _get_client()
    if not gc:
        logger.error("Sheets: get_todays_rows failed — no gspread client")
        return []
    today_str = datetime.now(THOROLD_TZ).strftime("%Y-%m-%d")
    return _get_rows_from_sheets_where(gc, lambda d: d == today_str)


def get_rows_for_date_range(start_date_iso: str, end_date_iso: str) -> list[dict]:
    """Return all rows from both sheets where Timestamp date is in [start_date_iso, end_date_iso] (inclusive)."""
    gc = _get_client()
    if not gc:
        logger.error("Sheets: get_rows_for_date_range failed — no gspread client")
        return []

    def in_range(d: str) -> bool:
        return start_date_iso <= d <= end_date_iso

    return _get_rows_from_sheets_where(gc, in_range)


USAGE_STATS_SHEET_TITLE = "Usage Stats"
USAGE_STATS_HEADER = ["Date", "Total_Requests"]


def log_daily_usage_to_sheet(date_str: str, total_calls: int) -> bool:
    """Log daily API usage to the 'Usage Stats' tab on the fulltime sheet."""
    gc = _get_client()
    if not gc:
        logger.error("Sheets: log_daily_usage failed — no gspread client")
        return False
    try:
        sh = gc.open(FULLTIME_SHEET)
        try:
            ws = sh.worksheet(USAGE_STATS_SHEET_TITLE)
        except Exception:
            ws = sh.add_worksheet(USAGE_STATS_SHEET_TITLE, rows=1000, cols=5)
        all_vals = ws.get_all_values()
        if not all_vals:
            ws.update(values=[USAGE_STATS_HEADER], range_name="A1:B1", value_input_option="USER_ENTERED")
        ws.append_row([date_str, total_calls], value_input_option="USER_ENTERED", table_range="A1")
        logger.info("Sheets: logged daily usage %s -> %d calls", date_str, total_calls)
        return True
    except Exception as e:
        logger.error("Sheets: log_daily_usage failed: %s", e)
        return False
