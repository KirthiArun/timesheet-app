# ============================================================
# sync_shows_from_sheet.py
# Reads the _ShowList tab from the Internal Master sheet
# and upserts shows into SQLite or PostgreSQL.
# ============================================================

import os, base64, json, sqlite3
from dotenv import load_dotenv
load_dotenv()

from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials

try:
    import psycopg
except ImportError:
    psycopg = None

DATABASE_URL   = os.environ.get("DATABASE_URL", "")
MASTER_SHEET_ID = "1ENTP8XLLoFISNwLvF5Z7mM6X-ycYcOAERvcyoQ-dqhk"
SHOW_LIST_TAB   = "_ShowList"
DB_NAME         = "timesheet.db"
SPECIAL_LABELS  = {"vacation", "holiday", "other"}
SCOPES          = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

print(f"[SyncShows] DATABASE_URL set: {bool(DATABASE_URL)}")


# ── DB connection ─────────────────────────────────────────────────────────────

def get_sync_db(db_name=DB_NAME):
    if DATABASE_URL:
        conn = psycopg.connect(DATABASE_URL, row_factory=psycopg.rows.dict_row)
        return conn
    conn = sqlite3.connect(db_name)
    conn.row_factory = sqlite3.Row
    return conn


# ── Sheets service ────────────────────────────────────────────────────────────

def get_sheets_service():
    b64 = os.environ.get("GOOGLE_CREDENTIALS_B64", "")
    if b64:
        info  = json.loads(base64.b64decode(b64).decode("utf-8"))
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    else:
        creds = Credentials.from_service_account_file("service_account.json", scopes=SCOPES)
    return build("sheets", "v4", credentials=creds)


# ── Fetch from sheet ──────────────────────────────────────────────────────────

def fetch_shows_from_showlist():
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

        if entry.lower() in SPECIAL_LABELS:
            shows.append(("GENERAL", entry.upper(), entry))
        else:
            parts = [p.strip() for p in entry.split(" - ", 2)]
            if len(parts) == 3:
                shows.append((parts[0], parts[1], parts[2]))
            elif len(parts) == 2:
                shows.append((parts[0], parts[1], parts[1]))
            else:
                shows.append(("GENERAL", entry[:50], entry))

    print(f"[SyncShows] Read {len(shows)} entries from _ShowList tab")
    return shows


# ── Upsert to DB ──────────────────────────────────────────────────────────────

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
            for wc in cur.fetchall():
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
                try:
                    cur.execute("""
                        INSERT OR IGNORE INTO show_work_codes (show_id, work_code_id)
                        VALUES (?, ?)
                    """, (show_id, wc["work_code_id"]))
                except Exception:
                    pass

        added += 1

    conn.commit()
    cur.close()
    conn.close()
    print(f"[SyncShows] {added} new show(s) added to DB")
    return added


# ── Main entry ────────────────────────────────────────────────────────────────

def sync_shows(db_name=DB_NAME):
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
