from flask import Flask, render_template, request, redirect, url_for, session, flash, Response
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, date, timedelta
from functools import wraps
from io import StringIO
import csv, os, secrets, string, smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from dotenv import load_dotenv
load_dotenv()

import psycopg
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor

# ── App + DB config ───────────────────────────────────────────────────────────

APP_WORK_CODES = [
    'EAAEP', 'EAALU', 'EACRO', 'EADATST', 'EADMT',
    'EAEWS', 'OTDEV', 'OTMIS', 'OTPM',   'OTQA',
    'OTTRAIN', 'PRWS', 'QATEST', 'SHAWS', 'Vacation'
]

VACATION_NOTIFY_EMAILS = ["kirthika@zydesoft.com", "sivanraj@zydesoft.com"]

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "be-kind-you-never-know-what-the-other-person-is-going-through")

DATABASE_URL = os.environ.get("DATABASE_URL", "")
DB_NAME      = os.environ.get("DB_PATH", "timesheet.db")

# ── Google Sheets sync ────────────────────────────────────────────────────────

SHEETS_SYNC_ENABLED = (
    os.path.exists("service_account.json") or
    bool(os.environ.get("GOOGLE_CREDENTIALS_B64", ""))
)

if SHEETS_SYNC_ENABLED:
    try:
        from sheets_sync import sync_user_to_sheet
        from sync_shows_from_sheet import sync_shows
        print("[App] Google Sheets sync enabled.")
    except Exception as e:
        print(f"[App] Sheets sync import failed: {e}")
        SHEETS_SYNC_ENABLED = False
else:
    print("[App] No credentials found — running without Google Sheets sync.")


# ── DB Helpers ────────────────────────────────────────────────────────────────
def get_db():
    if DATABASE_URL:
        conn = psycopg.connect(DATABASE_URL, row_factory=psycopg.rows.dict_row)
        return conn
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn

def db_execute(conn, sql, params=()):
    if DATABASE_URL:
        sql = sql.replace("?", "%s")
        cur = conn.cursor()
        cur.execute(sql, params)
        return cur
    return conn.execute(sql, params)

def get_setting(key, default=""):
    conn = get_db()
    row  = db_execute(conn, "SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default

def set_setting(key, value):
    conn = get_db()
    if DATABASE_URL:
        db_execute(conn, """
            INSERT INTO app_settings (key, value) VALUES (%s, %s)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """, (key, value))
    else:
        db_execute(conn, """
            INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)
        """, (key, value))
    conn.commit()
    conn.close()

def prev_month_range_ist():
    """(first_day_str, last_day_str) for the previous calendar month in IST."""
    today_ist     = (datetime.utcnow() + timedelta(hours=5, minutes=30)).date()
    first_of_curr = today_ist.replace(day=1)
    last_of_prev  = first_of_curr - timedelta(days=1)
    first_of_prev = last_of_prev.replace(day=1)
    return first_of_prev.isoformat(), last_of_prev.isoformat()

def column_exists(conn, table_name, column_name):
    if DATABASE_URL:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name = %s AND column_name = %s
            """, (table_name, column_name))
            return cur.fetchone() is not None



def get_placeholder():
    return "%s" if DATABASE_URL else "?"


def init_db():
    conn = get_db()
    cur  = conn.cursor()

    if DATABASE_URL:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id       SERIAL PRIMARY KEY,
            name          TEXT NOT NULL,
            email         TEXT UNIQUE NOT NULL,
            password      TEXT,
            role          TEXT NOT NULL CHECK(role IN ('admin', 'user')),
            auth_provider TEXT NOT NULL DEFAULT 'local'
        )""")

        # Add new user columns if missing (safe for existing DBs)
        for col, defn in [
            ("team",            "TEXT DEFAULT ''"),
            ("description",     "TEXT DEFAULT ''"),
            ("rate_per_hour",   "REAL DEFAULT 0"),
            ("hike",            "REAL DEFAULT 0"),
            ("reset_token",     "TEXT DEFAULT NULL"),
            ("reset_token_exp", "TEXT DEFAULT NULL"),
        ]:
            if not column_exists(conn, "users", col):
                cur.execute(f"ALTER TABLE users ADD COLUMN {col} {defn}")

        cur.execute("""
        CREATE TABLE IF NOT EXISTS shows (
            show_id     SERIAL PRIMARY KEY,
            year        TEXT NOT NULL,
            show_code   TEXT NOT NULL,
            show_name   TEXT NOT NULL,
            active_flag TEXT NOT NULL DEFAULT 'Y',
            UNIQUE(year, show_code, show_name)
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS work_codes (
            work_code_id SERIAL PRIMARY KEY,
            code         TEXT UNIQUE NOT NULL,
            description  TEXT NOT NULL,
            active_flag  TEXT NOT NULL DEFAULT 'Y'
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS show_work_codes (
            show_id      INTEGER NOT NULL REFERENCES shows(show_id),
            work_code_id INTEGER NOT NULL REFERENCES work_codes(work_code_id),
            PRIMARY KEY (show_id, work_code_id)
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS timesheet_entries (
            entry_id     SERIAL PRIMARY KEY,
            user_id      INTEGER NOT NULL REFERENCES users(user_id),
            show_id      INTEGER REFERENCES shows(show_id),
            work_code_id INTEGER NOT NULL REFERENCES work_codes(work_code_id),
            work_date    TEXT NOT NULL,
            hours        REAL NOT NULL,
            comments     TEXT,
            created_at   TEXT NOT NULL,
            updated_at   TEXT NOT NULL
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS notifications (
            notification_id   SERIAL PRIMARY KEY,
            user_id           INTEGER REFERENCES users(user_id),
            message           TEXT NOT NULL,
            notification_type TEXT NOT NULL DEFAULT 'warning',
            is_read           TEXT NOT NULL DEFAULT 'N',
            created_at        TEXT NOT NULL
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS app_settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL DEFAULT ''
        )""")

    else:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL, email TEXT UNIQUE NOT NULL,
            password TEXT, role TEXT NOT NULL CHECK(role IN ('admin', 'user')),
            auth_provider TEXT NOT NULL DEFAULT 'local'
        )""")
        if not column_exists(conn, "users", "auth_provider"):
            cur.execute("ALTER TABLE users ADD COLUMN auth_provider TEXT NOT NULL DEFAULT 'local'")
        if not column_exists(conn, "users", "team"):
            cur.execute("ALTER TABLE users ADD COLUMN team TEXT DEFAULT ''")
        if not column_exists(conn, "users", "description"):
            cur.execute("ALTER TABLE users ADD COLUMN description TEXT DEFAULT ''")
        if not column_exists(conn, "users", "rate_per_hour"):
            cur.execute("ALTER TABLE users ADD COLUMN rate_per_hour REAL DEFAULT 0")
        if not column_exists(conn, "users", "hike"):
            cur.execute("ALTER TABLE users ADD COLUMN hike REAL DEFAULT 0")
        if not column_exists(conn, "users", "reset_token"):
            cur.execute("ALTER TABLE users ADD COLUMN reset_token TEXT DEFAULT NULL")
        if not column_exists(conn, "users", "reset_token_exp"):
            cur.execute("ALTER TABLE users ADD COLUMN reset_token_exp TEXT DEFAULT NULL")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS shows (
            show_id INTEGER PRIMARY KEY AUTOINCREMENT,
            year TEXT NOT NULL, show_code TEXT NOT NULL, show_name TEXT NOT NULL,
            active_flag TEXT NOT NULL DEFAULT 'Y', UNIQUE(year, show_code, show_name)
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS work_codes (
            work_code_id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL, description TEXT NOT NULL,
            active_flag TEXT NOT NULL DEFAULT 'Y'
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS show_work_codes (
            show_id INTEGER NOT NULL, work_code_id INTEGER NOT NULL,
            PRIMARY KEY (show_id, work_code_id),
            FOREIGN KEY(show_id) REFERENCES shows(show_id),
            FOREIGN KEY(work_code_id) REFERENCES work_codes(work_code_id)
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS timesheet_entries (
            entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL, show_id INTEGER, work_code_id INTEGER NOT NULL,
            work_date TEXT NOT NULL, hours REAL NOT NULL, comments TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(user_id),
            FOREIGN KEY(show_id) REFERENCES shows(show_id),
            FOREIGN KEY(work_code_id) REFERENCES work_codes(work_code_id)
        )""")
        if not column_exists(conn, "timesheet_entries", "show_id"):
            cur.execute("ALTER TABLE timesheet_entries ADD COLUMN show_id INTEGER")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS notifications (
            notification_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, message TEXT NOT NULL,
            notification_type TEXT NOT NULL DEFAULT 'warning',
            is_read TEXT NOT NULL DEFAULT 'N', created_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(user_id)
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS app_settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL DEFAULT ''
        )""")

    # Seed admin user
    p = get_placeholder()
    cur.execute(f"SELECT 1 FROM users WHERE email = {p}", ("admin@zydesoft.com",))
    if not cur.fetchone():
        cur.execute(f"""
            INSERT INTO users (name, email, password, role, auth_provider)
            VALUES ({p},{p},{p},{p},{p})
        """, ("Admin", "admin@zydesoft.com", generate_password_hash("changeme123"), "admin", "local"))

    # Seed work codes
    for code in APP_WORK_CODES:
        if DATABASE_URL:
            cur.execute("""
                INSERT INTO work_codes (code, description, active_flag)
                VALUES (%s,%s,'Y') ON CONFLICT (code) DO NOTHING
            """, (code, code))
        else:
            cur.execute("INSERT OR IGNORE INTO work_codes (code, description, active_flag) VALUES (?,?,'Y')", (code, code))

    conn.commit()
    if DATABASE_URL:
        cur.close()
    conn.close()

    if SHEETS_SYNC_ENABLED:
        print("[App] Syncing shows from Google Sheet...")
        added, err = sync_shows(DB_NAME)
        if err:
            print(f"[App] Show sync warning: {err}")
        else:
            print(f"[App] Show sync done — {added} new show(s) added")
    else:
        _seed_special_shows()


def _seed_special_shows():
    special = [
        ("GENERAL", "VACATION", "Vacation"),
        ("GENERAL", "HOLIDAY",  "Holiday"),
        ("GENERAL", "OTHER",    "Other"),
    ]
    conn = get_db()
    for year, show_code, show_name in special:
        try:
            db_execute(conn, """
                INSERT INTO shows (year, show_code, show_name, active_flag)
                VALUES (?,?,?,'Y')
            """ + (" ON CONFLICT (year,show_code,show_name) DO NOTHING" if DATABASE_URL else ""),
            (year, show_code, show_name))
        except Exception:
            pass

    show_rows = db_execute(conn, "SELECT show_id FROM shows").fetchall()
    work_rows = db_execute(conn, "SELECT work_code_id FROM work_codes").fetchall()
    for show in show_rows:
        for work in work_rows:
            try:
                db_execute(conn, "INSERT INTO show_work_codes (show_id, work_code_id) VALUES (?,?)",
                           (show["show_id"], work["work_code_id"]))
            except Exception:
                pass
    conn.commit()
    conn.close()


# ── Auth Decorators ───────────────────────────────────────────────────────────

def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if session.get("role") != "admin":
            flash("Admin access required.", "error")
            return redirect(url_for("dashboard"))
        return view(*args, **kwargs)
    return wrapped


# ── Utility ───────────────────────────────────────────────────────────────────

def get_monday(d=None):
    if d is None:
        d = date.today()
    return d - timedelta(days=d.weekday())


def is_locked(work_date_text):
    work_date = datetime.strptime(work_date_text, "%Y-%m-%d").date()
    # During May 2026 rollout — allow editing any date in May 2026
    if work_date.year == 2026 and work_date.month == 5:
        return False
    return work_date < get_monday(date.today())


def week_bounds(selected_week=None):
    if selected_week:
        selected = datetime.strptime(selected_week, "%Y-%m-%d").date()
    else:
        selected = date.today()
    monday = get_monday(selected)
    return monday, monday + timedelta(days=6)


def create_notification(user_id, message, notification_type="warning"):
    conn = get_db()
    now  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    db_execute(conn, """
        INSERT INTO notifications (user_id, message, notification_type, is_read, created_at)
        VALUES (?,?,?,'N',?)
    """, (user_id, message, notification_type, now))
    conn.commit()
    conn.close()


def validate_day(user_id, work_date):
    conn  = get_db()
    total = db_execute(conn, """
        SELECT COALESCE(SUM(hours), 0) AS total_hours
        FROM timesheet_entries WHERE user_id = ? AND work_date = ?
    """, (user_id, work_date)).fetchone()["total_hours"]

    missing_notes = db_execute(conn, """
        SELECT COUNT(*) AS cnt FROM timesheet_entries
        WHERE user_id = ? AND work_date = ?
          AND (comments IS NULL OR TRIM(comments) = '')
    """, (user_id, work_date)).fetchone()["cnt"]

    user = db_execute(conn, "SELECT name FROM users WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    name = user["name"] if user else "User"

    if total < 8:
        create_notification(user_id,
            f"{name}: {work_date} has only {total} hours logged. Minimum is 8.", "warning")
    if missing_notes > 0:
        create_notification(user_id,
            f"{name}: {work_date} has {missing_notes} entr{'y' if missing_notes == 1 else 'ies'} missing notes.", "warning")


def get_show_work_codes(conn, show_id=None):
    if show_id:
        return db_execute(conn, """
            SELECT w.* FROM work_codes w
            JOIN show_work_codes swc ON w.work_code_id = swc.work_code_id
            WHERE swc.show_id = ? AND w.active_flag = 'Y'
            ORDER BY w.code
        """, (show_id,)).fetchall()
    return db_execute(conn,
        "SELECT * FROM work_codes WHERE active_flag = 'Y' ORDER BY code"
    ).fetchall()


MAX_SYNC_RETRIES = 3

_sync_executor  = ThreadPoolExecutor(max_workers=2)
_queued_users   = set()
_queued_lock    = threading.Lock()

def do_sheets_sync(user_id):
    if not SHEETS_SYNC_ENABLED:
        return

    with _queued_lock:
        if user_id in _queued_users:
            return  # a sync is already queued; it will read the latest DB state when it runs
        _queued_users.add(user_id)

    print(f"[SheetsSync] Starting sync for user {user_id}")

    def _sync():
        with _queued_lock:
            _queued_users.discard(user_id)

        for attempt in range(1, MAX_SYNC_RETRIES + 1):
            try:
                print(f"[SheetsSync] Attempt {attempt}/{MAX_SYNC_RETRIES} for user {user_id}")
                ok, err = sync_user_to_sheet(user_id)
                if ok:
                    print(f"[SheetsSync] Sync complete for user {user_id} (attempt {attempt})")
                    return
                else:
                    print(f"[SheetsSync] Sync failed for user {user_id}: {err}")
            except Exception as e:
                print(f"[SheetsSync] Sync error for user {user_id} attempt {attempt}: {e}")

            if attempt < MAX_SYNC_RETRIES:
                time.sleep(2 ** attempt)  # exponential backoff: 2s, 4s

        print(f"[SheetsSync] ❌ All {MAX_SYNC_RETRIES} attempts failed for user {user_id}")

    _sync_executor.submit(_sync)

def generate_password(length=10):
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))


# ── Routes: Auth ──────────────────────────────────────────────────────────────

@app.route("/")
def home():
    return redirect(url_for("dashboard") if "user_id" in session else url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    # Already logged in — send to dashboard
    if "user_id" in session:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        email    = request.form["email"].strip().lower()
        password = request.form["password"]
        conn     = get_db()
        user     = db_execute(conn, "SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        conn.close()

        if user and user["password"] and check_password_hash(user["password"], password):
            session["user_id"] = user["user_id"]
            session["name"]    = user["name"]
            session["role"]    = user["role"]
            return redirect(url_for("dashboard"))

        flash("Invalid email or password.", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/notifications/mark-read", methods=["POST"])
@login_required
def mark_notifications_read():
    conn = get_db()
    if session["role"] == "admin":
        db_execute(conn, "UPDATE notifications SET is_read = 'Y' WHERE is_read = 'N'")
    else:
        db_execute(conn, "UPDATE notifications SET is_read = 'Y' WHERE user_id = ? AND is_read = 'N'",
                   (session["user_id"],))
    conn.commit()
    conn.close()
    return {"ok": True}
    
# ── Routes: Dashboard ─────────────────────────────────────────────────────────

@app.route("/dashboard")
@login_required
def dashboard():
    conn           = get_db()
    today          = date.today()
    current_monday = get_monday(today)
    current_friday = current_monday + timedelta(days=4)
    business_days  = [current_monday + timedelta(days=i) for i in range(5)]

    admin_missing_month = []

    if session["role"] == "admin":
        total_hours = db_execute(conn,
            "SELECT COALESCE(SUM(hours), 0) AS total FROM timesheet_entries"
        ).fetchone()["total"]

        users = db_execute(conn,
            "SELECT user_id, name, email FROM users WHERE role = 'user' ORDER BY name"
        ).fetchall()

        admin_missing_users = []
        for u in users:
            rows = db_execute(conn, """
                SELECT work_date, COALESCE(SUM(hours), 0) AS total_hours
                FROM timesheet_entries
                WHERE user_id = ? AND work_date BETWEEN ? AND ?
                GROUP BY work_date
            """, (u["user_id"], current_monday.isoformat(), current_friday.isoformat())).fetchall()

            totals       = {r["work_date"]: r["total_hours"] for r in rows}
            week_entered = 0
            days         = []
            for d in business_days:
                entered = totals.get(d.isoformat(), 0)
                week_entered += entered
                days.append({"date": d.isoformat(), "day_name": d.strftime("%A"),
                             "entered": entered, "missing": max(8 - entered, 0)})

            total_missing = max(40 - week_entered, 0)
            if total_missing > 0:
                admin_missing_users.append({
                    "user_id": u["user_id"], "name": u["name"], "email": u["email"],
                    "entered": week_entered, "missing": total_missing, "days": days
                })

        # Missing this month
        month_start = today.replace(day=1)
        month_working_days = []
        d = month_start
        while d <= today:
            if d.weekday() < 5:
                month_working_days.append(d)
            d += timedelta(days=1)
        month_required_hrs = len(month_working_days) * 8

        for u in users:
            row = db_execute(conn, """
                SELECT COALESCE(SUM(hours), 0) AS total_hours
                FROM timesheet_entries
                WHERE user_id = ? AND work_date BETWEEN ? AND ?
            """, (u["user_id"], month_start.isoformat(), today.isoformat())).fetchone()
            logged = row["total_hours"]
            if logged < month_required_hrs:
                admin_missing_month.append({
                    "user_id": u["user_id"], "name": u["name"],
                    "logged":  logged, "missing": round(month_required_hrs - logged, 1)
                })

        pending_summary = None

    else:
        total_hours = db_execute(conn,
            "SELECT COALESCE(SUM(hours), 0) AS total FROM timesheet_entries WHERE user_id = ?",
            (session["user_id"],)
        ).fetchone()["total"]

        week_rows = db_execute(conn, """
            SELECT work_date, COALESCE(SUM(hours), 0) AS total_hours
            FROM timesheet_entries
            WHERE user_id = ? AND work_date BETWEEN ? AND ?
            GROUP BY work_date
        """, (session["user_id"], current_monday.isoformat(), current_friday.isoformat())).fetchall()

        totals       = {r["work_date"]: r["total_hours"] for r in week_rows}
        week_entered = 0
        pending_days = []
        for d in business_days:
            entered = totals.get(d.isoformat(), 0)
            week_entered += entered
            pending_days.append({"date": d.isoformat(), "day_name": d.strftime("%A"),
                                 "entered": entered, "missing": max(8 - entered, 0)})

        pending_summary     = {"entered": week_entered, "missing": max(40 - week_entered, 0), "days": pending_days}
        admin_missing_users = []

    recent_entries = db_execute(conn, """
        SELECT t.entry_id, t.work_date, t.hours, t.comments,
               w.code, w.description, s.show_code, s.show_name, u.name
        FROM timesheet_entries t
        JOIN work_codes w  ON t.work_code_id = w.work_code_id
        LEFT JOIN shows s  ON t.show_id      = s.show_id
        JOIN users u       ON t.user_id      = u.user_id
        WHERE (? = 'admin' OR t.user_id = ?)
          AND t.work_date BETWEEN ? AND ?
        ORDER BY t.work_date DESC, t.entry_id DESC LIMIT 40
    """, (session["role"], session["user_id"],
          current_monday.isoformat(), current_friday.isoformat())).fetchall()

    grouped = []
    current_group = None
    for entry in recent_entries:
        if not current_group or current_group["date"] != entry["work_date"]:
            current_group = {"date": entry["work_date"], "entries": []}
            grouped.append(current_group)
        current_group["entries"].append(entry)

    if session["role"] == "admin":
        notifications = db_execute(conn, """
            SELECT n.*, u.name FROM notifications n
            LEFT JOIN users u ON n.user_id = u.user_id
            ORDER BY n.created_at DESC LIMIT 10
        """).fetchall()
        notification_count = db_execute(conn,
            "SELECT COUNT(*) AS cnt FROM notifications WHERE is_read = 'N'"
        ).fetchone()["cnt"]
    else:
        notifications = db_execute(conn, """
            SELECT * FROM notifications WHERE user_id = ?
            ORDER BY created_at DESC LIMIT 10
        """, (session["user_id"],)).fetchall()
        notification_count = db_execute(conn,
            "SELECT COUNT(*) AS cnt FROM notifications WHERE user_id = ? AND is_read = 'N'",
            (session["user_id"],)
        ).fetchone()["cnt"]

    conn.close()

    from flask import make_response
    resp = make_response(render_template("dashboard.html",
        total_hours=total_hours,
        pending_summary=pending_summary,
        admin_missing_users=admin_missing_users,
        admin_missing_month=admin_missing_month,
        grouped_recent_entries=grouped,
        notifications=notifications,
        notification_count=notification_count,
        current_datetime=datetime.now().strftime("%A, %B %d, %Y %I:%M %p"),
        week_start=current_monday.isoformat(),
        week_end=current_friday.isoformat(),
        sheets_sync_enabled=SHEETS_SYNC_ENABLED
    ))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"]        = "no-cache"
    resp.headers["Expires"]       = "0"
    return resp


# ── Routes: Timesheet ─────────────────────────────────────────────────────────

@app.route("/timesheet", methods=["GET", "POST"])
@login_required
def timesheet():
    if session.get("role") == "admin":
        flash("Admins use Dashboard and Reports.", "error")
        return redirect(url_for("dashboard"))

    conn = get_db()

    if request.method == "POST":
        work_date    = request.form["work_date"]
        show_id      = request.form["show_id"]
        work_code_id = request.form["work_code_id"]
        hours        = float(request.form["hours"])
        comments     = request.form.get("comments", "").strip()
        now          = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        allowed = db_execute(conn, """
            SELECT 1 FROM show_work_codes WHERE show_id = ? AND work_code_id = ?
        """, (show_id, work_code_id)).fetchone()

        if is_locked(work_date):
            flash("This week is locked. Previous weeks cannot be edited.", "error")
        elif not allowed:
            flash("Work code not assigned to selected show.", "error")
        elif hours <= 0 or hours > 24:
            flash("Hours must be between 0 and 24.", "error")
        elif not comments:
            flash("Notes are required.", "error")
        else:
            # Check for duplicate in-flight submission
            existing = db_execute(conn, """
                SELECT entry_id FROM timesheet_entries
                WHERE user_id = ? AND show_id = ? AND work_code_id = ?
                  AND work_date = ? AND hours = ? AND created_at >= ?
            """, (session["user_id"], show_id, work_code_id, work_date, hours,
                  (datetime.now() - timedelta(seconds=10)).strftime("%Y-%m-%d %H:%M:%S")
            )).fetchone()

            if not existing:
                db_execute(conn, """
                    INSERT INTO timesheet_entries
                    (user_id, show_id, work_code_id, work_date, hours, comments, created_at, updated_at)
                    VALUES (?,?,?,?,?,?,?,?)
                """, (session["user_id"], show_id, work_code_id, work_date, hours, comments, now, now))

            conn.commit()
            conn.close()
            validate_day(session["user_id"], work_date)
            do_sheets_sync(session["user_id"])
            flash("Entry saved.", "success")
            return redirect(url_for("timesheet"))

    shows      = db_execute(conn, "SELECT * FROM shows WHERE active_flag = 'Y' ORDER BY year, show_code, show_name").fetchall()
    work_codes = get_show_work_codes(conn)
    current_monday = get_monday(date.today())
    current_friday = current_monday + timedelta(days=4)

    entries = db_execute(conn, """
        SELECT t.entry_id, t.work_date, t.hours, t.comments,
               w.code, w.description, s.show_code, s.show_name, s.year
        FROM timesheet_entries t
        JOIN work_codes w  ON t.work_code_id = w.work_code_id
        LEFT JOIN shows s  ON t.show_id      = s.show_id
        WHERE t.user_id = ? AND t.work_date BETWEEN ? AND ?
        ORDER BY t.work_date DESC, t.entry_id DESC
    """, (session["user_id"], current_monday.isoformat(), current_friday.isoformat())).fetchall()

    conn.close()
    prefill_date = request.args.get("prefill_date", datetime.now().strftime("%Y-%m-%d"))
    return render_template("timesheet.html",
        shows=shows, work_codes=work_codes, entries=entries,
        today=prefill_date, is_locked=is_locked)


@app.route("/api/work-codes/<int:show_id>")
@login_required
def api_work_codes(show_id):
    conn  = get_db()
    rows  = get_show_work_codes(conn, show_id)
    conn.close()
    return {"work_codes": [{"work_code_id": r["work_code_id"], "code": r["code"],
                            "description": r["description"]} for r in rows]}


@app.route("/edit-entry/<int:entry_id>", methods=["GET", "POST"])
@login_required
def edit_entry(entry_id):
    conn  = get_db()
    entry = db_execute(conn, """
        SELECT * FROM timesheet_entries
        WHERE entry_id = ? AND (? = 'admin' OR user_id = ?)
    """, (entry_id, session["role"], session["user_id"])).fetchone()

    if not entry:
        conn.close()
        flash("Entry not found.", "error")
        return redirect(url_for("timesheet"))

    if is_locked(entry["work_date"]) and session["role"] != "admin":
        conn.close()
        flash("This entry is locked.", "error")
        return redirect(url_for("timesheet"))

    # Resolve where to go after save/cancel.
    # GET: from query string. POST: from hidden form field.
    # Must be a relative URL to prevent open-redirect attacks.
    raw_next = request.args.get("next") or request.form.get("next", "")
    next_url = raw_next if (raw_next and raw_next.startswith("/")) else url_for("timesheet")

    if request.method == "POST":
        work_date    = request.form["work_date"]
        show_id      = request.form["show_id"]
        work_code_id = request.form["work_code_id"]
        hours        = float(request.form["hours"])
        comments     = request.form.get("comments", "").strip()
        now          = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        allowed = db_execute(conn, """
            SELECT 1 FROM show_work_codes WHERE show_id = ? AND work_code_id = ?
        """, (show_id, work_code_id)).fetchone()

        if is_locked(work_date) and session["role"] != "admin":
            flash("Cannot move entry to a locked week.", "error")
        elif not allowed:
            flash("Work code not assigned to selected show.", "error")
        elif hours <= 0 or hours > 24:
            flash("Hours must be between 0 and 24.", "error")
        elif not comments:
            flash("Notes are required.", "error")
        else:
            db_execute(conn, """
                UPDATE timesheet_entries
                SET show_id = ?, work_date = ?, work_code_id = ?, hours = ?, comments = ?, updated_at = ?
                WHERE entry_id = ?
            """, (show_id, work_date, work_code_id, hours, comments, now, entry_id))
            conn.commit()
            conn.close()
            # Always use the entry owner's user_id — not the session user —
            # so admin edits sync the correct employee's sheet.
            validate_day(entry["user_id"], work_date)
            do_sheets_sync(entry["user_id"])
            flash("Entry updated.", "success")
            return redirect(next_url)

    shows      = db_execute(conn, "SELECT * FROM shows WHERE active_flag = 'Y' ORDER BY year, show_code, show_name").fetchall()
    work_codes = get_show_work_codes(conn, entry["show_id"])
    conn.close()
    return render_template("edit_entry.html", entry=entry, shows=shows, work_codes=work_codes, next_url=next_url)


@app.route("/delete-entry/<int:entry_id>")
@login_required
def delete_entry(entry_id):
    conn  = get_db()
    entry = db_execute(conn, "SELECT * FROM timesheet_entries WHERE entry_id = ?", (entry_id,)).fetchone()

    if not entry:
        conn.close()
        flash("Entry not found.", "error")
        return redirect(url_for("timesheet"))

    if is_locked(entry["work_date"]) and session["role"] != "admin":
        conn.close()
        flash("This entry is locked.", "error")
        return redirect(url_for("timesheet"))

    user_id = entry["user_id"]

    if session["role"] == "admin":
        db_execute(conn, "DELETE FROM timesheet_entries WHERE entry_id = ?", (entry_id,))
    else:
        db_execute(conn, "DELETE FROM timesheet_entries WHERE entry_id = ? AND user_id = ?",
                   (entry_id, session["user_id"]))

    conn.commit()
    conn.close()
    do_sheets_sync(user_id)
    flash("Entry deleted.", "success")
    return redirect(request.referrer or url_for("timesheet"))


# ── Routes: Weekly View ───────────────────────────────────────────────────────

@app.route("/weekly")
@login_required
def weekly():
    if session.get("role") == "admin":
        flash("Admins use Dashboard and Reports.", "error")
        return redirect(url_for("dashboard"))

    selected_week = request.args.get("week")
    monday, _     = week_bounds(selected_week)
    friday        = monday + timedelta(days=4)

    conn = get_db()
    rows = db_execute(conn, """
        SELECT work_date, SUM(hours) AS total_hours
        FROM timesheet_entries
        WHERE user_id = ? AND work_date BETWEEN ? AND ?
        GROUP BY work_date
    """, (session["user_id"], monday.isoformat(), friday.isoformat())).fetchall()

    day_totals = {r["work_date"]: r["total_hours"] for r in rows}
    days = []
    for i in range(5):
        d     = monday + timedelta(days=i)
        total = day_totals.get(d.isoformat(), 0)
        if total == 0:
            status, cls = "No time entered", "no-time"
        elif total < 8:
            status, cls = "Partially filled", "partial-time"
        else:
            status, cls = "Complete", "full-time"
        days.append({"date": d.isoformat(), "day_name": d.strftime("%A"),
                     "total": total, "status": status, "status_class": cls,
                     "locked": is_locked(d.isoformat())})

    entries = db_execute(conn, """
        SELECT t.entry_id, t.work_date, t.hours, t.comments,
               w.code, w.description, s.show_code, s.show_name
        FROM timesheet_entries t
        JOIN work_codes w  ON t.work_code_id = w.work_code_id
        LEFT JOIN shows s  ON t.show_id      = s.show_id
        WHERE t.user_id = ? AND t.work_date BETWEEN ? AND ?
        ORDER BY t.work_date, t.entry_id
    """, (session["user_id"], monday.isoformat(), friday.isoformat())).fetchall()

    conn.close()
    return render_template("weekly.html",
        monday=monday, sunday=friday, days=days, entries=entries,
        prev_week=(monday - timedelta(days=7)).isoformat(),
        next_week=(monday + timedelta(days=7)).isoformat())


# ── Routes: Monthly View ──────────────────────────────────────────────────────

@app.route("/monthly")
@login_required
def monthly():
    if session.get("role") == "admin":
        flash("Admins use Dashboard and Reports.", "error")
        return redirect(url_for("dashboard"))

    month_param = request.args.get("month")
    today       = date.today()

    if month_param:
        try:
            first_day = datetime.strptime(month_param + "-01", "%Y-%m-%d").date()
        except ValueError:
            first_day = today.replace(day=1)
    else:
        first_day = today.replace(day=1)

    # Last day of this month
    if first_day.month == 12:
        last_day = date(first_day.year + 1, 1, 1) - timedelta(days=1)
    else:
        last_day = date(first_day.year, first_day.month + 1, 1) - timedelta(days=1)

    # Prev / next month strings
    prev_first = date(first_day.year - 1, 12, 1) if first_day.month == 1 \
                 else date(first_day.year, first_day.month - 1, 1)
    next_first = date(first_day.year + 1, 1, 1) if first_day.month == 12 \
                 else date(first_day.year, first_day.month + 1, 1)
    prev_month = prev_first.strftime("%Y-%m")
    next_month = next_first.strftime("%Y-%m")

    conn = get_db()
    day_rows = db_execute(conn, """
        SELECT work_date, SUM(hours) AS total_hours
        FROM timesheet_entries
        WHERE user_id = ? AND work_date BETWEEN ? AND ?
        GROUP BY work_date
    """, (session["user_id"], first_day.isoformat(), last_day.isoformat())).fetchall()

    entry_rows = db_execute(conn, """
        SELECT t.entry_id, t.work_date, t.hours, t.comments,
               w.code, s.show_code, s.show_name
        FROM timesheet_entries t
        JOIN work_codes w  ON t.work_code_id = w.work_code_id
        LEFT JOIN shows s  ON t.show_id      = s.show_id
        WHERE t.user_id = ? AND t.work_date BETWEEN ? AND ?
        ORDER BY t.work_date, t.entry_id
    """, (session["user_id"], first_day.isoformat(), last_day.isoformat())).fetchall()
    conn.close()

    day_totals      = {r["work_date"]: r["total_hours"] for r in day_rows}
    entries_by_date = {}
    for e in entry_rows:
        entries_by_date.setdefault(e["work_date"], []).append(e)

    # Build week blocks (Mon-Fri)
    w_start    = first_day - timedelta(days=first_day.weekday())  # Monday on/before first_day
    cur_monday = get_monday(today)
    weeks      = []

    while w_start <= last_day:
        week_days    = []
        flat_entries = []
        week_logged  = 0
        week_cap     = 0   # full week capacity (all working days in month)
        week_req     = 0   # elapsed working days only (for on-track badge)

        for i in range(5):
            d = w_start + timedelta(days=i)

            if d.month != first_day.month:
                week_days.append({"is_padding": True})
                continue

            total = day_totals.get(d.isoformat(), 0)
            week_cap += 8               # always accumulate full capacity
            if d <= today:
                week_req += 8           # only elapsed days count as "required"
            week_logged += total

            if d > today:
                status, cls = "—", "no-time"
            elif total == 0:
                status, cls = "No entry", "no-time"
            elif total < 8:
                status, cls = f"{total} hrs", "partial-time"
            else:
                status, cls = "Complete", "full-time"

            week_days.append({
                "is_padding":   False,
                "date":         d.isoformat(),
                "short_name":   d.strftime("%a"),
                "day_num":      d.day,
                "total":        total,
                "status":       status,
                "status_class": cls,
                "locked":       is_locked(d.isoformat()),
                "is_today":     d == today,
                "is_future":    d > today,
            })
            flat_entries.extend(entries_by_date.get(d.isoformat(), []))

        if any(not d["is_padding"] for d in week_days):
            friday = w_start + timedelta(days=4)
            weeks.append({
                "label":      f"{w_start.strftime('%d %b')} – {friday.strftime('%d %b')}",
                "days":       week_days,
                "entries":    flat_entries,
                "logged":     week_logged,
                "capacity":   week_cap,   # shown in header (full week)
                "required":   week_req,   # used for on-track badge
                "is_current": w_start == cur_monday,
            })

        w_start += timedelta(days=7)

    # Month totals
    month_logged = sum(day_totals.values())

    # Full month capacity (all working days regardless of date)
    month_capacity = sum(
        1 for i in range((last_day - first_day).days + 1)
        if (first_day + timedelta(days=i)).weekday() < 5
    ) * 8

    # Elapsed working days (for progress % — are you on track?)
    month_required = sum(
        1 for i in range((min(last_day, today) - first_day).days + 1)
        if (first_day + timedelta(days=i)).weekday() < 5
    ) * 8
    progress_pct = min(100, round(month_logged / month_required * 100)) if month_required else 0

    return render_template("monthly.html",
        month_label    = first_day.strftime("%B %Y"),
        prev_month     = prev_month,
        next_month     = next_month,
        weeks          = weeks,
        month_logged   = month_logged,
        month_capacity = month_capacity,
        month_required = month_required,
        progress_pct   = progress_pct,
    )


# ── Routes: Reports (Admin) ───────────────────────────────────────────────────

@app.route("/reports")
@login_required
@admin_required
def reports():
    from_date    = request.args.get("from_date", "")
    to_date      = request.args.get("to_date", "")
    user_id      = request.args.get("user_id", "")
    show_id      = request.args.get("show_id", "")
    work_code_id = request.args.get("work_code_id", "")
    report_type  = request.args.get("report_type", "custom")

    today = date.today()
    if report_type == "weekly":
        monday    = get_monday(today)
        from_date = monday.isoformat()
        to_date   = (monday + timedelta(days=4)).isoformat()
    elif report_type == "monthly":
        from_date = today.replace(day=1).isoformat()
        to_date   = today.isoformat()

    base_filters = ""
    params       = []
    if from_date:
        base_filters += " AND t.work_date >= ?"
        params.append(from_date)
    if to_date:
        base_filters += " AND t.work_date <= ?"
        params.append(to_date)
    if user_id:
        base_filters += " AND t.user_id = ?"
        params.append(user_id)
    if show_id:
        base_filters += " AND t.show_id = ?"
        params.append(show_id)
    if work_code_id:
        base_filters += " AND t.work_code_id = ?"
        params.append(work_code_id)

    conn    = get_db()
    entries = db_execute(conn, f"""
        SELECT t.entry_id, u.name, t.work_date,
               s.year, s.show_code, s.show_name,
               w.code, w.description, t.hours, t.comments
        FROM timesheet_entries t
        JOIN users u       ON t.user_id      = u.user_id
        JOIN work_codes w  ON t.work_code_id = w.work_code_id
        LEFT JOIN shows s  ON t.show_id      = s.show_id
        WHERE 1=1 {base_filters}
        ORDER BY t.work_date DESC, u.name, s.show_code, w.code
    """, params).fetchall()

    summary = db_execute(conn, f"""
        SELECT u.name, s.show_code, s.show_name, w.code, SUM(t.hours) AS total_hours
        FROM timesheet_entries t
        JOIN users u       ON t.user_id      = u.user_id
        JOIN work_codes w  ON t.work_code_id = w.work_code_id
        LEFT JOIN shows s  ON t.show_id      = s.show_id
        WHERE 1=1 {base_filters}
        GROUP BY u.name, s.show_code, s.show_name, w.code
        ORDER BY u.name, s.show_code, w.code
    """, params).fetchall()

    missing_report = []
    if from_date and to_date:
        all_users = db_execute(conn,
            "SELECT user_id, name FROM users WHERE role = 'user' ORDER BY name"
        ).fetchall()
        fd = datetime.strptime(from_date, "%Y-%m-%d").date()
        td = datetime.strptime(to_date,   "%Y-%m-%d").date()
        working_days = []
        d = fd
        while d <= td:
            if d.weekday() < 5:
                working_days.append(d)
            d += timedelta(days=1)

        for u in all_users:
            rows = db_execute(conn, """
                SELECT work_date, COALESCE(SUM(hours), 0) AS total_hours
                FROM timesheet_entries
                WHERE user_id = ? AND work_date BETWEEN ? AND ?
                GROUP BY work_date
            """, (u["user_id"], from_date, to_date)).fetchall()
            totals       = {r["work_date"]: r["total_hours"] for r in rows}
            missing_days = []
            for wd in working_days:
                hrs = totals.get(wd.isoformat(), 0)
                if hrs < 8:
                    missing_days.append({"date": wd.isoformat(), "day_name": wd.strftime("%A"),
                                         "entered": hrs, "missing": round(8 - hrs, 2)})
            if missing_days:
                missing_report.append({"name": u["name"], "user_id": u["user_id"],
                    "missing_days": missing_days,
                    "total_missing": sum(d["missing"] for d in missing_days)})

    # ── Daily totals per employee ─────────────────────────────────────────────
    daily_rows = db_execute(conn, f"""
        SELECT u.name, t.work_date, SUM(t.hours) AS day_total
        FROM timesheet_entries t
        JOIN users u ON t.user_id = u.user_id
        WHERE 1=1 {base_filters}
        GROUP BY u.name, t.work_date
        ORDER BY u.name ASC, t.work_date ASC
    """, params).fetchall()

    emp_days = {}
    for r in daily_rows:
        emp_days.setdefault(r["name"], []).append({
            "date":     r["work_date"],
            "day_name": datetime.strptime(r["work_date"], "%Y-%m-%d").strftime("%a"),
            "hours":    r["day_total"],
        })
    daily_breakdown = [
        {"name": name, "days": days, "total": round(sum(d["hours"] for d in days), 2)}
        for name, days in emp_days.items()
    ]

    users     = db_execute(conn, "SELECT user_id, name FROM users ORDER BY name").fetchall()
    show_rows = db_execute(conn,
        "SELECT show_id, year, show_code, show_name FROM shows ORDER BY year, show_code, show_name"
    ).fetchall()
    codes = db_execute(conn, "SELECT work_code_id, code FROM work_codes ORDER BY code").fetchall()
    conn.close()

    return render_template("reports.html",
        entries=entries, summary=summary, missing_report=missing_report,
        daily_breakdown=daily_breakdown,
        users=users, shows=show_rows, codes=codes,
        from_date=from_date, to_date=to_date, report_type=report_type)


@app.route("/reports/export")
@login_required
@admin_required
def export_report():
    from_date    = request.args.get("from_date", "")
    to_date      = request.args.get("to_date", "")
    user_id      = request.args.get("user_id", "")
    show_id      = request.args.get("show_id", "")
    work_code_id = request.args.get("work_code_id", "")
    report_type  = request.args.get("report_type", "custom")

    today = date.today()
    if report_type == "weekly":
        monday    = get_monday(today)
        from_date = monday.isoformat()
        to_date   = (monday + timedelta(days=4)).isoformat()
    elif report_type == "monthly":
        from_date = today.replace(day=1).isoformat()
        to_date   = today.isoformat()

    query  = """
        SELECT u.name, t.work_date, s.year, s.show_code, s.show_name,
               w.code, w.description, t.hours, t.comments
        FROM timesheet_entries t
        JOIN users u       ON t.user_id      = u.user_id
        JOIN work_codes w  ON t.work_code_id = w.work_code_id
        LEFT JOIN shows s  ON t.show_id      = s.show_id
        WHERE 1=1
    """
    params = []
    if from_date:
        query += " AND t.work_date >= ?"
        params.append(from_date)
    if to_date:
        query += " AND t.work_date <= ?"
        params.append(to_date)
    if user_id:
        query += " AND t.user_id = ?"
        params.append(user_id)
    if show_id:
        query += " AND t.show_id = ?"
        params.append(show_id)
    if work_code_id:
        query += " AND t.work_code_id = ?"
        params.append(work_code_id)
    query += " ORDER BY t.work_date DESC, u.name, s.show_code, w.code"

    conn = get_db()
    rows = db_execute(conn, query, params).fetchall()
    conn.close()

    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["User","Date","Year","Show Code","Show Name","Work Code","Description","Hours","Comments"])
    for row in rows:
        writer.writerow([row["name"], row["work_date"], row["year"], row["show_code"],
                         row["show_name"], row["code"], row["description"], row["hours"], row["comments"]])

    filename = f"timesheet_{report_type}_{date.today().isoformat()}.csv"
    return Response(output.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={filename}"})


# ── Admin: Sync Shows ─────────────────────────────────────────────────────────

@app.route("/admin/sync-shows")
@login_required
@admin_required
def admin_sync_shows():
    if not SHEETS_SYNC_ENABLED:
        flash("Google Sheets sync is not configured.", "error")
        return redirect(url_for("dashboard"))
    added, err = sync_shows(DB_NAME)
    if err:
        flash(f"Show sync failed: {err}", "error")
    else:
        flash(f"Show list refreshed — {added} new show(s) added.", "success")
    return redirect(url_for("dashboard"))


# ── Admin: Users ──────────────────────────────────────────────────────────────

@app.route("/admin/users")
@login_required
@admin_required
def admin_users():
    conn  = get_db()
    users = db_execute(conn, "SELECT user_id, name, email, role, team, description, rate_per_hour, hike FROM users ORDER BY team, name").fetchall()
    conn.close()
    return render_template("admin_users.html", users=users,
                           backup_sheet_id=get_setting("backup_sheet_id"))


@app.route("/admin/users/add", methods=["POST"])
@login_required
@admin_required
def admin_add_user():
    name     = request.form["name"].strip()
    email    = request.form["email"].strip().lower()
    password = request.form.get("password", "").strip() or generate_password()
    role     = request.form.get("role", "user")

    conn = get_db()
    try:
        db_execute(conn, """
            INSERT INTO users (name, email, password, role, auth_provider)
            VALUES (?,?,?,?,'local')
        """, (name, email, generate_password_hash(password), role))
        conn.commit()
        flash(f"User {name} created. Password: {password}", "success")
    except Exception:
        conn.rollback()
        flash(f"Email {email} already exists.", "error")
    conn.close()
    return redirect(url_for("admin_users"))


@app.route("/admin/users/delete/<int:user_id>")
@login_required
@admin_required
def admin_delete_user(user_id):
    if user_id == session["user_id"]:
        flash("Cannot delete your own account.", "error")
        return redirect(url_for("admin_users"))
    conn = get_db()
    db_execute(conn, "DELETE FROM notifications WHERE user_id = ?", (user_id,))
    db_execute(conn, "DELETE FROM timesheet_entries WHERE user_id = ?", (user_id,))
    db_execute(conn, "DELETE FROM users WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()
    flash("User deleted.", "success")
    return redirect(url_for("admin_users"))


@app.route("/admin/users/reset-password/<int:user_id>", methods=["POST"])
@login_required
@admin_required
def admin_reset_password(user_id):
    new_password = request.form.get("new_password", "").strip() or generate_password()
    conn = get_db()
    user = db_execute(conn, "SELECT name FROM users WHERE user_id = ?", (user_id,)).fetchone()
    if not user:
        conn.close()
        flash("User not found.", "error")
        return redirect(url_for("admin_users"))
    db_execute(conn, "UPDATE users SET password = ? WHERE user_id = ?",
               (generate_password_hash(new_password), user_id))
    conn.commit()
    conn.close()
    flash(f"Password for {user['name']} reset to: {new_password} — share this with them directly.", "success")
    return redirect(url_for("admin_users"))


@app.route("/admin/users/generate-password")
@login_required
@admin_required
def admin_generate_password():
    return {"password": generate_password()}


# ── Admin: Edit User ─────────────────────────────────────────────────────────

@app.route("/admin/users/edit/<int:user_id>", methods=["POST"])
@login_required
@admin_required
def admin_edit_user(user_id):
    name          = request.form.get("name", "").strip()
    email         = request.form.get("email", "").strip().lower()
    role          = request.form.get("role", "user")
    team          = request.form.get("team", "").strip()
    description   = request.form.get("description", "").strip()
    rate_per_hour = request.form.get("rate_per_hour", "0").strip()
    hike          = request.form.get("hike", "0").strip()

    if not name or not email:
        flash("Name and email are required.", "error")
        return redirect(url_for("admin_users"))

    try:
        rate_per_hour = float(rate_per_hour) if rate_per_hour else 0
        hike          = float(hike) if hike else 0
    except ValueError:
        flash("Rate and hike must be numbers.", "error")
        return redirect(url_for("admin_users"))

    conn = get_db()
    try:
        db_execute(conn, """
            UPDATE users SET name=?, email=?, role=?, team=?, description=?, rate_per_hour=?, hike=?
            WHERE user_id=?
        """, (name, email, role, team, description, rate_per_hour, hike, user_id))
        conn.commit()
        flash(f"User updated successfully.", "success")
    except Exception:
        conn.rollback()
        flash("Email already in use by another user.", "error")
    conn.close()
    return redirect(url_for("admin_users"))


@app.route("/admin/users/sync/<int:user_id>")
@login_required
@admin_required
def admin_sync_user(user_id):
    conn = get_db()
    user = db_execute(conn, "SELECT name FROM users WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    if not user:
        flash("User not found.", "error")
        return redirect(url_for("admin_users"))
    do_sheets_sync(user_id)
    flash(f"Sync to live sheet triggered for {user['name']}.", "success")
    return redirect(url_for("admin_users"))


@app.route("/admin/users/sync-backup/<int:user_id>")
@login_required
@admin_required
def admin_sync_user_backup(user_id):
    if not SHEETS_SYNC_ENABLED:
        flash("Google Sheets sync is not configured.", "error")
        return redirect(url_for("admin_users"))

    backup_id = get_setting("backup_sheet_id")
    if not backup_id:
        flash("No backup sheet ID saved. Paste it in the Backup Sheet panel first.", "error")
        return redirect(url_for("admin_users"))

    conn = get_db()
    user = db_execute(conn, "SELECT name FROM users WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    if not user:
        flash("User not found.", "error")
        return redirect(url_for("admin_users"))

    prev_start, prev_end = prev_month_range_ist()
    ok, err = sync_user_to_sheet(user_id, spreadsheet_id=backup_id,
                                 from_date=prev_start, to_date=prev_end)
    if ok:
        flash(f"Backup sync complete for {user['name']} ({prev_start[:7]}).", "success")
    else:
        flash(f"Backup sync failed for {user['name']}: {err}", "error")
    return redirect(url_for("admin_users"))


@app.route("/admin/settings/backup-sheet", methods=["POST"])
@login_required
@admin_required
def admin_save_backup_sheet():
    sheet_id = request.form.get("backup_sheet_id", "").strip()
    set_setting("backup_sheet_id", sheet_id)
    if sheet_id:
        flash("Backup sheet ID saved.", "success")
    else:
        flash("Backup sheet ID cleared.", "success")
    return redirect(url_for("admin_users"))


# ── Vacation Notification ─────────────────────────────────────────────────────

@app.route("/vacation", methods=["GET", "POST"])
@login_required
def vacation():
    if session.get("role") == "admin":
        flash("This page is for team members only.", "error")
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        dates   = request.form.getlist("vac_date")
        hours   = request.form.getlist("vac_hours")
        notes   = request.form.get("vac_notes", "").strip()
        entries = []

        for d, h in zip(dates, hours):
            d = d.strip()
            h = h.strip()
            if not d or not h:
                continue
            try:
                hrs = float(h)
                if hrs <= 0 or hrs > 24:
                    raise ValueError
            except ValueError:
                flash(f"Invalid hours for {d}. Must be between 0.25 and 24.", "error")
                return redirect(url_for("vacation"))
            entries.append({"date": d, "hours": hrs})

        if not entries:
            flash("Please add at least one vacation date.", "error")
            return redirect(url_for("vacation"))

        total_hours = sum(e["hours"] for e in entries)
        name        = session.get("name")
        user_id_bg  = session["user_id"]

        # ── All heavy work in background thread to prevent 502 ────────────────
        entries_copy = list(entries)
        notes_copy   = notes

        def _process_vacation():
            try:
                # DB inserts
                conn = get_db()
                now  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                vac_show = db_execute(conn, """
                    SELECT show_id FROM shows
                    WHERE LOWER(show_name) = 'vacation' LIMIT 1
                """).fetchone()

                vac_code = db_execute(conn, """
                    SELECT work_code_id FROM work_codes
                    WHERE LOWER(code) = 'vacation' LIMIT 1
                """).fetchone()

                logged = []
                if vac_show and vac_code:
                    for e in entries_copy:
                        existing = db_execute(conn, """
                            SELECT entry_id FROM timesheet_entries
                            WHERE user_id = ? AND work_date = ? AND show_id = ? AND work_code_id = ?
                        """, (user_id_bg, e["date"], vac_show["show_id"], vac_code["work_code_id"])).fetchone()
                        if not existing:
                            db_execute(conn, """
                                INSERT INTO timesheet_entries
                                (user_id, show_id, work_code_id, work_date, hours, comments, created_at, updated_at)
                                VALUES (?,?,?,?,?,?,?,?)
                            """, (user_id_bg, vac_show["show_id"], vac_code["work_code_id"],
                                  e["date"], e["hours"], notes_copy or "Vacation", now, now))
                            logged.append(e)
                    conn.commit()
                conn.close()
                print(f"[Vacation] {len(logged)} entries logged for user {user_id_bg}")

                # Sheets sync
                if logged:
                    do_sheets_sync(user_id_bg)

                # Email
                rows_html = "".join(f"""
                    <tr>
                        <td style="padding:8px 14px;border-bottom:1px solid #eee;">
                            {datetime.strptime(e["date"], "%Y-%m-%d").strftime("%A, %B %d, %Y")}
                        </td>
                        <td style="padding:8px 14px;border-bottom:1px solid #eee;font-weight:600;">
                            {e["hours"]} hrs
                        </td>
                    </tr>
                """ for e in entries_copy)

                notes_section = f"<p style='margin-top:16px;'><strong>Notes:</strong> {notes_copy}</p>" if notes_copy else ""

                html_body = f"""
                <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:20px;">
                    <div style="background:#fff8f0;border-left:4px solid #ff9800;padding:20px;border-radius:4px;">
                        <h2 style="margin-top:0;color:#e65100;">🌴 Vacation Request — {name}</h2>
                        <p style="color:#555;">
                            <strong>{name}</strong> has submitted a vacation notification for
                            <strong>{len(entries_copy)} day{"s" if len(entries_copy) != 1 else ""}</strong>
                            totalling <strong>{total_hours} hrs</strong>.
                        </p>
                        <table style="width:100%;border-collapse:collapse;margin-top:16px;">
                            <thead>
                                <tr style="background:#ff9800;color:white;">
                                    <th style="padding:10px 14px;text-align:left;">Date</th>
                                    <th style="padding:10px 14px;text-align:left;">Hours</th>
                                </tr>
                            </thead>
                            <tbody>{rows_html}</tbody>
                            <tfoot>
                                <tr style="background:#f5f5f5;font-weight:bold;">
                                    <td style="padding:10px 14px;">Total</td>
                                    <td style="padding:10px 14px;">{total_hours} hrs</td>
                                </tr>
                            </tfoot>
                        </table>
                        {notes_section}
                        <p style="color:#888;font-size:12px;margin-top:20px;">
                            Hours have been automatically logged in the timesheet and synced to the master sheet.
                        </p>
                    </div>
                    <p style="color:#aaa;font-size:12px;margin-top:20px;text-align:center;">
                        Zydesoft Timesheet System
                    </p>
                </div>"""

                resend_key = os.environ.get("RESEND_API_KEY", "")
                if resend_key:
                    try:
                        import resend
                        resend.api_key = resend_key
                        resend.Emails.send({
                            "from":    "Zydesoft Timesheet <noreply@zydesoft.com>",
                            "to":      VACATION_NOTIFY_EMAILS,
                            "subject": f"🌴 Vacation Notice — {name} ({len(entries_copy)} day{'s' if len(entries_copy) != 1 else ''}, {total_hours} hrs)",
                            "html":    html_body
                        })
                        print(f"[Vacation] Email sent for {name}")
                    except Exception as email_err:
                        print(f"[Vacation] Email failed: {email_err}")
                else:
                    print("[Vacation] RESEND_API_KEY not set — skipping email")


            except Exception as ex:
                print(f"[Vacation] Background processing error: {ex}")

        threading.Thread(target=_process_vacation, daemon=True).start()

        flash(f"✅ Vacation request submitted — {len(entries)} day(s), {total_hours} hrs. Logging and notification in progress.", "success")
        return redirect(url_for("vacation"))

    # Fetch this year's vacation entries for display
    conn  = get_db()
    year  = date.today().year

    vacation_entries = db_execute(conn, """
       SELECT t.work_date, t.hours, t.comments
        FROM timesheet_entries t
        JOIN work_codes w ON t.work_code_id = w.work_code_id
        LEFT JOIN shows s ON t.show_id = s.show_id
        WHERE t.user_id = ?
          AND LOWER(w.code) = 'vacation'
          AND LOWER(COALESCE(s.show_name, '')) != 'holiday'
          AND t.work_date BETWEEN ? AND ?
        ORDER BY t.work_date ASC
    """, (session["user_id"], f"{year}-01-01", f"{year}-12-31")).fetchall()
    conn.close()

    return render_template("vacation.html", vacation_entries=vacation_entries, year=year)




# ── Bulk Import Endpoint ──────────────────────────────────────────────────────

IMPORT_API_KEY = "zydesoft-import-2026"

@app.route("/admin/import-entries", methods=["POST"])
def import_entries():
    """
    Bulk import endpoint called by import_may_entries.gs Apps Script.
    Accepts JSON: { api_key, entries: [{employee_name, work_date, show_label, work_code, hours, comments}] }
    Returns: { ok, imported, skipped }
    """
    data = request.get_json(silent=True)
    if not data:
        return {"ok": False, "error": "Invalid JSON"}, 400

    if data.get("api_key") != IMPORT_API_KEY:
        return {"ok": False, "error": "Unauthorized"}, 401

    entries  = data.get("entries", [])
    imported = 0
    skipped  = 0
    now      = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn = get_db()

    for e in entries:
        try:
            employee_name = e.get("employee_name", "").strip()
            work_date     = e.get("work_date", "").strip()
            show_label    = e.get("show_label", "").strip()
            work_code     = e.get("work_code", "").strip()
            hours         = float(e.get("hours", 0))
            comments      = e.get("comments", "").strip() or work_code

            if not employee_name or not work_date or not work_code or hours <= 0:
                skipped += 1
                continue

            # Find user by name
            user = db_execute(conn,
                "SELECT user_id FROM users WHERE name = ?", (employee_name,)
            ).fetchone()
            if not user:
                print(f"[Import] User not found: {employee_name}")
                skipped += 1
                continue

            user_id = user["user_id"]

            # Find work code
            wc = db_execute(conn,
                "SELECT work_code_id FROM work_codes WHERE LOWER(code) = LOWER(?)", (work_code,)
            ).fetchone()
            if not wc:
                print(f"[Import] Work code not found: {work_code}")
                skipped += 1
                continue

            work_code_id = wc["work_code_id"]

            # Find show by label — format "YEAR - CODE - NAME" or just show name
            show_id = None
            if show_label:
                parts = [p.strip() for p in show_label.split(" - ", 2)]
                if len(parts) == 3:
                    show = db_execute(conn, """
                        SELECT show_id FROM shows
                        WHERE year = ? AND show_code = ? AND show_name = ?
                    """, (parts[0], parts[1], parts[2])).fetchone()
                elif len(parts) == 1:
                    show = db_execute(conn, """
                        SELECT show_id FROM shows
                        WHERE LOWER(show_name) = LOWER(?)
                    """, (parts[0],)).fetchone()
                else:
                    show = None

                if show:
                    show_id = show["show_id"]

            # Skip only if exact duplicate (same user, date, show, workcode, hours AND comments)
            existing = db_execute(conn, """
                SELECT entry_id FROM timesheet_entries
                WHERE user_id = ? AND work_date = ? AND work_code_id = ?
                  AND show_id IS NOT DISTINCT FROM ? AND hours = ? AND comments = ?
            """ if DATABASE_URL else """
                SELECT entry_id FROM timesheet_entries
                WHERE user_id = ? AND work_date = ? AND work_code_id = ?
                  AND (show_id = ? OR (show_id IS NULL AND ? IS NULL)) AND hours = ? AND comments = ?
            """,
            (user_id, work_date, work_code_id, show_id, hours, comments) if DATABASE_URL else
            (user_id, work_date, work_code_id, show_id, show_id, hours, comments)
            ).fetchone()

            if existing:
                skipped += 1
                continue

            db_execute(conn, """
                INSERT INTO timesheet_entries
                (user_id, show_id, work_code_id, work_date, hours, comments, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?)
            """, (user_id, show_id, work_code_id, work_date, hours, comments, now, now))
            imported += 1

        except Exception as ex:
            print(f"[Import] Row error: {ex}")
            skipped += 1

    conn.commit()
    conn.close()

    print(f"[Import] Done — {imported} imported, {skipped} skipped")
    return {"ok": True, "imported": imported, "skipped": skipped}


# ── Password Reset ────────────────────────────────────────────────────────────

import secrets as _secrets

@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        conn  = get_db()
        user  = db_execute(conn, "SELECT user_id, name FROM users WHERE email = ?", (email,)).fetchone()
        conn.close()

        # Always show success to prevent email enumeration
        if user:
            token     = _secrets.token_urlsafe(32)
            token_exp = (datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")

            conn = get_db()
            db_execute(conn, """
                UPDATE users SET reset_token = ?, reset_token_exp = ? WHERE user_id = ?
            """, (token, token_exp, user["user_id"]))
            conn.commit()
            conn.close()

            reset_url = url_for("reset_password", token=token, _external=True)

            # Send email via Resend
            def _send_reset():
                try:
                    import resend
                    resend.api_key = os.environ.get("RESEND_API_KEY", "")
                    resend.Emails.send({
                        "from":    "Zydesoft Timesheet <noreply@zydesoft.com>",
                        "to":      [email],
                        "subject": "Reset your timesheet password",
                        "html":    f"""
                        <div style="font-family:Arial,sans-serif;max-width:500px;margin:0 auto;padding:24px;">
                            <h2 style="color:#1a2236;">Reset Your Password</h2>
                            <p>Hi {user["name"]},</p>
                            <p>Click the button below to reset your password. This link expires in 1 hour.</p>
                            <a href="{reset_url}"
                               style="display:inline-block;background:#1a2236;color:#fff;
                                      padding:12px 24px;border-radius:6px;text-decoration:none;
                                      font-weight:bold;margin:16px 0;">
                                Reset Password
                            </a>
                            <p style="color:#888;font-size:12px;">If you didn't request this, ignore this email.</p>
                            <p style="color:#aaa;font-size:11px;">Link: {reset_url}</p>
                        </div>"""
                    })
                    print(f"[PasswordReset] Email sent to {email}")
                except Exception as ex:
                    print(f"[PasswordReset] Email failed: {ex}")

            threading.Thread(target=_send_reset, daemon=True).start()

        flash("If that email exists, a reset link has been sent.", "success")
        return redirect(url_for("forgot_password"))

    return render_template("forgot_password.html")


@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    conn = get_db()
    user = db_execute(conn, """
        SELECT user_id, name FROM users
        WHERE reset_token = ? AND reset_token_exp > ?
    """, (token, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))).fetchone()
    conn.close()

    if not user:
        flash("This reset link is invalid or has expired. Please request a new one.", "error")
        return redirect(url_for("forgot_password"))

    if request.method == "POST":
        password = request.form.get("password", "").strip()
        confirm  = request.form.get("confirm", "").strip()

        if len(password) < 8:
            flash("Password must be at least 8 characters.", "error")
            return render_template("reset_password.html", token=token, name=user["name"])

        if password != confirm:
            flash("Passwords do not match.", "error")
            return render_template("reset_password.html", token=token, name=user["name"])

        conn = get_db()
        db_execute(conn, """
            UPDATE users SET password = ?, reset_token = NULL, reset_token_exp = NULL
            WHERE user_id = ?
        """, (generate_password_hash(password), user["user_id"]))
        conn.commit()
        conn.close()

        flash("Password updated successfully. Please log in.", "success")
        return redirect(url_for("login"))

    return render_template("reset_password.html", token=token, name=user["name"])

@app.route("/admin/add-entry", methods=["GET", "POST"])
@login_required
@admin_required
def admin_add_entry():
    conn  = get_db()
    users = db_execute(conn, "SELECT user_id, name FROM users WHERE role='user' ORDER BY name").fetchall()
    shows = db_execute(conn, "SELECT show_id, year, show_code, show_name FROM shows WHERE active_flag='Y' ORDER BY year DESC, show_name").fetchall()
    codes = db_execute(conn, "SELECT work_code_id, code FROM work_codes ORDER BY code").fetchall()

    if request.method == "POST":
        user_id      = request.form.get("user_id", "").strip()
        show_id      = request.form.get("show_id", "").strip()
        work_code_id = request.form.get("work_code_id", "").strip()
        work_date    = request.form.get("work_date", "").strip()
        hours        = request.form.get("hours", "").strip()
        comments     = request.form.get("comments", "").strip()

        if not all([user_id, show_id, work_code_id, work_date, hours, comments]):
            flash("All fields are required.", "error")
        else:
            try:
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                db_execute(conn, """
                    INSERT INTO timesheet_entries
                    (user_id, show_id, work_code_id, work_date, hours, comments, created_at, updated_at)
                    VALUES (?,?,?,?,?,?,?,?)
                """, (user_id, show_id, work_code_id, work_date, float(hours), comments, now, now))
                conn.commit()

                # Sync to sheet
                do_sheets_sync(int(user_id))

                # Get user name for confirmation
                user = db_execute(conn, "SELECT name FROM users WHERE user_id=?", (user_id,)).fetchone()
                flash(f"Entry added for {user['name']} on {work_date}.", "success")
            except Exception as e:
                conn.rollback()
                flash(f"Error adding entry: {str(e)}", "error")

    conn.close()
    return render_template("admin_add_entry.html",
        users=users, shows=shows, codes=codes,
        today=date.today().isoformat()
    )
# ── Startup ───────────────────────────────────────────────────────────────────

with app.app_context():
    init_db()

@app.route("/health")
def health():
    return {"status": "ok"}, 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, host="0.0.0.0", port=port)
