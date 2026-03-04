#!/usr/bin/env python3
"""
Test Forebet API: fetch predictions and verify match lookup.
Run: python test_forebet.py
"""
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))

import forebet

THOROLD_TZ = ZoneInfo("America/Toronto")


def main():
    print("=== Forebet pipeline test ===\n")

    # 1. Fetch predictions (force refresh to hit Apify)
    print("1. Fetching predictions from Apify (force_refresh=True)...")
    predictions = forebet.fetch_forebet_predictions(force_refresh=True)
    print(f"   Fetched {len(predictions)} matches.\n")

    if not predictions:
        print("   FAIL: No predictions returned. Check APIFY_TOKEN and Apify credits.")
        return 1

    # 2. Search for Udinese vs Fiorentina (Serie A)
    home, away = "Udinese", "Fiorentina"
    league = "Serie A"
    today = datetime.now(THOROLD_TZ).strftime("%Y-%m-%d")

    print(f"2. Searching for {home} vs {away} (league={league}, date={today})...")
    fb = forebet.get_forebet_for_match(home, away, match_date=today, league=league, predictions=predictions)
    if fb:
        pred = fb.get("predictedScore", "?")
        uo = fb.get("underOverPrediction", "?")
        prob_u = fb.get("probability_under_percent", "?")
        prob_o = fb.get("probability_over_percent", "?")
        print(f"   FOUND: predictedScore={pred}, underOver={uo}, prob_under={prob_u}%, prob_over={prob_o}%")
        print(f"   Forebet summary for snap: Forebet: {pred}, {uo} ({prob_u or prob_o}%)")
    else:
        print("   NOT FOUND. Trying without league filter...")
        fb = forebet.get_forebet_for_match(home, away, match_date=today, predictions=predictions)
        if fb:
            print(f"   FOUND (no league): {fb.get('predictedScore')}, {fb.get('underOverPrediction')}")
        else:
            print("   Still not found. Checking cache for similar team names...")
            for m in predictions[:50]:
                h = m.get("home", "")
                a = m.get("away", "")
                if "udinese" in (h or "").lower() or "fiorentina" in (a or "").lower() or "udinese" in (a or "").lower() or "fiorentina" in (h or "").lower():
                    print(f"   Sample: {h} vs {a} (league={m.get('leagueName')}, date={m.get('matchDate')})")

    # 3. Verify lookup works for a match we KNOW is in cache
    if predictions:
        sample = predictions[0]
        sh, sa = sample.get("home", ""), sample.get("away", "")
        sd = sample.get("matchDate", "")
        print(f"\n3. Verifying lookup for a match IN cache: {sh} vs {sa} ({sd})...")
        found = forebet.get_forebet_for_match(sh, sa, match_date=sd, predictions=predictions)
        if found:
            print(f"   OK: Lookup works. Got {found.get('underOverPrediction')}, {found.get('probability_under_percent')}%")
        else:
            print("   FAIL: Could not find match that is in cache.")

    # 4. Sample a few from cache
    print("\n4. Sample matches in cache (first 3):")
    for i, m in enumerate(predictions[:3]):
        print(f"   {i+1}. {m.get('home')} vs {m.get('away')} | {m.get('leagueName')} | {m.get('matchDate')} | {m.get('underOverPrediction')}")

    print("\n=== Test complete ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
