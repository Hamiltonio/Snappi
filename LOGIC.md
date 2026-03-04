# Snappi – Definitive Logic (single source of truth)

Use this document as the authority for windows, guards, sheet, and money rules. Code and sheets must match this.

---

## 1. Time windows and when we fire

- **Window 1 (first half)**  
  - **Queue window:** 25–28 minutes (elapsed). Any match that passes the filters in this 3-minute span is added to the queue.  
  - **Fire:** Send the Telegram alert at **28 minutes** (one alert per batch collected in 25–28).

- **Window 2 (second half)**  
  - **Queue window:** 70–73 minutes (elapsed). Matches that pass the filters in this 3-minute span are added to the queue.  
  - **Fire:** Send the Telegram alert at **73 minutes** (one alert per batch collected in 70–73).

- **Latency:** Send the alert **immediately** when we fire (no extra delay). Gemini Sentry runs in the background and **replies** to that alert with the traffic-light label and narrative.

---

## 2. The guards (filters and Sentry)

- **Dynamic line (all windows)**  
  - No hardcoded “Under 2.5”.  
  - **Line = Current total goals + 1.5** (e.g. 0–0 → Under 1.5; 1–1 → Under 3.5).  
  - Shown in alerts and sheet as “Target Line” / “Line”.

- **Window 2 (70–73) only – stat guards (can force RED or narrative)**  
  - **Shot kill-switch:** If Shots > 25 at 70′, treat as **RED** (Strong Avoid).  
  - **Corner guard:** If Corners > 10 at 70′, treat as **RED**.  
  - **Foul indicator:** If Fouls > 15 at 70′, add “High Extra Time Risk” to the analysis narrative.  
  - **Favorite trailing:** If the pre-match favorite is trailing at 70′, treat as **RED** (needs odds/predictions source for “favorite”).

- **Sentry (Gemini)**  
  - Runs **after** the alert is sent.  
  - **Replies** to the alert message with:  
    - **Traffic light:** RED / YELLOW / GREEN.  
    - Short narrative.  
  - Stat guards above can set RED; Sentry can add RED/YELLOW/GREEN and narrative on top.

- **DA removed**  
  - No Dangerous Attacks anywhere (code, sheet, or alerts).

---

## 3. Sheet (Google Sheets – SnappiLogger)

- **Backend:** Google Sheets only (no .xlsx as live data). Sheet name: **SnappiLogger**.
- **Column order (exact):**  
  `Timestamp, Match, Window, Shots, Corners, Fouls, Score, Target Line, Final Score, Status, Result, Gemini_Label, Gemini_Analysis, Fixtures_ID, Stake, Odds, Profit_Loss`  
  (Stake/Odds/Profit_Loss may be derived from balance/units – see money rules.)
- **Real-time resolution:** An async loop (or periodic task) checks fixture status. When a fixture hits **FT**, update that row’s Final Score, Result (WIN/LOSS), and Profit_Loss. Do **not** wait for “Analysis Mode” – update as soon as FT is known.

---

## 4. Money: balance and units (definitive)

- **Balance**  
  - One number: “How much is in your betting account right now?”  
  - Asked at start of day or when Snappi launches (once).  
  - **Each snap:** Snappi assumes you placed the suggested stake and deducts it from balance.
- Use **/updatebalance** to correct (e.g. you didn’t place that snap, or you won and added winnings).

- **Units**  
  - Your balance is always divided into **4 units**.  
  - **1 unit = Balance ÷ 4.**  
  - Example: $100 balance → 1 unit = $25.

- **Stake per slip (by Sentry label)**  
  - **RED:** 1 unit (e.g. $25 if balance $100).  
  - **YELLOW:** 2 units (e.g. $50).  
  - **GREEN:** 3 units (e.g. $75).  
  - We **never** stake 4 units on one snap, so you always have at least 1 unit left for other snaps while waiting for results.

- **In the alert**  
  - Show suggested stake in dollars, e.g. “Use $25 on this (1 unit · RED)” or “Use $75 on this (3 units · GREEN)”.  
  - No per-snap “reply with stake and odds” prompt. Stake is derived from balance and label only.

- **P&L**  
  - Profit/Loss is computed from the suggested stake (units × unit size) and the result (WIN/LOSS), using the bet’s effective odds where available (or a placeholder if we don’t store odds).

---

## 5. Summary table

| Item | Rule |
|------|------|
| Window 1 queue | 25–28 min |
| Window 1 fire | 28 min |
| Window 2 queue | 70–73 min |
| Window 2 fire | 73 min |
| Line | Current total goals + 1.5 |
| Shots > 25 @ 70′ | RED |
| Corners > 10 @ 70′ | RED |
| Fouls > 15 @ 70′ | Add “High Extra Time Risk” to narrative |
| Favorite trailing @ 70′ | RED (when we have favorite) |
| DA | Removed everywhere |
| Balance | Asked once at start; /updatebalance to change |
| 1 unit | Balance ÷ 4 |
| RED stake | 1 unit |
| YELLOW stake | 2 units |
| GREEN stake | 3 units |
| Sheet | Google Sheets, SnappiLogger, columns as above |
| FT updates | As soon as fixture is FT (async), not only in Analysis Mode |

---

*When in doubt, this file wins. Code and docs should be updated to match LOGIC.md.*
