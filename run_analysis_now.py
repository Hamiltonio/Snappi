"""
Run nightly analysis now (on the spreadsheet).
Use this to resolve PENDING rows, update outcomes, send nightly + daily summary, and run self-optimization.

  python run_analysis_now.py

Requires: .env (API_FOOTBALL_KEY, GEMINI_API_KEY, TELEGRAM_*), service_account.json, halftime + fulltime sheets.
"""
import os
import sys

_script_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _script_dir)
os.chdir(_script_dir)

# Load env before importing main (main loads dotenv too)
from dotenv import load_dotenv
load_dotenv(os.path.join(_script_dir, ".env"))

import main

if __name__ == "__main__":
    print("[Snappi] Loading state...")
    main._load_balance()
    main._load_soul()
    main._load_memory()
    main._load_day_start_balance()
    main.init_persistent_data()
    print("[Snappi] Running nightly analysis (spreadsheet PENDING rows)...")
    main.nightly_analysis()
    print("[Snappi] Done. Check Telegram for nightly summary and daily summary.")
