"""
One-off script: load API key from .env and call the live fixtures endpoint.
Run from the Snappi folder:  python check_live_api.py
"""
import os
import json
from pathlib import Path

from dotenv import load_dotenv

_script_dir = Path(__file__).resolve().parent
load_dotenv(_script_dir / ".env")
key = os.getenv("API_FOOTBALL_KEY", "").strip()

if not key:
    print("ERROR: API_FOOTBALL_KEY not set in .env")
    exit(1)

import requests

url = "https://v3.football.api-sports.io/fixtures"
params = {"live": "all"}
headers = {"x-apisports-key": key}

print("Calling live fixtures API (key from .env)...")
r = requests.get(url, params=params, headers=headers, timeout=20)
data = r.json()
count = len(data.get("response") or [])
errors = data.get("errors") or data.get("message") or "(none)"
print(f"Status: {r.status_code}")
print(f"Live fixtures returned: {count}")
print(f"Errors/message: {errors}")
if count > 0:
    leagues = set()
    for m in data["response"][:20]:
        league = (m.get("league") or {}).get("name") or (m.get("league") or {}).get("country") or "?"
        if league != "?":
            leagues.add(league)
    print(f"Leagues (sample): {', '.join(sorted(leagues)[:10])}")
else:
    print("Full response keys:", list(data.keys()))
