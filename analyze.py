"""
Snappi Audit Analyzer: Fetch stats for audit matches (wins + losses) and test
filter thresholds to find the best balance—catch losses without wrongly
rejecting too many wins.

Run: python analyze.py
  Scans last 21 days for audit matches, fetches stats, prints table + threshold sweep.
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

AUDIT_MATCHES = [
    (("Brest", "Marseille"), "WON"),
    (("Mainz", "Hamburger"), "WON"),
    (("Dundalk", "Drogheda"), "WON"),
    (("Gimnasia", "Gimnasia"), "WON"),
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


def would_reject(stats, fouls_max, corners_max, sog_max, red_veto):
    if not stats:
        return False, []
    flags = []
    if red_veto and (stats.get("Red Cards") or 0) > 0:
        flags.append("Red>0")
    if stats.get("Fouls", 0) > fouls_max:
        flags.append(f"Fouls>{fouls_max}")
    if (stats.get("Corner Kicks") or 0) > corners_max:
        flags.append(f"Corners>{corners_max}")
    if stats.get("Shots on Goal", 0) > sog_max:
        flags.append(f"SoG>{sog_max}")
    return len(flags) > 0, flags


# Fetch audit matches
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

by_name = {}
for fid, name, res, d in found:
    k = name.lower()
    if k not in by_name or res == "LOST":
        by_name[k] = (fid, name, res, d)
found = list(by_name.values())

print(f"Found {len(found)} audit matches. Fetching stats...\n")
rows = []
for fid, name, result, d in found:
    stats = fetch_stats(fid)
    rows.append({"name": name, "result": result, "stats": stats})

wins_with_stats = [r for r in rows if r["result"] == "WON" and r["stats"]]
losses_with_stats = [r for r in rows if r["result"] == "LOST" and r["stats"]]
n_wins = len(wins_with_stats)
n_losses = len(losses_with_stats)

print("=" * 95)
print(f"{'Match':<48} {'Result':<5} {'Shots':>6} {'SoG':>5} {'Corn':>5} {'Fouls':>6} {'Red':>4}")
print("-" * 95)
for r in rows:
    s = r["stats"]
    if s:
        corn = s.get("Corner Kicks") or 0
        print(f"{r['name'][:47]:<48} {r['result']:<5} {s.get('Total Shots',0):>6} {s.get('Shots on Goal',0):>5} {corn:>5} {s.get('Fouls',0):>6} {s.get('Red Cards') or 0:>4}")
    else:
        print(f"{r['name'][:47]:<48} {r['result']:<5} (no stats)")

print("\n" + "=" * 95)
print("THRESHOLD SWEEP: Find best balance (catch losses, avoid rejecting wins)")
print("=" * 95)
print(f"{'Threshold':<30} {'Losses Caught':<14} {'Wins Rejected':<14} {'Win% Pass':<10} {'Score':<8}")
print("-" * 95)

best = None
best_score = -1

# Test various combinations (red_veto=True always)
for fouls in [99, 30, 25, 22, 20]:
    for corners in [99, 12, 11, 10, 9, 8, 7]:
        for sog in [99, 12, 11, 10, 9, 8]:
            if fouls == 99 and corners == 99 and sog == 99:
                continue
            losses_caught = sum(1 for r in losses_with_stats if would_reject(r["stats"], fouls, corners, sog, True)[0])
            wins_rejected = sum(1 for r in wins_with_stats if would_reject(r["stats"], fouls, corners, sog, True)[0])
            wins_pass = n_wins - wins_rejected
            win_pct = 100 * wins_pass / n_wins if n_wins else 0
            # Score: prioritize catching losses, then passing wins
            score = losses_caught * 10 + wins_pass
            label = f"F>{fouls} C>{corners} SoG>{sog}".replace(">99", "off")
            if fouls == 99:
                label = f"C>{corners} SoG>{sog}".replace(">99", "off")
            if corners == 99 and sog == 99:
                label = f"F>{fouls}"
            print(f"{label:<30} {losses_caught}/{n_losses:<10} {wins_rejected}/{n_wins:<10} {win_pct:.0f}%        {score}")
            if losses_caught >= n_losses and score > best_score:
                best_score = score
                best = (fouls, corners, sog, losses_caught, wins_rejected)

print("\n" + "=" * 95)
print("BEST BALANCE (catches all losses, maximizes wins passing):")
if best:
    f, c, s, lc, wr = best
    print(f"  Fouls>{f}  Corners>{c}  Shots on Goal>{s}  (Red>0 always)")
    print(f"  Losses: {lc}/{n_losses} caught | Wins: {n_wins - wr}/{n_wins} pass ({100*(n_wins-wr)/n_wins:.0f}%)")
else:
    print("  No single threshold catches all losses while keeping wins. Try Corners>10 or Corners>11.")
print("=" * 95)
