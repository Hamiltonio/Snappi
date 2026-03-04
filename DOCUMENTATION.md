# Snappi – In-Depth Documentation

This document describes the Snappi soccer betting alert bot so you can run it, modify it, or recreate a similar instance (e.g. another sport, region, or strategy).

---

## Table of Contents

1. [Overview](#1-overview)
2. [Architecture](#2-architecture)
3. [Environment & Configuration](#3-environment--configuration)
4. [File Layout](#4-file-layout)
5. [Data Files](#5-data-files)
6. [Dual-Phase Lifecycle](#6-dual-phase-lifecycle)
7. [Hunter Phase – Logic in Detail](#7-hunter-phase--logic-in-detail)
8. [Analyst Phase – Nightly Analysis](#8-analyst-phase--nightly-analysis)
9. [API Usage & Counting](#9-api-usage--counting)
10. [Telegram – Commands & Notifications](#10-telegram--commands--notifications)
11. [Pause / Resume & Auto-Resume](#11-pause--resume--auto-resume)
12. [Logging & Rotation](#12-logging--rotation)
13. [Error Handling](#13-error-handling)
14. [Deployment (e.g. Raspberry Pi)](#14-deployment-eg-raspberry-pi)
15. [Why the Link Doesn’t Add the Match to the Betslip](#15-why-the-link-doesnt-add-the-match-to-the-betslip)
16. [Recreating or Extending](#16-recreating-or-extending)

---

## 1. Overview

**What Snappi does**

- Runs 24/7 in two phases by **Thorold, Ontario (America/Toronto)** time.
- **Hunter (05:00–00:00 / Midnight):** Polls live football fixtures every **120 seconds**, looks for “low pressure” matches in two time windows (around 30' and 70'), and sends **Telegram** alerts with a “SNAP THE PARLAY” button (bet365 link).
- **Analyst (00:01–04:59):** No live polling. At **00:01** sends "Snappi entering Analyst mode" alert. At **00:05** it runs a **nightly analysis**: loads the day’s alerts, fetches final scores and (for losses) fixture events, asks **Gemini** for a one-sentence take on each loss. At **00:30** sends **daily summary** (total profit).

**Design goals**

- Stay within **7500 API calls/day** limit (api-sports.io Pro tier).
- Let you **pause** live monitoring from your phone to save credits when the board is dry.
- **Auto-resume** at 05:00 so the bot is ready for the next day without manual /resume.
- Run **headless** (e.g. Raspberry Pi) with full control via Telegram.

**Stack**

- **Python 3** (zoneinfo, so 3.9+).
- **api-sports.io (API-Football)** for fixtures, statistics, and events.
- **Telegram** (pyTelegramBotAPI / telebot) for alerts and commands.
- **Google Gemini** (gemini-2.5-flash) for loss analysis.
- **.env** for all secrets; no credentials in code.

---

## 2. Architecture

**Main loop (`main.py` → `run()`)**

- Single infinite loop.
- Uses **Thorold time** (`get_thorold_now()`) to decide:
  - **Hunter:** `is_hunter_phase()` True → run `process_live_matches()` every 120s (unless paused), session summary every 10 min ("Matches Rejected This Session").
  - **Analyst:** else → at 00:01 send "entering Analyst mode" alert (once); hourly heartbeat; at 00:05 run `nightly_analysis()`; at 00:30 send daily summary.

**Telegram listener (separate thread)**

- `run_telegram_listener()` runs in a **daemon thread** and calls `bot.infinity_polling()`.
- So the bot can receive `/pause`, `/resume`, `/status`, etc. **while** the main loop is sleeping (e.g. 180s or 60s). No blocking.

**Modules**

- **`main.py`:** Config, API fetches, Hunter/Analyst logic, queues, timers, Telegram **command** handlers (/status, /pause, /pending, /logs, …).
- **`notifier.py`:** Sending messages to Telegram (alerts, nightly summary, stake/odds prompts, veto alerts, simple one-off messages) and calling **Gemini** for loss analysis and post-alert veto.
- **`sheets_logger.py`:** Google Sheets integration. Logs bets to **Halftime** and **Fulltime** spreadsheets (28-min vs 73-min window). Requires `service_account.json` and `gspread`, `google-auth`. Share both sheets with the service account email (Editor). Override names via `HALFTIME_SHEET_TITLE` / `FULLTIME_SHEET_TITLE` in `.env` if yours differ.
- **`.env`:** All keys and IDs; loaded from the script directory first, then `find_dotenv()`.

**Global state (main.py)**

- Queues: `flagged_30`, `flagged_70` (fixture_id → match entry).
- Timers: `queue_30_started_at`, `queue_70_started_at` (when the 180s window started).
- Counters: `rejections_count`, `alerts_sent_today`, `api_calls_today`, `api_calls_date`.
- Pause: `is_paused`, `allow_auto_resume_next_hunter`.
- All Telegram handlers and the main loop read/write these globals; the `global` declaration for `is_paused` and `allow_auto_resume_next_hunter` is at the **top** of `run()` to avoid Python “global headache” (must be first use in the function).

---

## 3. Environment & Configuration

**`.env` (in project root, no spaces around `=`):**

```env
API_FOOTBALL_KEY=your_api_sports_key
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
GEMINI_API_KEY=your_gemini_key
```

- **API_FOOTBALL_KEY:** From [api-sports.io](https://api-sports.io/) (API-Football). Used for fixtures, statistics, and events.
- **TELEGRAM_BOT_TOKEN:** From [@BotFather](https://t.me/BotFather). The bot that sends alerts and replies to commands.
- **TELEGRAM_CHAT_ID:** Where alerts and nightly summary are sent. Commands (/status, /pause, …) can be sent from any chat; replies go to the user who sent the command.
- **GEMINI_API_KEY:** For Gemini 2.5 Flash loss analysis.
- **PINNACLE_USERNAME** / **PINNACLE_PASSWORD:** Pinnacle API credentials (Basic auth; password max 10 chars). Request access at [Pinnacle Solution](https://www.pinnaclesolution.com/en/contact-us).
- **AUTO_PLACE_PINNACLE:** Set to `true` to auto-place Under bets on Pinnacle when Sentry gives a verdict. Requires PINNACLE_* credentials.
- **CHAT_HISTORY_MAX_TURNS:** Max conversation turns sent to Gemini (default 50). Order preserved via timestamps.

**Key constants (main.py) – tune for your strategy/timezone:**

| Constant | Default | Meaning |
|--------|--------|--------|
| `THOROLD_TZ` | `America/Toronto` | Timezone for “now” and midnight reset. |
| `HUNTER_START_HOUR` | 5 | Hunter phase starts at 05:00. |
| `HUNTER_END_HOUR` | 24 | Hunter phase ends at midnight (00:00 = Analyst). |
| `ANALYST_NIGHTLY_HOUR` / `ANALYST_NIGHTLY_MINUTE` | 0 / 5 | Nightly analysis runs at 00:05. |
| `ANALYST_SUMMARY_HOUR` / `ANALYST_SUMMARY_MINUTE` | 0 / 30 | Daily summary (profit) at 00:30. |
| `UNDER_GOALS_LOSS_THRESHOLD` | 3 | “Loss” = we predicted low pressure but total goals ≥ this. |
| `VALUE_WINDOW_SECONDS` | 180 | 3-minute master timer: send queue after 180s. |
| `SAFETY_NET_MINUTE_1ST_HALF` | 36 | If any match in 30' queue reaches 36', force-send. |
| `SAFETY_NET_MINUTE_2ND_HALF` | 74 | If any match in 70' queue reaches 74', force-send. |
| `MIN_PARLAY_SIZE` | 1 | Minimum matches in queue to send (1 = solo allowed). |
| `MAX_QUEUE_SIZE` | 5 | Max matches per window; at 5, send immediately. |
| `TARGET_ODDS` | 2.00 | Send when combined odds ≥ this (or timer/safety/queue full). |
| `API_DAILY_LIMIT` | 7500 | Pro tier; shown in /status; counter resets at 05:00 Thorold (usage day). |
| `MAX_SHOTS_30` / `MAX_DA_30` | 5 / 15 | 30' window: reject if Shots > 5 or DA > 15. |
| `MAX_CORNERS_70` / `MAX_SHOTS_70` | 11 / 40 | 70' window: reject if Corners > 11 or Shots > 40; Red > 0 = veto. |
| `LOG_MAX_BYTES` | 2 MB | Rotate snappi.log when this size. |
| `LOG_BACKUP_COUNT` | 1 | Keep one rotated file (snappi.log.1). |

**30' window criteria (main.py):** Add **Dangerous Attacks ≤ 15** (MAX_DA_30).

- Elapsed minute **28–32**.
- Score **0–0, 1–0, or 0–1**.
- **Total Shots** (home + away) **≤ 5** (MAX_SHOTS_30).

**70' window criteria:**

- Elapsed minute **68–72**.
- Total goals **≤ 2**.
- **Veto:** Red Card > 0. **Reject:** Corners > 11 or Shots > 40.

Rejection reasons (e.g. “High Pressure: X Shots (Limit: 2)”) are written to `rejections.csv`.

---

## 4. File Layout

```
Snappi/
├── main.py              # Entry point, loop, fetch logic, queues, Telegram commands
├── notifier.py          # Telegram alerts/summary, stake/odds prompts, veto, Gemini
├── sheets_logger.py     # Google Sheets (SnappiLogger), bet logging
├── test_sheet_write.py  # Test script to verify SnappiLogger write
├── service_account.json # Google service account (do not commit)
├── .env                 # Secrets (do not commit)
├── bet_history.json     # Created at runtime; each alerted match
├── rejections.csv       # Created at runtime; skipped matches + reason
├── snappi.log           # Rotating log (snappi.log.1 when rotated)
├── DOCUMENTATION.md     # This file
├── blueprint.txt        # Optional short spec
├── run_simulation.py     # Optional test mode (simulation)
└── simulation_data.py   # Optional test data
```

**Run:**

```bash
python main.py
```

No simulation: `main.py` uses real API and real time. Simulation is only when you run `run_simulation.py` (or equivalent) that patches/maintains test data.

---

## 5. Data Files

**`bet_history.json`**

- One object per **alert sent**: fixture_id, teams, window (e.g. "30-Minute Scan", "70-Minute Scan"), prediction, timestamp.
- For **70-Minute Scan** entries, **`score_at_70`** is stored (e.g. "1 - 0") for nightly loss analysis.
- **Wiped at startup** (go-live clean slate). Cleared again after nightly analysis so the next day starts empty.

**`rejections.csv`**

- Header: `timestamp,match_name,minute,reason,total_shots,dangerous_attacks`.
- One row per match that entered a window but **failed** the pressure check (e.g. too many shots or dangerous attacks).
- **Wiped at startup** with `bet_history.json`. `/rejections` returns the last 5 rows.

**`snappi.log`**

- Application log (logging module). Rotates at 2 MB; backup `snappi.log.1`. `/logs` reads last 20 lines; `/clearlogs` truncates the current file.

---

## 6. Dual-Phase Lifecycle

**Phase 1 – Hunter (05:00–00:00 Thorold)**

1. If **auto-resume** conditions hold (first time in 05:xx and was paused), set `is_paused = False` and send “Good Morning! Snappi has auto-resumed…” to Telegram.
2. If **`is_paused`:** print “Bot is PAUSED. Skipping poll…”, sleep 180s, continue loop (no API calls).
3. Else: call **`process_live_matches()`** (fetch live fixtures, scan 30'/70' windows, update queues, maybe send alerts). Every 10 minutes print a session summary (queue size, rejections count). Then sleep 180s.

**Phase 2 – Analyst (00:01–04:59)**

1. Set **`allow_auto_resume_next_hunter = True`** so the next 05:00 can trigger auto-resume again.
2. Every **60s** check time. If an hour has passed since last heartbeat, print “Snappi Analyst is idling…”.
3. If time is **20:15** and we haven’t run analysis for today’s date: run **`nightly_analysis()`**, then set `last_analysis_date` to today.
5. If time is **00:30** and nightly has run: send **daily summary** (total profit).
6. Sleep 60s and repeat.

---

## 7. Hunter Phase – Logic in Detail

**`process_live_matches()`**

1. **Fetch** live fixtures: `GET /fixtures?live=all` (one API call).
2. For each fixture, get **elapsed minute** and **goals** from the fixture object.
3. **Apply in order:**

   **A) 30' queue – Master timer & safety**

   - If the 30' queue has entries and the 180s timer was started:
     - If 180s have passed → **send_queue_alert(flagged_30, "30-Minute Scan")**, clear timer.
     - Else if any match in the queue has elapsed ≥ **36'** → force-send and clear timer.
   - Same idea for **70' queue** with **74'** safety net.

   **B) 70' queue – Master timer & safety**

   - Same as 30' but for `flagged_70` and 74'.

   **C) Scan and add matches**

   - For each live match:
     - **30' window:** elapsed 28–32, score 0–0/1–0/0–1 → fetch statistics → if Total Shots < 2, add to `flagged_30`, start timer if first in queue, then **check_and_send_alert** (sends if queue full or odds ≥ target).
     - **70' window:** elapsed 68–72, total goals ≤ 2 → fetch statistics → if Dangerous Attacks < 5, add to `flagged_70`, same send logic.
   - If a match is in window but fails the stat check, **log_rejection** to CSV.

**Sending**

- **send_queue_alert:** Used for the 180s master timer and 36'/74' safety. Sends whatever is in the queue (1–5 matches), appends to bet_history, increments `alerts_sent_today`, clears queue.
- **check_and_send_alert:** Used when adding a match. Sends if queue size ≥ 1 and (force_send **or** total_odds ≥ TARGET_ODDS **or** queue size = MAX_QUEUE_SIZE).

**Bet history**

- For **70-Minute Scan** entries, **score_at_70** is stored (score at alert time) for Gemini context later.

---

## 8. Analyst Phase – Nightly Analysis

**`nightly_analysis()`** (runs once at 20:15):

1. Load **bet_history.json**. If empty or missing, send nightly summary with 0 wins, 0 losses and exit.
2. For each bet:
   - **Fetch result:** `GET /fixtures?id={fixture_id}` → home/away goals.
   - **Win:** total goals < UNDER_GOALS_LOSS_THRESHOLD (3). Increment wins.
   - **Loss:** total goals ≥ 3. Increment losses. Then:
     - Read **score_at_70** from the bet record (or "? - ?").
     - **Fetch events:** `GET /fixtures/events?fixture={id}`.
     - **events_after_70():** keep only Goals and Red Cards with minute > 70; build a short event list string.
     - Call **notifier.ask_gemini_loss(home_name, away_name, score_at_70, final_score, event_list)**.
     - Append (teams_str, gemini_reason) to **loss_details**.
3. Call **notifier.send_nightly_summary(wins, losses, loss_details)** (HTML, one message).
4. **Clear** bet_history.json (write `[]`) for the next day.

**Gemini prompt (notifier.py):**

- “Analyze this loss: {Home} vs {Away}. Flagged at 70' (Score: {Score_at_70}). Final: {Final_Score}. Events after 70': {Event_List}. Based on this, was it a preventable error or 'the game being the game'? One concise sentence.”
- Model: **gemini-2.5-flash**. Response truncated to 300 chars.

---

## 9. API Usage & Counting

**Endpoints used**

- **GET /fixtures?live=all** – live fixtures (Hunter, every 120s when not paused).
- **GET /fixtures/statistics?fixture={id}** – per fixture in 30'/70' windows.
- **GET /fixtures?id={id}** – full-time result (nightly, per bet).
- **GET /fixtures/events?fixture={id}** – events (nightly, per loss).

**Counting**

- **`_count_api_call()`** in main.py: if current **Thorold date** is different from **api_calls_date**, reset **api_calls_today** to 0 and set **api_calls_date** to today. Then increment **api_calls_today**.
- Called **once per request** (before each `requests.get` to api-sports.io) in:
  - `fetch_live_fixtures()`
  - `fetch_fixture_statistics()`
  - `fetch_fixture_result()`
  - `fetch_fixture_events()`
- **/status** shows: `API calls today: X / 7500`.

---

## 10. Telegram – Commands & Notifications

**All replies use `parse_mode='HTML'`.** Escaping is done via `_html_escape` / `_bold_html` where needed.

**Commands (handled in main.py, same bot as alerts):**

| Command | Description |
|--------|-------------|
| **/status** | Status (ACTIVE/PAUSED), Phase (Hunter/Analyst), 30' and 70' queue counts, alerts sent today, API calls today / 7500. |
| **/pause** | Set `is_paused = True`. Reply: “Snappi Paused. Live monitoring has stopped to save API credits.” |
| **/resume** | Set `is_paused = False`. Reply: “Snappi Resumed. Hunting for low-pressure matches…” |
| **/heartbeat** | Reply: “Snappi is active.” + current Thorold time. |
| **/pending** | List parlays waiting for stake/odds. Reply to each message with stake/odds or SKIP. |
| **/rejections** | Last 5 rows of rejections.csv (match, minute, reason). |
| **/logs** | Last 20 lines of snappi.log in a `<pre>` block. If file missing/empty: “No logs found yet. Snappi is fresh!” |
| **/clearlogs** | Truncate snappi.log, reply “Logs cleared. snappi.log emptied.” |

**Notifications (notifier.py → TELEGRAM_CHAT_ID):**

- **Snappi alert:** Window name, SOLO/DOUBLE/PARLAY, per match: Home vs Away (score), Shots, Dangerous Attacks, “SNAP THE PARLAY” button (bet365 link).
- **Nightly summary:** Wins, losses, and for each loss: match name + Gemini one-sentence take (HTML).
- **Simple message:** Auto-resume “Good Morning! Snappi has auto-resumed…”; Hunter error traceback (see Error Handling).

---

## 11. Pause / Resume & Auto-Resume

**Pause**

- **/pause** sets global **`is_paused = True**.
- In the Hunter block, the first check after auto-resume is: if **is_paused**, print “Bot is PAUSED. Skipping poll…”, **sleep(180)**, **continue**. So **no** `fetch_live_fixtures()` or statistics calls while paused → saves API credits.

**Resume**

- **/resume** sets **is_paused = False**. Next loop iteration will run `process_live_matches()` again.

**Auto-resume**

- At the **start** of the Hunter block, if **current hour == 5** (HUNTER_START_HOUR) and **is_paused** and **allow_auto_resume_next_hunter**:
  - Set **is_paused = False**, **allow_auto_resume_next_hunter = False**.
  - Send “Good Morning! Snappi has auto-resumed for the new hunting session.” to TELEGRAM_CHAT_ID.
- So if you left it paused overnight, it turns back on once at 05:xx. **Only once per Hunter session:** after that, `allow_auto_resume_next_hunter` is False until you enter Analyst again.
- When in **Analyst**, we set **allow_auto_resume_next_hunter = True** each loop, so the next day’s 05:00 can trigger auto-resume again.

**Threading**

- Telegram **infinity_polling** runs in a **daemon thread**, so the main loop can be in **sleep(180)** and still receive **/resume** (or any command). No need to wait for the next poll to react.

---

## 12. Logging & Rotation

- **LOG_FILE** = script directory + `"snappi.log"`.
- **RotatingFileHandler:** maxBytes = **2 MB**, backupCount = **1**. When snappi.log exceeds 2 MB it is rotated to **snappi.log.1** and a new snappi.log is created.
- **Format:** `%(asctime)s [%(levelname)s] %(message)s`.
- **Logger:** `logger = logging.getLogger(__name__)`; used for **logger.exception(...)** in the Hunter error handler (writes traceback to the log as well as sending it to Telegram).
- **/logs** reads the **current** snappi.log (last 20 lines, `<pre>`, truncated to 4000 chars if needed). **/clearlogs** truncates the current file.

---

## 13. Error Handling

**Hunter phase (process_live_matches and session summary):**

- **ValueError** (e.g. missing API key): caught, printed, loop continues.
- **Any other Exception:**
  - **traceback** = `traceback.format_exc()`.
  - **logger.exception("Hunter phase error")** → full traceback in snappi.log.
  - **notifier.send_simple_message(...)** → Telegram message: “Snappi Hunter Error” + traceback in `<pre>` (truncated to 4000 chars).
  - Print the exception, then **continue** (no re-raise). Next iteration in 180s.

So an API change or network error sends the traceback to Telegram and the bot keeps running.

---

## 14. Deployment (e.g. Raspberry Pi)

- **Python:** 3.9+ (for zoneinfo). Install dependencies (e.g. `requests`, `python-dotenv`, `pyTelegramBotAPI`, `google-generativeai`).
- **.env:** Create in project root with the four keys; ensure no spaces around `=`.
- **Run:** `python main.py`. For 24/7: use **systemd** or **screen**/ **tmux** (e.g. `python main.py` in a detached session).
- **Paths:** All paths (LOG_FILE, bet_history.json, rejections.csv) are under the **script directory** (`_script_dir`), so run from the project root or set `PYTHONPATH` so `main.py` is the entry point and its directory is the base.
- **Wipe on go-live:** Startup calls **wipe_bet_history_and_rejections()** so each run starts with empty bet_history and rejections. Comment that out if you want to preserve history across restarts.

**Hardware**

- **Pi Zero (current):** Snappi runs fine with Apify-only Forebet (no headless browser). Do **not** run Playwright/Chromium on a Pi Zero; it will overload the board.
- **If you want headless-browser features later** (e.g. Forebet fallback when Apify misses a match): use a **Raspberry Pi 4 (2GB RAM minimum)** or **Pi 5**. Pi 4 2GB is the cheapest option that can run Chromium headless; Pi 4 4GB or Pi 5 gives more headroom. Clone this repo onto the new Pi, install `playwright` and `playwright install chromium` in the venv, then add the headless Forebet script when ready.

---

## 15. Why the Link Doesn’t Add the Match to the Betslip

**Short answer:** It’s not something we can fix in Snappi alone. Bet365 does **not** provide a public URL format that adds a specific selection (or parlay) to the betslip.

**What Snappi does today**

- The button opens: **Bet365 in-play** (`#/AS/B1/`) plus a **search** for the first team name (`?q=TeamName`), so you land near the right match and can tap into it and add to betslip yourself.
- That’s the best we can do without Bet365 exposing an “add to betslip” deep link.

**Why it’s not fixable (for now)**

1. **No public betslip deep link**  
   Bet365 doesn’t document a URL (e.g. `?sel=12345` or `&addToBetslip=...`) that pre-fills the betslip. Other bookmakers (e.g. some BetMGM flows) do, but Bet365’s scheme isn’t public.
2. **Our data doesn’t include Bet365 selection IDs**  
   We use API-Football for **fixtures and stats**, not for Bet365 odds/selection IDs. Even if we called an odds endpoint, the IDs in the API are often not the same as the ones Bet365 uses in their own URLs, and Bet365’s URL format isn’t documented for third parties.
3. **So:**  
   We can’t build a URL that “contains the match in the betslip” until either (a) Bet365 publishes such a format, or (b) we get a documented way (e.g. affiliate/partner docs) to build betslip links from their IDs.

**What *is* possible**

- **Improve the current link:** Keep using in-play + search; you can tweak `BET365_BASE` or the `?q=` value (e.g. league or competition) if your region supports more query params.
- **Multiple buttons (one per match):** We could send several “Open match” buttons, each with a search for that match’s home team, so you open the exact match in one tap—still no betslip pre-fill.
- **If Bet365 ever adds betslip deep links:** Then we’d need a source for their selection/event IDs (e.g. from an odds provider that documents Bet365 IDs and URL format) and we’d add something like `build_bet365_betslip_link(selection_ids)` in `main.py` and use that for the main button.

So: it’s not difficult on our side—it’s **blocked by lack of a public Bet365 betslip URL scheme and matching IDs**. The current link is there to get you to the right place quickly; the actual “add to betslip” step stays on Bet365’s site.

---

## 16. Recreating or Extending

**To recreate a similar bot (e.g. different sport or region):**

1. **Timezone:** Change `THOROLD_TZ` and phase hours (`HUNTER_START_HOUR`, etc.).
2. **Criteria:** Adjust 30'/70' minute bands, score rules, and stat thresholds (shots, dangerous attacks). You can add more windows (e.g. 45') by adding more queues and timers.
3. **API:** Swap or add endpoints; keep **one** `_count_api_call()` per request if you have a daily cap.
4. **Notifier:** Same Telegram/Gemini pattern; change message text and (if needed) the Gemini prompt and model.

**To extend this codebase:**

- **New Telegram command:** In **run_telegram_listener()**, add another **@bot.message_handler(commands=["newname"])** and reply with **parse_mode="HTML"**.
- **New phase or schedule:** In **run()**, add another time check and branch (similar to Hunter vs Analyst).
- **Different bookmaker link:** Change **BET365_BASE** and/or **build_bet365_link_for_match** / **build_alert_button_rows**.
- **Log rotation:** Change **LOG_MAX_BYTES** or **LOG_BACKUP_COUNT** in main.py.

**Important gotchas:**

- **Global variables:** If a function assigns to a global (e.g. `is_paused`), put **`global name`** at the **top** of that function, before any use of the name.
- **Script directory:** `.env` and **LOG_FILE** are resolved from **`_script_dir`**; define **_script_dir** before any code that uses it (e.g. before **load_dotenv**).
- **Telegram message length:** Max 4096 characters; long tracebacks or logs are truncated (e.g. 4000 chars + “…(truncated)”).

---

---

## Changelog (2026-02-19)

- **Sheet name:** Snappi Live Tracker → **SnappiLogger** (`sheets_logger.py`)
- **70' window filters:** Replaced DA-based filter with Corners > 11, Shots > 40 (auto ignore), Red Card > 0 (veto). DA no longer used (API often returns 0).
- **Statistics:** Now fetches total_corners and red_cards from API.
- **Alert text:** Per-match stats now include Shots, Corners, DA.
- **Rejection reasons:** Exact text ("Shots > 40", "Corners > 11", "Red Card (veto)").
- **Post-alert Google veto:** After each alert, Gemini checks events asynchronously; if advised to hold off, sends "⚠️ Hold – [reason]" to Telegram.
- **Analyst mode alert:** At 00:01, sends "Snappi entering Analyst mode" to Telegram.
- **Stake/odds UX:** Reply **SKIP** (or 0 0, NO, etc.) to clear a prompt without saving. New **/pending** command lists parlays waiting for stake/odds.
- **Session summary:** "Matches Rejected Today" → "Matches Rejected This Session".
- **test_sheet_write.py:** New script to verify SnappiLogger write. Run: `python test_sheet_write.py`.

---

## Changelog (2026-02-28)

**Labeled snaps & /accept**

- Every snap is labeled **Snap #N** (N resets each usage day at 05:00). Alerts and the Sentry reply show the number (e.g. "Snap #12 — use /accept 12 to confirm stake").
- **No auto-deduction:** When a snap is sent, the suggested stake is stored in **pending_snaps** (file: `pending_snaps.json`). Balance is **not** reduced until you run **/accept**.
- **/accept N** (or **/accept N M** for multiple): Accepts the listed snap(s) by ID, deducts their stakes from balance, removes them from pending. Reply confirms total deducted and new balance. Invalid IDs get a reply listing current pending IDs.
- Pending snaps are loaded at startup and **cleared at 05:00** with the rest of the daily reset. Snap counter is in `snap_counter.json` (date + next_id).

**API-Football odds**

- **fetch_fixture_odds(fixture_id, target_line)** calls **GET /odds?fixture={id}**. Parses bookmaker bets for Goals Over/Under, matches the target line (e.g. Under 2.5), prefers Bet365 when present. Returns decimal odds or `None`.
- **Odds cache:** In-memory, 5-minute TTL per fixture (`_odds_cache`). Cleared at 05:00.
- Odds are fetched when matches are added to the 28' or 73' queue. They feed **total_parlay_odds()** (TARGET_ODDS trigger) and are shown in alerts as **Odds: X.XX**. If the API returns no odds, **DEFAULT_ODDS** (1.25) is used.
- **ODDS_URL** = `https://v3.football.api-sports.io/odds`. Counted like other API calls.

**Forebet in alerts and Sentry**

- **forebet.py** already provided **fetch_forebet_predictions()** (Apify, 6-hour cache) and **get_forebet_for_match(home, away, ...)**. It is now wired in:
  - **`_enrich_entries_with_forebet(entries)`** runs before sending any alert (in **send_queue_alert** and **check_and_send_alert**). Uses cached predictions; for each entry attaches **forebet_summary** (e.g. "Forebet: 2-1, Over 2.5 (52%)"), **forebet_under_over**, **forebet_predicted_score**, **forebet_prob_under** / **forebet_prob_over**.
  - **Alerts:** Each match line can show **Forebet: 2-1, Over 2.5 (52%)** when a match is found.
  - **Sentry:** The Gemini Sentry prompt now includes Forebet per match and an explicit rule: *"If a match looks low-pressure in-play (low shots, 0-0 or 1-0) but Forebet predicted high-scoring (e.g. Over 2.5, or a score like 2-1, 3-1), treat that as RED or strong caution."*

**Today’s schedule (busy day / peak times)**

- **get_todays_fixtures_schedule()** calls **GET /fixtures?date=YYYY-MM-DD** (Thorold date), groups fixtures by **hour** (Thorold), returns `{ date, total, by_hour: { 12: 15, 19: 12, ... }, fetched_at }`. Cached 2 hours in memory and **today_fixtures_cache.json**. Cache cleared at 05:00.
- **/status** now includes: **Today: X fixtures total**, **Peak times:** top 3 hours (e.g. 12:00 (15), 19:00 (12)), **Live now: N** (from last poll).
- **/schedule:** Full hour-by-hour list for today plus "Live now" from last poll.
- **WEEKLY_REPORT_HOUR** is not used for schedule; schedule is used only for /status and /schedule.

**Extended sheet columns (feedback loop)**

- Sheet header (both halftime and fulltime) extended with: **League**, **Odds**, **Forebet_Summary**, **Snap_ID**, **Units**, **Stake_Dollars** (same 21-column layout for both sheets).
- **log_bet_to_sheet(..., snap_id=...)** now writes: League (from argument), Odds (from entry), Forebet_Summary (from entry), Snap_ID, and leaves Units/Stake_Dollars blank.
- When the Sentry reply is processed, **update_sentry_label(..., units=..., stake_dollars=...)** writes **Units** and **Stake_Dollars** from **pending_snaps[snap_id]** so each row has full context for analysis.
- **get_pending_sheet_rows()** tolerates rows with fewer columns (legacy rows) by requiring only the columns needed for pending resolution.
- This supports win-rate analysis by league, odds band, Forebet Over/Under, and time (via Timestamp).

**Weekly report (automated)**

- **run_weekly_report():** Loads sheet rows for the **last 7 days** via **sheets_logger.get_rows_for_date_range(start_iso, end_iso)**. Builds a text summary with **`_build_snap_rows_text()`**, calls **notifier.ask_gemini_weekly_breakdown()**. Gemini returns a short report (win/loss summary, by league, by Forebet, 2–3 suggestions). Report is sent to Telegram and appended to **optimization_log.txt**. **`_last_weekly_report_date`** is set so it runs only once per Sunday.
- Trigger: In the **Hunter** phase, when **weekday == 6** (Sunday), **hour == 8**, **minute < 5**, and ** _last_weekly_report_date != today**. Constant **WEEKLY_REPORT_HOUR = 8** (Thorold).
- **get_rows_for_date_range(start_date_iso, end_date_iso)** in sheets_logger returns all rows from both sheets whose Timestamp date is in the inclusive range.

**Daily report (on-demand)**

- **/daily:** Loads **get_todays_rows()**, builds the same row text, calls **notifier.ask_gemini_daily_breakdown()**. Replies in Telegram with a short "daily so far" summary (snap count, results so far, one-line take). No persistence.

**Auto-start (systemd)**

- **snappi.service** (in project root): Runs Snappi as user **hamilton**, **WorkingDirectory** = `/home/hamilton/Snappi`, **ExecStart** = venv `python -u main.py` (or **/usr/bin/python3 -u main.py** if no venv). **Restart=always**, **RestartSec=15**. Optional **MemoryMax** (commented out) to cap RAM on a Pi. Install: copy to **/etc/systemd/system/**, **daemon-reload**, **systemctl enable snappi**, **systemctl start snappi**.
- **snappi-sudoers:** Passwordless sudo for **hamilton** (`NOPASSWD: ALL`). Lets Snappi and PicoClaw (via Snappi) run `sudo` commands (e.g. `systemctl restart snappi`) without a password. **install-autostart.sh** copies it to **/etc/sudoers.d/snappi-sudoers** and sets mode 440.
- **install-autostart.sh:** Installs **snappi-sudoers** (if present) then copies the service file, runs daemon-reload and enable. Run from Snappi dir: `./install-autostart.sh`. To update code: **systemctl stop snappi**, edit, **systemctl start snappi**. Logs: **journalctl -u snappi -f**.

**Telegram commands (updated list)**

- **/status** — Now includes Today’s fixtures total, peak times, and Live now.
- **/schedule** — Today’s fixtures by hour.
- **/daily** — On-demand daily-so-far report (Gemini summary).
- **/accept N [M …]** — Accept snap(s) by ID; deduct stake from balance.

**Files added or referenced**

- **pending_snaps.json** — Pending snap IDs and stakes (cleared at 05:00).
- **snap_counter.json** — Usage date and next snap ID.
- **today_fixtures_cache.json** — Cached today’s schedule (2-hour TTL).
- **snappi.service** — systemd unit.
- **snappi-sudoers** — Passwordless sudo for hamilton (installed to /etc/sudoers.d by install-autostart.sh).
- **install-autostart.sh** — One-shot install of sudoers and the service.

---

*End of documentation. Update this file as you change behavior or add features so the next instance (or future you) has a single source of truth.*
