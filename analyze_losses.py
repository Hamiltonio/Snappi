"""
Analyze the 4 LOST matches from the audit: fetch their stats and see which
filters (red cards, fouls, corners, shots on goal) would have caught them.
Run: python analyze_losses.py [date]
  date = YYYY-MM-DD (default: tries last 7 days)
"""
import os
import sys
from pathlib import Path
from datetime import datetime, timedelta

_script_dir = Path(__file__).resolve().parent
from dotenv import load_dotenv
load_dotenv(_script_dir / ".env")

key = os.getenv("API_FOOTBALL_KEY", "").strip()
if not key:
    print("ERROR: API_FOOTBALL_KEY not set in .env")
    sys.exit(1)

import requests

FIXTURES_URL = "https://v3.football.api-sports.io/fixtures"
STATS_URL = "https://v3.football.api-sports.io/fixtures/statistics"
headers = {"x-apisports-key": key}

# Lost matches from audit (keywords to match - home/away substrings)
LOST_MATCHES = [
    ("Tigres", "Pachuca"),           # Tigres UANL vs Pachuca
    ("Pereira", "Pasto"),            # Dep. Pereira vs Dep. Pasto
    ("Llaneros", "Medellin"),        # Llaneros vs Ind. Medellin
    ("Concepción", "Cobresal"),      # Concepción vs Cobresal (note: Concepción may have accent variants)
    ("Concepcion", "Cobresal"),      # alternate spelling
]

def get_match_name(fix):
    home = (fix.get("teams") or {}).get("home") or {}
    away = (fix.get("teams") or {}).get("away") or {}
    return f"{home.get('name','?')} vs {away.get('name','?')}"

def matches_lost_pair(home_name, away_name):
    home_l, away_l = home_name.lower(), away_name.lower()
    for a, b in LOST_MATCHES:
        if (a.lower() in home_l and b.lower() in away_l) or (a.lower() in away_l and b.lower() in home_l):
            return True
    return False

def fetch_stats(fixture_id):
    r = requests.get(STATS_URL, params={"fixture": fixture_id}, headers=headers, timeout=20)
    data = r.json()
    teams_data = data.get("response") or []
    if not teams_data:
        return None
    out = {}
    for team_block in teams_data:
        for s in (team_block.get("statistics") or []):
            stype = (s.get("type") or "").strip()
            val = s.get("value")
            if val is None:
                continue
            try:
                v = int(val) if isinstance(val, (int, float)) else int(str(val).replace("%", "").split()[0])
            except (ValueError, TypeError):
                continue
            if stype not in out:
                out[stype] = 0
            out[stype] += v
    return out

# Try last 7 days
dates_to_try = []
if len(sys.argv) > 1:
    try:
        dt = datetime.strptime(sys.argv[1], "%Y-%m-%d")
        dates_to_try = [dt.date()]
    except ValueError:
        print("Usage: python analyze_losses.py [YYYY-MM-DD]")
        sys.exit(1)
else:
    today = datetime.now().date()
    dates_to_try = [today - timedelta(days=i) for i in range(14)]

found = []
for d in dates_to_try:
    r = requests.get(FIXTURES_URL, params={"date": d.isoformat()}, headers=headers, timeout=20)
    data = r.json()
    fixtures = data.get("response") or []
    for fix in fixtures:
        teams = fix.get("teams") or {}
        home = (teams.get("home") or {}).get("name") or ""
        away = (teams.get("away") or {}).get("name") or ""
        if matches_lost_pair(home, away):
            fid = (fix.get("fixture") or {}).get("id")
            if fid and not any(f[0] == fid for f in found):
                found.append((fid, f"{home} vs {away}", d))

if not found:
    print("No lost matches found in last 14 days.")
    print("Try: python analyze_losses.py 2025-02-19  (use the actual match date)")
    sys.exit(1)

print(f"Found {len(found)} lost match(es). Fetching stats...\n")
print("=" * 90)

results = []
for fid, match_name, _ in found:
    stats = fetch_stats(fid)
    if not stats:
        results.append((match_name, None))
        continue
    total_shots = stats.get("Total Shots", 0)
    shots_on = stats.get("Shots on Goal", 0)
    corners = stats.get("Corner Kicks", 0) or 0
    fouls = stats.get("Fouls", 0)
    red = stats.get("Red Cards", 0) or 0
    yellow = stats.get("Yellow Cards", 0) or 0
    results.append((match_name, {
        "Total Shots": total_shots,
        "Shots on Goal": shots_on,
        "Corner Kicks": corners,
        "Fouls": fouls,
        "Red Cards": red,
        "Yellow Cards": yellow,
    }))

# Print table
print(f"{'Match':<45} {'Shots':>6} {'SoG':>5} {'Corn':>5} {'Fouls':>6} {'Red':>4}")
print("-" * 90)
for name, s in results:
    if s is None:
        print(f"{name:<45} (no stats)")
        continue
    print(f"{name[:44]:<45} {s['Total Shots']:>6} {s['Shots on Goal']:>5} {s['Corner Kicks']:>5} {s['Fouls']:>6} {s['Red Cards']:>4}")

# Filter analysis
print("\n" + "=" * 90)
print("FILTER ANALYSIS: Would these have caught the losses?")
print("=" * 90)
for name, s in results:
    if s is None:
        continue
    flags = []
    if (s["Red Cards"] or 0) > 0:
        flags.append("RED CARD")
    if s["Fouls"] > 20:  # example broad threshold
        flags.append(f"Fouls>{20}")
    if s["Corner Kicks"] > 10:
        flags.append(f"Corners>{10}")
    if s["Corner Kicks"] > 7:
        flags.append(f"Corners>{7}")
    if s["Shots on Goal"] > 10:
        flags.append(f"ShotsOnGoal>{10}")
    if s["Shots on Goal"] > 8:
        flags.append(f"ShotsOnGoal>{8}")
    if flags:
        print(f"  {name[:50]}")
        print(f"    -> {', '.join(flags)}")
    else:
        print(f"  {name[:50]}")
        print(f"    -> No red flags at suggested thresholds (Red>0, Fouls>20, Corners>7/10, SoG>8/10)")
