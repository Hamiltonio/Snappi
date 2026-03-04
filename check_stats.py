"""
Debug script: fetch statistics for a live fixture and print all stat types the API returns.
Use this to verify which stats (Dangerous Attacks, Total Shots, Corners, etc.) are available.
Run: python check_stats.py [fixture_id]
  - No args: fetch live fixtures, pick first one, get its stats
  - fixture_id: get stats for that fixture directly
"""
import os
import sys
from pathlib import Path

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

fixture_id = None
if len(sys.argv) > 1:
    try:
        fixture_id = int(sys.argv[1])
    except ValueError:
        print("Usage: python check_stats.py [fixture_id]")
        sys.exit(1)

if fixture_id is None:
    print("Fetching live fixtures...")
    r = requests.get(FIXTURES_URL, params={"live": "all"}, headers=headers, timeout=20)
    data = r.json()
    fixtures = data.get("response") or []
    if not fixtures:
        print("No live fixtures. Pass a fixture_id: python check_stats.py 12345")
        sys.exit(1)
    fixture_id = fixtures[0].get("fixture", {}).get("id")
    teams = fixtures[0].get("teams", {})
    home = (teams.get("home") or {}).get("name", "?")
    away = (teams.get("away") or {}).get("name", "?")
    print(f"Using first live match: {home} vs {away} (fixture_id={fixture_id})")
else:
    print(f"Fetching stats for fixture_id={fixture_id}...")

r = requests.get(STATS_URL, params={"fixture": fixture_id}, headers=headers, timeout=20)
data = r.json()
teams_data = data.get("response") or []

if not teams_data:
    print("No statistics returned. Response:", list(data.keys()))
    if data.get("errors"):
        print("Errors:", data["errors"])
    sys.exit(1)

print("\n=== RAW STAT TYPES FROM API (per team) ===\n")
seen_types = set()
for i, team_block in enumerate(teams_data):
    team_name = (team_block.get("team") or {}).get("name", f"Team {i+1}")
    stats = team_block.get("statistics") or []
    print(f"--- {team_name} ({len(stats)} stats) ---")
    for s in stats:
        stype = (s.get("type") or "?").strip()
        val = s.get("value", "?")
        seen_types.add(stype)
        print(f"  {stype!r} = {val}")
    print()

print("=== ALL UNIQUE STAT TYPES (across both teams) ===")
for t in sorted(seen_types):
    print(f"  {t!r}")
print()
print("Look for: 'Total Shots', 'Shots on Goal', 'Dangerous Attacks', 'Corners', 'Cross Attacks', etc.")
print("If 'Dangerous Attacks' is missing, that explains the DA=0 glitch.")
