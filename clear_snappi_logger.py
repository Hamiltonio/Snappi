"""Clear halftime and fulltime sheet data (keeps header rows). Run once to reset both tables."""
import sheets_logger

if __name__ == "__main__":
    ok = sheets_logger.clear_sheet_data(keep_headers=True)
    print("Halftime + fulltime sheets cleared (headers kept)." if ok else "Failed to clear (check service_account.json and sheet access).")
