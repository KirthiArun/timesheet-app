# ============================================================
# sheets_sync.py
# Writes a developer's timesheet entries to the Internal Master sheet.
# Called on every save / edit / delete in app.py.
# ============================================================

import os, base64, json, sqlite3
from datetime import datetime
from dotenv import load_dotenv
load_dotenv()

from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials

try:
    import psycopg
except ImportError:
    psycopg = None

DATABASE_URL    = os.environ.get("DATABASE_URL", "")
MASTER_SHEET_ID = "1ENTP8XLLoFISNwLvF5Z7mM6X-ycYcOAERvcyoQ-dqhk"
DB_NAME         = "timesheet.db"

print(f"[SheetsSync] DATABASE_URL set: {bool(DATABASE_URL)}")

WORK_CODE_COLUMNS = [
    "EAAEP", "EAALU", "EACRO", "EADATST", "EADMT",
    "EAEWS", "OTDEV", "OTMIS", "OTPM",   "OTQA",
    "OTTRAIN", "PRWS", "QATEST", "SHAWS", "Vacation"
]

HEADER_ROW = [
    "Employee", "Show/Year", "Date",
    "EAAEP Task Hours", "EAALU Task Hours", "EACRO Task Hours",
    "EADATST Task Hours", "EADMT Task Hours", "EAEWS Task Hours",
    "OTDEV Task Hours", "OTMIS Task Hours", "OTPM Task Hours",
    "OTQA Task Hours", "OTTRAIN Task Hours", "PRWS Task Hours",
    "QATEST Task Hours", "SHAWS Task Hours", "Vacation Hours",
    "Total", "Notes"
]

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


# ── DB connection ─────────────────────────────────────────────────────────────

def get_db(db_name=DB_NAME):
    if DATABASE_URL:
        conn = psycopg.connect(DATABASE_URL, row_factory=psycopg.rows.dict_row)
        return conn
    conn = sqlite3.connect(db_name)
    conn.row_factory = sqlite3.Row
    return conn


def db_execute(conn, sql, params=()):
    if DATABASE_URL:
        sql = sql.replace("?", "%s")
        cur = conn.cursor()
        cur.execute(sql, params)
        return cur
    return conn.execute(sql, params)


# ── Sheets service ────────────────────────────────────────────────────────────

def get_sheets_service():
    b64 = os.environ.get("GOOGLE_CREDENTIALS_B64", "")
    if b64:
        info  = json.loads(base64.b64decode(b64).decode("utf-8"))
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    else:
        creds = Credentials.from_service_account_file("service_account.json", scopes=SCOPES)
    return build("sheets", "v4", credentials=creds)


# ── Data fetchers ─────────────────────────────────────────────────────────────

def fetch_user(user_id):
    conn = get_db()
    user = db_execute(conn,
        "SELECT user_id, name, email FROM users WHERE user_id = ?", (user_id,)
    ).fetchone()
    conn.close()
    return user


def fetch_all_entries_for_user(user_id):
    conn = get_db()
    rows = db_execute(conn, """
        SELECT
            t.work_date,
            s.year,
            s.show_code,
            s.show_name,
            w.code        AS work_code,
            t.hours,
            t.comments
        FROM timesheet_entries t
        JOIN work_codes w  ON t.work_code_id = w.work_code_id
        LEFT JOIN shows s  ON t.show_id      = s.show_id
        WHERE t.user_id = ?
        ORDER BY t.work_date ASC, s.show_code ASC
    """, (user_id,)).fetchall()
    conn.close()
    return rows


# ── Row builder ───────────────────────────────────────────────────────────────

def build_sheet_rows(tab_name, db_entries):
    grouped = {}

    for e in db_entries:
        if e["year"] and e["show_code"] and e["show_name"]:
            show_label = f"{e['year']} - {e['show_code']} - {e['show_name']}"
        elif e["show_name"]:
            show_label = e["show_name"]
        else:
            show_label = ""

        key = (e["work_date"], show_label)

        if key not in grouped:
            grouped[key] = {code: 0 for code in WORK_CODE_COLUMNS}
            grouped[key]["_notes"] = []

        wc = e["work_code"].strip()
        # Match case-insensitively
        matched = next((c for c in WORK_CODE_COLUMNS if c.upper() == wc.upper()), None)
        if matched:
            grouped[key][matched] += e["hours"]

        if e["comments"] and e["comments"].strip():
            grouped[key]["_notes"].append(e["comments"].strip())

    sheet_rows = [HEADER_ROW]

   for (work_date, show_label), data in sorted(grouped.items()):
        hours_cols = [data.get(code, 0) for code in WORK_CODE_COLUMNS]
        # Exclude Vacation hours from billable total
        total = sum(
            hrs for code, hrs in zip(WORK_CODE_COLUMNS, hours_cols)
            if code.lower() != "vacation"
        )
        notes = " | ".join(data["_notes"])

        try:
           formatted_date = datetime.strptime(work_date, "%Y-%m-%d").strftime("%Y-%m-%d")
        except Exception:
            formatted_date = work_date

        row = [tab_name, show_label, formatted_date] + hours_cols + [total, notes]
        sheet_rows.append(row)

    return sheet_rows


# ── Sheet operations ──────────────────────────────────────────────────────────

def get_tab_sheet_id(service, tab_name):
    spreadsheet = service.spreadsheets().get(spreadsheetId=MASTER_SHEET_ID).execute()
    for sheet in spreadsheet.get("sheets", []):
        if sheet["properties"]["title"] == tab_name:
            return sheet["properties"]["sheetId"]
    raise ValueError(f"Tab '{tab_name}' not found in master sheet. Run 1_create_and_share.js first.")


def clear_tab(service, tab_name):
    service.spreadsheets().values().clear(
        spreadsheetId=MASTER_SHEET_ID,
        range=f"'{tab_name}'!A:T"
    ).execute()


def write_rows(service, tab_name, rows):
    service.spreadsheets().values().update(
        spreadsheetId=MASTER_SHEET_ID,
        range=f"'{tab_name}'!A1",
        valueInputOption="USER_ENTERED",
        body={"values": rows}
    ).execute()


def format_header(service, sheet_id):
    requests = [{
        "repeatCell": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": 0, "endRowIndex": 1,
                "startColumnIndex": 0, "endColumnIndex": 20
            },
            "cell": {
                "userEnteredFormat": {
                    "backgroundColor": {"red": 0.29, "green": 0.53, "blue": 0.91},
                    "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}}
                }
            },
            "fields": "userEnteredFormat(backgroundColor,textFormat)"
        }
    }]
    service.spreadsheets().batchUpdate(
        spreadsheetId=MASTER_SHEET_ID,
        body={"requests": requests}
    ).execute()


# ── Main sync function ────────────────────────────────────────────────────────

def sync_user_to_sheet(user_id):
    try:
        user = fetch_user(user_id)
        if not user:
            return False, f"User {user_id} not found in DB"

        tab_name   = user["name"].strip()
        service    = get_sheets_service()
        sheet_id   = get_tab_sheet_id(service, tab_name)
        db_entries = fetch_all_entries_for_user(user_id)
        rows       = build_sheet_rows(tab_name, db_entries)

        clear_tab(service, tab_name)
        write_rows(service, tab_name, rows)
        format_header(service, sheet_id)

        print(f"[SheetsSync] ✅ Synced {tab_name} — {len(rows) - 1} row(s)")
        return True, None

    except Exception as e:
        print(f"[SheetsSync] ❌ Error syncing user {user_id}: {e}")
        return False, str(e)
