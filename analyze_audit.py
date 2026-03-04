"""
Analyze ALL matches from the Snappi audit (wins + losses): fetch their stats
and see if suggested filters (Red>0, Fouls>20, Corners>7, SoG>8) would've
caught losses without wrongly rejecting too many wins.
"""
import os
from pathlib import Path
from datetime import datetime, timedelta

_script_dir = Path(__file__).resolve().parent
from dotenv import load_dotenv
load_dotenv(_script_dir / ".env")

key = os.getenv("API_FOOTBALL_KEY", "").strip()
if not key:
    print("ERROR: API_FOOTBALL_KEY not set in .env")
    exit(1)

import requests

FIXTURES_URL = "https://v3.football.api-sports.io/fixtures"
STATS_URL = "https://v3.football.api-sports.io/fixtures/statistics"
headers = {"x-apisports-key": key}

# All audit matches: (home_keyword, away_keyword) -> "WON" or "LOST"
# Exclude U21 - we use "U21" not in name to skip youth
AUDIT_MATCHES = [
    (("Brest", "Marseille"), "WON"),
    (("Mainz", "Hamburger"), "WON"),
    (("Dundalk", "Drogheda"), "WON"),
    (("Gimnasia", "Gimnasia"), "WON"),  # Gimnasia M vs Gimnasia L.P.
    (("Athletic Club", "Elche"), "WON"),
    (("Rosario Central", "Talleres"), "WON"),
    (("Boca Juniors", "Racing"), "WON"),
    (("La Calera", "Nublense"), "WON"),
    (("Estrela", "Tondela"), "WON"),
    (("Estudiantes", "Sarmiento"), "WON"),
    (("2 de Mayo", "Recoleta"), "WON"),
    (("Boca Juniors", "Platense"), "WON"),
    (("Alianza Lima", "Sport Boys"), "WON"),
    (("Libertad", "Rubio"), "WON"),
    (("Orense", "LDU Quito"), "WON"),
    (("Tigres", "Pachuca"), "LOST"),
    (("Pereira", "Pasto"), "LOST"),
    (("Llaneros", "Medellin"), "LOST"),
    (("Concepcion", "Cobresal"), "LOST"),
    (("Concepción", "Cobresal"), "LOST"),
]

def matches_audit(home_name, away_name):
    home_l, away_l = home_name.lower(), away_name.lower()
    if "u21" in home_l or "u21" in away_l or "u19" in home_l or "u19" in away_l:
        return None
    for (a, b), result in AUDIT_MATCHES:
        if (a.lower() in home_l and b.lower() in away_l) or (a.lower() in away_l and b.lower() in home_l):
            return result
    return None

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

def would_reject(stats, filters):
    if not stats:
        return []
    flags = []
    if filters.get("red") and (stats.get("Red Cards") or 0) > 0:
        flags.append("Red>0")
    if stats.get("Fouls", 0) > filters.get("fouls", 99):
        flags.append(f"Fouls>{filters['fouls']}")
    if stats.get("Corner Kicks", 0) or 0 > filters.get("corners", 99):
        flags.append(f"Corners>{filters['corners']}")
    if stats.get("Shots on Goal", 0) > filters.get("sog", 99):
        flags.append(f"SoG>{filters['sog']}")
    return flags

today = datetime.now().date()
dates = [today - timedelta(days=i) for i in range(21)]
seen = set()
found = []
for d in dates:
    r = requests.get(FIXTURES_URL, params={"date": d.isoformat()}, headers=headers, timeout=20)
    data = r.json()
    for fix in (data.get("response") or []):
        teams = fix.get("teams") or {}
        home = (teams.get("home") or {}).get("name") or ""
        away = (teams.get("away") or {}).get("name") or ""
        result = matches_audit(home, away)
        if result is None:
            continue
        key = (home, away, d)
        if key in seen:
            continue
        fid = (fix.get("fixture") or {}).get("id")
        if fid:
            seen.add(key)
            found.append((fid, f"{home} vs {away}", result, d))

# Dedupe by match name (keep first found)
by_name = {}
for fid, name, res, d in found:
    k = name.lower()
    if k not in by_name or res == "LOST":
        by_name[k] = (fid, name, res, d)
found = list(by_name.values())

print(f"Found {len(found)} audit matches. Fetching stats...\n")
FILTERS = {"red": True, "fouls": 20, "corners": 7, "sog": 8}

rows = []
for fid, name, result, d in found:
    stats = fetch_stats(fid)
    flags = would_reject(stats, FILTERS) if stats else []
    rows.append({
        "name": name,
        "result": result,
        "stats": stats,
        "flags": flags,
        "would_reject": len(flags) > 0,
    })

wins = [r for r in rows if r["result"] == "WON"]
losses = [r for r in rows if r["result"] == "LOST"]
wins_with_stats = [r for r in wins if r["stats"]]
losses_with_stats = [r for r in losses if r["stats"]]

print("=" * 95)
print(f"{'Match':<48} {'Result':<5} {'Shots':>6} {'SoG':>5} {'Corn':>5} {'Fouls':>6} {'Red':>4}  Filters?")
print("-" * 95)
for r in rows:
    s = r["stats"]
    if s:
        corn = s.get("Corner Kicks") or 0
        line = f"{r['name'][:47]:<48} {r['result']:<5} {s.get('Total Shots',0):>6} {s.get('Shots on Goal',0):>5} {corn:>5} {s.get('Fouls',0):>6} {s.get('Red Cards') or 0:>4}  "
        if r["would_reject"]:
            line += "REJECT: " + ", ".join(r["flags"])
        else:
            line += "OK"
        print(line)
    else:
        print(f"{r['name'][:47]:<48} {r['result']:<5} (no stats)")

print("\n" + "=" * 95)
print("SUMMARY (filters: Red>0, Fouls>20, Corners>7, Shots on Goal>8)")
print("=" * 95)
losses_caught = sum(1 for r in losses_with_stats if r["would_reject"])
losses_missed = len(losses_with_stats) - losses_caught
wins_wrongly_rejected = sum(1 for r in wins_with_stats if r["would_reject"])
wins_passed = len(wins_with_stats) - wins_wrongly_rejected
print(f"  Losses: {losses_caught}/{len(losses_with_stats)} caught, {losses_missed} would still slip through")
print(f"  Wins:   {wins_wrongly_rejected}/{len(wins_with_stats)} wrongly rejected, {wins_passed} would pass")
print()
if wins_with_stats:
    pct_reject = 100 * wins_wrongly_rejected / len(wins_with_stats)
    print(f"  Trade-off: Rejecting {pct_reject:.0f}% of wins to catch {losses_caught}/{len(losses_with_stats)} losses")
