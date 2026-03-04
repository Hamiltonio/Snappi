"""
Run Snappi in simulation mode: yesterday's Champions League data, 3:45 PM (70' window).

1. Force time to 3:45 PM Thorold so we're in Hunter phase.
2. Use simulation_data for live fixtures and stats (Olympiacos & Brugge at 70', low DA/Shots).
3. Process once so both fixtures enter the 70' queue; force-send to trigger Telegram alert(s).
4. Run nightly_analysis() with simulation final scores (0-2, 3-3): Brugge counts as loss, Gemini analyzes, summary to Telegram.

Usage: python run_simulation.py
"""

import main
from main import (
    THOROLD_TZ,
    process_live_matches,
    send_queue_alert,
    nightly_analysis,
)
from datetime import datetime
import simulation_data


def _mock_thorold_1545():
    """Fix time at 3:45 PM so we're in Hunter phase and in the 70' window."""
    return datetime(2025, 2, 18, 15, 45, 0, tzinfo=THOROLD_TZ)


def main_simulation():
    # Patch to use simulation data and fixed time
    original_fetch_live = main.fetch_live_fixtures
    original_fetch_stats = main.fetch_fixture_statistics
    original_fetch_result = main.fetch_fixture_result
    original_thorold_now = main.get_thorold_now

    main.fetch_live_fixtures = lambda: simulation_data.get_live_fixtures_simulation()
    main.fetch_fixture_statistics = simulation_data.get_fixture_statistics_simulation
    main.fetch_fixture_result = simulation_data.get_fixture_result_simulation
    main.get_thorold_now = _mock_thorold_1545

    try:
        # Clear 70' queue so we start fresh
        main.flagged_70.clear()
        main.queue_70_started_at = None

        print("[Simulation] Time set to 3:45 PM. Processing two 70' fixtures (Olympiacos, Brugge)...")
        process_live_matches()

        # Both fixtures should be in flagged_70; force-send to trigger Telegram (180s timer / safety net)
        if main.flagged_70:
            print("[Simulation] Force-sending 70' queue to Telegram...")
            send_queue_alert(main.flagged_70, "70-Minute Scan")
        else:
            print("[Simulation] WARNING: No matches in 70' queue. Check simulation data.")

        print("[Simulation] Running nightly_analysis() (final 0-2, 3-3 → Brugge loss, Gemini)...")
        nightly_analysis()
        print("[Simulation] Done. Check Telegram for alert and nightly summary with Gemini's Brugge loss reason.")
    finally:
        # Restore originals
        main.fetch_live_fixtures = original_fetch_live
        main.fetch_fixture_statistics = original_fetch_stats
        main.fetch_fixture_result = original_fetch_result
        main.get_thorold_now = original_thorold_now


if __name__ == "__main__":
    main_simulation()
