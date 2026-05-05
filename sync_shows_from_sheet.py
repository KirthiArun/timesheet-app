# ============================================================
# sync_shows_from_sheet.py
# Reads the _ShowList tab from the Internal Master sheet
# and upserts shows into SQLite.
#
# _ShowList format (one entry per row, col A):
#   "2026 - 26A01 - COSMOPROF NORTH AMERICA MIAMI 2026"
#   "Vacation"
#   "Holiday"
#   "Other"
#
# Called at app startup and via /admin/sync-shows route.
# ============================================================

from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials
import os, base64, json
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

# REPLACE WITH
import sqlite3, os
from dotenv import load_dotenv
load_dotenv()

try:
    import psycopg
except ImportError:
    psycopg = None

DATABASE_URL = os.environ.get("DATABASE_URL", "")

# Debug
print(f"[SyncShows] DATABASE_URL set: {bool(DATABASE_URL)}")

def get_sync_db(db_name):
    if DATABASE_URL:
        conn = psycopg.connect(DATABASE_URL, row_factory=psycopg.rows.dict_row)
        return conn
    conn = sqlite3.connect(db_name)
    conn.row_factory = sqlite3.Row
    return conn


SERVICE_ACCOUNT_FILE = "service_account.json"
MASTER_SHEET_ID      = "1ENTP8XLLoFISNwLvF5Z7mM6X-ycYcOAERvcyoQ-dqhk"
SHOW_LIST_TAB        = "_ShowList"
DB_NAME              = "timesheet.db"

# Special entries that have no year/code — stored as GENERAL
SPECIAL_LABELS = {"vacation", "holiday", "other"}

SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]


def get_sheets_service():
    b64 = os.environ.get("GOOGLE_CREDENTIALS_B64", "")
    if b64:
        info  = json.loads(base64.b64decode(b64).decode("utf-8"))
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    else:
        # Local fallback — reads from file
        creds = Credentials.from_service_account_file("service_account.json", scopes=SCOPES)
    return build("sheets", "v4", credentials=creds)



def fetch_shows_from_showlist():
    """
    Reads col A of _ShowList tab.
    Each row is either:
      "YEAR - JobNo - ShowName"  →  parsed into (year, show_code, show_name)
      "Vacation" / "Holiday" / "Other"  →  stored as ("GENERAL", label.upper(), label)
    Returns list of (year, show_code, show_name).
    """
    service = get_sheets_service()
    result  = service.spreadsheets().values().get(
        spreadsheetId=MASTER_SHEET_ID,
        range=f"'{SHOW_LIST_TAB}'!A:A"
    ).execute()

    rows  = result.get("values", [])
    shows = []

    for row in rows:
        if not row:
            continue
        entry = row[0].strip()
        if not entry:
            continue

        label_lower = entry.lower()

        if label_lower in SPECIAL_LABELS:
            # Vacation / Holiday / Other
            shows.append(("GENERAL", entry.upper(), entry))
        else:
            # Expected format: "2026 - 26A01 - COSMOPROF NORTH AMERICA MIAMI 2026"
            parts = [p.strip() for p in entry.split(" - ", 2)]
            if len(parts) == 3:
                year, show_code, show_name = parts
                shows.append((year, show_code, show_name))
            elif len(parts) == 2:
                # Missing show name — use show_code as name
                year, show_code = parts
                shows.append((year, show_code, show_code))
            else:
                # Unrecognised format — store as-is under GENERAL
                shows.append(("GENERAL", entry[:50], entry))

    print(f"[SyncShows] Read {len(shows)} entries from _ShowList tab")
    return shows


def upsert_shows_to_db(shows, db_name=DB_NAME):
    conn  = get_sync_db(db_name)
    cur   = conn.cursor()
    added = 0

    for year, show_code, show_name in shows:
        if DATABASE_URL:
            cur.execute("""
                SELECT show_id FROM shows
                WHERE year = %s AND show_code = %s AND show_name = %s
            """, (year, show_code, show_name))
        else:
            cur.execute("""
                SELECT show_id FROM shows
                WHERE year = ? AND show_code = ? AND show_name = ?
            """, (year, show_code, show_name))

        if cur.fetchone():
            continue

        if DATABASE_URL:
            cur.execute("""
                INSERT INTO shows (year, show_code, show_name, active_flag)
                VALUES (%s, %s, %s, 'Y') RETURNING show_id
            """, (year, show_code, show_name))
            show_id = cur.fetchone()["show_id"]
            cur.execute("SELECT work_code_id FROM work_codes")
            work_codes = cur.fetchall()
            for wc in work_codes:
                try:
                    cur.execute("""
                        INSERT INTO show_work_codes (show_id, work_code_id)
                        VALUES (%s, %s) ON CONFLICT DO NOTHING
                    """, (show_id, wc["work_code_id"]))
                except Exception:
                    pass
        else:
            cur.execute("""
                INSERT INTO shows (year, show_code, show_name, active_flag)
                VALUES (?, ?, ?, 'Y')
            """, (year, show_code, show_name))
            show_id = cur.lastrowid
            cur.execute("SELECT work_code_id FROM work_codes")
            for wc in cur.fetchall():
                cur.execute("""
                    INSERT OR IGNORE INTO show_work_codes (show_id, work_code_id)
                    VALUES (?, ?)
                """, (show_id, wc["work_code_id"]))

        added += 1

    conn.commit()
    cur.close()
    conn.close()
    print(f"[SyncShows] {added} new show(s) added to DB")
    return added


def sync_shows(db_name=DB_NAME):
    """
    Main entry — fetch from _ShowList and upsert to SQLite.
    Returns (added_count, error_message or None)
    """
    try:
        shows = fetch_shows_from_showlist()
        added = upsert_shows_to_db(shows, db_name)
        return added, None
    except Exception as e:
        print(f"[SyncShows] ❌ Error: {e}")
        return 0, str(e)


if __name__ == "__main__":
    added, err = sync_shows()
    if err:
        print(f"❌ Sync failed: {err}")
    else:
        print(f"✅ Done — {added} new show(s) added from _ShowList")