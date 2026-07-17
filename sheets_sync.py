# ============================================================
# sheets_sync.py
# Writes a developer's timesheet entries to a Google Sheet tab.
# Called on every save / edit / delete in app.py.
# Also supports syncing to a separate backup sheet.
# ============================================================

import os, base64, json, sqlite3, threading
from datetime import datetime, timedelta
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

_sheets_service      = None
_sheets_service_lock = threading.Lock()
_tab_id_cache        = {}       # (spreadsheet_id, tab_name) -> numeric sheetId
_formatted_tabs      = set()    # (spreadsheet_id, tab_name) pairs already header-formatted

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
    global _sheets_service
    with _sheets_service_lock:
        if _sheets_service is None:
            b64 = os.environ.get("GOOGLE_CREDENTIALS_B64", "")
            if b64:
                info  = json.loads(base64.b64decode(b64).decode("utf-8"))
                creds = Credentials.from_service_account_info(info, scopes=SCOPES)
            else:
                creds = Credentials.from_service_account_file("service_account.json", scopes=SCOPES)
            _sheets_service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    return _sheets_service


# ── IST helpers ───────────────────────────────────────────────────────────────

def ist_now():
    """Current datetime in IST (UTC+5:30, no DST)."""
    return datetime.utcnow() + timedelta(hours=5, minutes=30)

def current_month_start_ist():
    """First day of the current month in IST as a YYYY-MM-DD string.
    Grace period: before 6 AM IST on the 1st, include the previous month so
    night-shift entries logged at ~3 AM aren't wiped from the master sheet."""
    now = ist_now()
    if now.day == 1 and now.hour < 6:
        last_of_prev = now.date() - timedelta(days=1)
        return last_of_prev.replace(day=1).isoformat()
    return now.date().replace(day=1).isoformat()

def prev_month_range_ist():
    """(first_day, last_day) strings for the previous calendar month in IST."""
    today_ist     = ist_now().date()
    first_of_curr = today_ist.replace(day=1)
    last_of_prev  = first_of_curr - timedelta(days=1)
    first_of_prev = last_of_prev.replace(day=1)
    return first_of_prev.isoformat(), last_of_prev.isoformat()


# ── Data fetchers ─────────────────────────────────────────────────────────────

def fetch_user_and_entries(user_id, from_date=None, to_date=None):
    """
    Fetch a user + their timesheet entries.
    from_date defaults to first day of current month (IST).
    to_date defaults to no upper bound (i.e. all future dates).
    """
    conn = get_db()
    if from_date is None:
        from_date = current_month_start_ist()
    if to_date is None:
        to_date = "9999-12-31"

    user = db_execute(conn,
        "SELECT user_id, name, email FROM users WHERE user_id = ?", (user_id,)
    ).fetchone()
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
        WHERE t.user_id = ? AND t.work_date >= ? AND t.work_date <= ?
        ORDER BY t.work_date ASC, s.show_code ASC
    """, (user_id, from_date, to_date)).fetchall()
    conn.close()
    return user, rows


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

def get_tab_sheet_id(service, spreadsheet_id, tab_name):
    """Return the numeric sheetId for tab_name inside spreadsheet_id (cached)."""
    cache_key = (spreadsheet_id, tab_name)
    if cache_key not in _tab_id_cache:
        spreadsheet = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
        for sheet in spreadsheet.get("sheets", []):
            _tab_id_cache[(spreadsheet_id, sheet["properties"]["title"])] = sheet["properties"]["sheetId"]
    if cache_key not in _tab_id_cache:
        raise ValueError(f"Tab '{tab_name}' not found in spreadsheet {spreadsheet_id}.")
    return _tab_id_cache[cache_key]


def clear_tab(service, spreadsheet_id, tab_name):
    service.spreadsheets().values().clear(
        spreadsheetId=spreadsheet_id,
        range=f"'{tab_name}'!A:T"
    ).execute()


def write_rows(service, spreadsheet_id, tab_name, rows):
    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=f"'{tab_name}'!A1",
        valueInputOption="USER_ENTERED",
        body={"values": rows}
    ).execute()


def format_header(service, spreadsheet_id, numeric_sheet_id):
    requests = [{
        "repeatCell": {
            "range": {
                "sheetId": numeric_sheet_id,
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
        spreadsheetId=spreadsheet_id,
        body={"requests": requests}
    ).execute()


# ── Main sync function ────────────────────────────────────────────────────────

def sync_user_to_sheet(user_id, spreadsheet_id=None, from_date=None, to_date=None):
    """
    Sync a user's entries to a sheet tab.
    - spreadsheet_id: defaults to MASTER_SHEET_ID (the live sheet)
    - from_date / to_date: date range; from_date defaults to current month start (IST)
    """
    if spreadsheet_id is None:
        spreadsheet_id = MASTER_SHEET_ID

    try:
        user, db_entries = fetch_user_and_entries(user_id, from_date=from_date, to_date=to_date)
        if not user:
            return False, f"User {user_id} not found in DB"

        tab_name        = user["name"].strip()
        service         = get_sheets_service()
        numeric_tab_id  = get_tab_sheet_id(service, spreadsheet_id, tab_name)
        rows            = build_sheet_rows(tab_name, db_entries)

        clear_tab(service, spreadsheet_id, tab_name)
        write_rows(service, spreadsheet_id, tab_name, rows)

        fmt_key = (spreadsheet_id, tab_name)
        if fmt_key not in _formatted_tabs:
            format_header(service, spreadsheet_id, numeric_tab_id)
            _formatted_tabs.add(fmt_key)

        # Verify row count matches
        verify = service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=f"'{tab_name}'!A:A"
        ).execute()
        sheet_count = len(verify.get("values", [])) - 1  # subtract header
        db_count    = len(rows) - 1

        if sheet_count != db_count:
            print(f"[SheetsSync] ⚠️ Count mismatch for {tab_name}: DB={db_count} Sheet={sheet_count} — will retry")
            return False, f"Count mismatch: DB={db_count} Sheet={sheet_count}"

        dest  = "backup" if spreadsheet_id != MASTER_SHEET_ID else "master"
        label = from_date[:7] if from_date else current_month_start_ist()[:7]
        print(f"[SheetsSync] ✅ Synced {tab_name} → {dest} sheet ({label}) — {db_count} row(s) verified")
        return True, None

    except Exception as e:
        print(f"[SheetsSync] ❌ Error syncing user {user_id}: {e}")
        return False, str(e)
