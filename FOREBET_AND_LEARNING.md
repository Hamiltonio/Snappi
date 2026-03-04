# Forebet API & Score-Times Learning — Implementation Guide

## Part 1: Forebet API Integration

### Overview

**Forebet** provides mathematical predictions (1X2, Under/Over, Halftime, BTTS, Corners, Cards). No official API — use **Apify** scraper `locos08/forebet-predictions-scraper`.

**Cost:** ~$0.10 per 1,000 results. Snappi might use 500–2000 matches/day → ~$0.05–0.20/day.

### Setup

1. **Apify account:** https://apify.com (free tier available)
2. **Get API token:** Apify Console → Settings → Integrations → API Token
3. **Add to `.env`:**
   ```
   APIFY_TOKEN=apify_api_xxxxxxxxxxxx
   ```

4. **Install client:**
   ```bash
   pip install apify-client
   ```

### Forebet Data Schema (relevant fields)

| Field | Use for Snappi |
|-------|----------------|
| `home`, `away` | Match identification (fuzzy match to API-Football) |
| `matchDate`, `matchTime` | When match starts |
| `underOverPrediction` | "Over" or "Under" 2.5 — **key for our Under bets** |
| `probability_under_percent`, `probability_over_percent` | Confidence |
| `halftimePrediction` | For 28' window |
| `averageGoals`, `predictedAverageCorners` | Extra signal |
| `finalScore`, `halftimeScore` | For finished matches (learning) |

### Matching Forebet ↔ API-Football

Team names differ (e.g. "Man City" vs "Manchester City"). Options:

- **Fuzzy match:** `rapidfuzz` or `difflib` on `home`/`away`
- **League + date + time:** Narrow by league and kickoff, then match teams
- **Fixture ID:** Forebet has no fixture ID; we match by `home`, `away`, `matchDate`

### When to Call Forebet

| Option | Pros | Cons |
|--------|------|------|
| **A) On each snap (before send)** | Real-time check | Extra latency (~10–30s), API cost per snap |
| **B) Daily batch (e.g. 06:00)** | One call, cache all day | Stale for late kickoffs |
| **C) Per live fixture (when we flag)** | Only when needed | Need to match live fixture to Forebet row |

**Recommendation:** **C** — When we add a match to `flagged_30` or `flagged_70`, call Forebet (or lookup from a morning cache) for that match. If Forebet says "Under" and probability_under > 55%, treat as a confidence boost.

### Implementation Sketch

```
1. forebet.py
   - fetch_forebet_predictions() → runs Apify actor, returns list of matches
   - get_forebet_for_match(home, away, date) → fuzzy match, return underOverPrediction + probs
   - Cache results in forebet_cache.json (TTL 6h?) to avoid repeated Apify calls

2. main.py — in build_match_entry or before adding to queue
   - Optional: if APIFY_TOKEN set, call get_forebet_for_match()
   - Add forebet_under, forebet_prob_under to match entry
   - Sentry / alert could mention "Forebet agrees: Under 2.5 (62%)"

3. .env
   - APIFY_TOKEN=...
```

### Open Questions

- **Caching:** Run Forebet once at 06:00 and cache, or call on-demand when we flag?
- **Matching:** How strict should team-name matching be? (Leagues vary.)
- **Integration point:** Pre-filter (don't flag if Forebet says Over)? Or post-filter (boost confidence, show in alert)?
- **Cost control:** Set a daily Apify budget or max runs?

---

## Part 2: Score Times for Self-Reflection

### Idea

Store **goal times** for every resolved snap. Over time this builds a dataset for learning:

- "When we flagged Under at 70' with 1–0, 73% of our losses had goals in 75–90'"
- "Halftime snaps: goals in 45+2' and 60–70' are the danger zones"
- "League X: late goals more common than League Y"

### Data Source

**API-Football Events** — we already call `fetch_fixture_events()`. Events include:

- `time.elapsed` (minute)
- `type` (e.g. "Goal")
- `detail` (e.g. "Normal Goal", "Penalty")
- `team.name` (home/away)

### Schema for `snap_outcomes.json`

```json
{
  "outcomes": [
    {
      "fixture_id": 12345,
      "teams": "Team A vs Team B",
      "window": "73-Minute Scan",
      "score_at_snap": "1 - 0",
      "target_line": "Under 2.5",
      "final_score": "2 - 1",
      "result": "LOSS",
      "goal_times": [
        {"minute": 23, "team": "home", "type": "Normal Goal"},
        {"minute": 67, "team": "away", "type": "Normal Goal"},
        {"minute": 89, "team": "away", "type": "Normal Goal"}
      ],
      "resolved_at": "2025-02-28T00:15:00-05:00"
    }
  ]
}
```

### Implementation Sketch

```
1. Extract goal times from events
   - New: extract_goal_times(events) → list of {minute, team, type}
   - Reuse fetch_fixture_events (we already call it for loss analysis)

2. When resolving a snap (check_pending_ft_resolution + nightly_analysis)
   - Fetch events
   - Extract goal_times
   - Append to snap_outcomes.json (or similar)

3. Nightly self-optimization
   - Include recent outcomes + goal_times in the prompt to Gemini
   - "Here are the last 30 resolved snaps with goal times. What patterns do you see?"
   - Gemini suggests threshold tweaks or "avoid 70' snaps when score is 1-1 in League X"

4. Optional: aggregate stats
   - Build histograms: "Goals in losses by minute bucket (0-15, 15-30, ...)"
   - Feed into a simple rule: "If 60%+ of losses have goals in 80-90', consider earlier windows"
```

### Open Questions

- **Storage:** Single `snap_outcomes.json` (append) or one file per day? (Single file grows; rotate at 5 AM?)
- **Retention:** Keep last N outcomes (e.g. 500) or all-time?
- **Gemini prompt:** How much outcome data to include? (Last 30? 50? Summarized stats?)
- **Privacy:** This stays local; no PII. Fine to keep indefinitely.

---

## Next Steps

1. **Forebet:** Create `forebet.py`, add `APIFY_TOKEN` to `.env`, decide caching + matching strategy.
2. **Score times:** Add `extract_goal_times()`, append to outcomes on resolution, wire into nightly reflection.
3. **Discussion:** Which integration point for Forebet? (Pre-filter vs confidence boost vs info-only in alert?)
