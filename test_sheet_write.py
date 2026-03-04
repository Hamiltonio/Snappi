"""
Test script: verify Snappi can write to halftime and fulltime sheets.
Run: python test_sheet_write.py
"""
import os
from datetime import datetime
from zoneinfo import ZoneInfo

_script_dir = os.path.dirname(os.path.abspath(__file__))
os.chdir(_script_dir)

import sheets_logger

THOROLD_TZ = ZoneInfo("America/Toronto")

def main():
    print(f"Testing write to '{sheets_logger.HALFTIME_SHEET}' and '{sheets_logger.FULLTIME_SHEET}'...")
    sa_path = sheets_logger.SERVICE_ACCOUNT_JSON
    print(f"  service_account.json: {'OK' if os.path.isfile(sa_path) else 'MISSING'}")

    gc = None
    try:
        from google.oauth2.service_account import Credentials
        import gspread
        creds = Credentials.from_service_account_file(sa_path, scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ])
        gc = gspread.authorize(creds)
        print("  gspread client: OK")
    except ImportError as e:
        print(f"  -> FAILED: {e}")
        print("  Run: pip install gspread google-auth")
        return
    except Exception as e:
        print(f"  -> FAILED creating client: {e}")
        return

    for sheet_name in (sheets_logger.HALFTIME_SHEET, sheets_logger.FULLTIME_SHEET):
        try:
            sh = gc.open(sheet_name)
            ws = sh.sheet1
            print(f"  Opened sheet '{sh.title}', worksheet '{ws.title}': OK")
        except Exception as e:
            print(f"  -> FAILED opening '{sheet_name}': {e}")
            print(f"\n  Fix: Create a Google Sheet named exactly '{sheet_name}', then share it")
            print("  with the service account email (see 'client_email' in service_account.json)")

    test_row = {
        "name": "Test Match vs Snappi",
        "score": "0 - 0",
        "total_shots": 4,
        "total_corners": 2,
        "fouls": 5,
        "fixture_id": 999999,
        "league": "Test League",
        "target_line": "Under 1.5",
    }

    try:
        ok = sheets_logger.log_bet_to_sheet(
            test_row,
            window="28-Minute Scan",
            league=test_row["league"],
            batch_timestamp=datetime.now(THOROLD_TZ).isoformat(),
        )
        if ok:
            print(f"  -> SUCCESS: Row appended to '{sheets_logger.HALFTIME_SHEET}'")
        else:
            print("  -> FAILED: log_bet_to_sheet returned False")
    except Exception as e:
        print(f"  -> FAILED: {e}")
        import traceback
        traceback.print_exc()

    print("\nIf it failed, check:")
    print(f"  - Google Sheets named '{sheets_logger.HALFTIME_SHEET}' and '{sheets_logger.FULLTIME_SHEET}' exist")
    print("  - Both are shared with the service account email from service_account.json")

if __name__ == "__main__":
    main()
