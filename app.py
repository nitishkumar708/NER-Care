"""
NER-Care: AI-Based Cognitive Gaming and Memory Assistance Platform
SIH 2026 - Problem Statement 26003

Backend architecture:
- Flask + SQLite
- Universal game-result data layer
- Game/domain registry
- Longitudinal cognitive analytics
- Explainable change detection
- Adaptive difficulty
- Future ML-compatible architecture

IMPORTANT:
This system provides cognitive performance monitoring and decision-support
signals. It is NOT a medical diagnosis engine.
"""

from flask import Flask, render_template, request, jsonify, session, redirect, url_for, flash
import sqlite3
import os
import json
from datetime import datetime, timedelta
from functools import wraps
import secrets
import string
from werkzeug.security import generate_password_hash, check_password_hash


# ============================================================================
# APP CONFIGURATION
# ============================================================================

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Database location. On a host with a persistent folder, set NERCARE_DB_PATH
# to a file in that folder so user data survives re-deploys.
DB_PATH = os.environ.get("NERCARE_DB_PATH", os.path.join(BASE_DIR, "nercare.db"))


def _load_secret_key():
    """Use NERCARE_SECRET_KEY if set, otherwise create and reuse a random key
    stored next to the database (so logins survive restarts)."""
    env_key = os.environ.get("NERCARE_SECRET_KEY")
    if env_key:
        return env_key
    key_file = os.path.join(os.path.dirname(DB_PATH), ".nercare_secret_key")
    try:
        with open(key_file) as f:
            key = f.read().strip()
            if key:
                return key
    except OSError:
        pass
    key = secrets.token_hex(32)
    try:
        with open(key_file, "w") as f:
            f.write(key)
        os.chmod(key_file, 0o600)
    except OSError:
        pass
    return key


app.secret_key = _load_secret_key()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=bool(os.environ.get("NERCARE_HTTPS")),
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
)


# ============================================================================
# DATABASE CONNECTION
# ============================================================================

def get_db():
    # timeout: wait for a competing write instead of failing with
    # "database is locked" when several people play at the same time.
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        # WAL needs shared-memory files and fails on some hosts' file systems
        # (e.g. PythonAnywhere), so it is opt-in: set NERCARE_WAL=1 to use it.
        if os.environ.get("NERCARE_WAL") == "1":
            conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
    except sqlite3.DatabaseError:
        pass
    return conn


# ============================================================================
# UNIVERSAL COGNITIVE DOMAINS
# ============================================================================
#
# Every future game can contribute to one or more of these domains.
#
# The AI engine works with domains, NOT hard-coded game names.
#
# This is the key feature that makes the architecture scalable.
# ============================================================================

COGNITIVE_DOMAINS = [
    "memory",
    "attention",
    "processing_speed",
    "executive_function",
    "visuospatial",
    "language",
    "orientation"
]


# ============================================================================
# DEFAULT GAME DOMAIN MAPPINGS
# ============================================================================
#
# Existing games are mapped here so they immediately work with the new system.
#
# Future games DO NOT need to be added here.
# A future game can send its own "domains" through /api/submit-score.
#
# Example future game:
#
# {
#     "game_name": "sequence_recall",
#     "domains": ["memory", "attention"]
# }
#
# The system will automatically register it.
# ============================================================================

DEFAULT_GAME_DOMAINS = {
    "memory_match": ["memory", "attention"],
    "memory match": ["memory", "attention"],

    "pattern_recognition": ["attention", "visuospatial"],
    "pattern recognition": ["attention", "visuospatial"],

    "daily_routine": ["memory", "executive_function", "orientation"],
    "daily routine": ["memory", "executive_function", "orientation"],
    # The Daily Routine game submits itself under this name.
    "daily_routine_recall": ["memory", "executive_function", "orientation"],

    "object_recognition": ["memory", "visuospatial"],
    "object recognition": ["memory", "visuospatial"],
}


# ============================================================================
# DATABASE SETUP + SAFE MIGRATION
# ============================================================================

def column_exists(conn, table_name, column_name):
    """
    Check whether a column already exists.

    This allows us to upgrade the existing SQLite database without
    deleting the user's existing game history.
    """

    columns = conn.execute(
        f"PRAGMA table_info({table_name})"
    ).fetchall()

    return any(column["name"] == column_name for column in columns)


def add_column_if_missing(conn, table_name, column_name, column_definition):
    """
    Add a database column only if it does not already exist.
    """

    if not column_exists(conn, table_name, column_name):
        conn.execute(
            f"ALTER TABLE {table_name} "
            f"ADD COLUMN {column_name} {column_definition}"
        )


def generate_unique_code(conn, prefix, table, column, length=6):
    alphabet = string.ascii_uppercase + string.digits
    while True:
        code = prefix + "".join(secrets.choice(alphabet) for _ in range(length))
        row = conn.execute(
            f"SELECT 1 FROM {table} WHERE {column} = ? LIMIT 1",
            (code,)
        ).fetchone()
        if not row:
            return code


def generate_invite_token(conn):
    while True:
        token = secrets.token_urlsafe(24)
        row = conn.execute(
            "SELECT 1 FROM patient_invitations WHERE token = ? LIMIT 1",
            (token,)
        ).fetchone()
        if not row:
            return token


def ensure_identity_codes(conn):
    caregivers = conn.execute("SELECT id, caregiver_code FROM caregivers").fetchall()
    for caregiver in caregivers:
        if not caregiver["caregiver_code"]:
            code = generate_unique_code(conn, "CARE-NC-", "caregivers", "caregiver_code")
            conn.execute("UPDATE caregivers SET caregiver_code = ? WHERE id = ?", (code, caregiver["id"]))

    patients = conn.execute("SELECT id, patient_code FROM patients").fetchall()
    for patient in patients:
        if not patient["patient_code"]:
            code = generate_unique_code(conn, "NER-P-", "patients", "patient_code")
            conn.execute("UPDATE patients SET patient_code = ? WHERE id = ?", (code, patient["id"]))

    # Backfill relationship rows from the existing primary caregiver_id field.
    conn.execute("""
        INSERT OR IGNORE INTO caregiver_patient (caregiver_id, patient_id, linked_at)
        SELECT caregiver_id, id, ?
        FROM patients
        WHERE caregiver_id IS NOT NULL
    """, (datetime.now().isoformat(),))


def init_db():

    conn = get_db()
    c = conn.cursor()

    # ------------------------------------------------------------------------
    # USERS / AUTHENTICATION
    # ------------------------------------------------------------------------
    # One user account can own a caregiver profile and its patient profiles.
    # Passwords are stored as secure hashes, never as plain text.
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'caregiver',
            caregiver_id INTEGER,
            patient_id INTEGER,
            created_at TEXT NOT NULL
        )
    """)

    # ------------------------------------------------------------------------
    # PATIENTS
    # ------------------------------------------------------------------------

    c.execute("""
        CREATE TABLE IF NOT EXISTS patients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            age INTEGER,
            language TEXT DEFAULT 'en',
            caregiver_id INTEGER,
            difficulty_level INTEGER DEFAULT 1
        )
    """)

    # Existing databases are upgraded safely without deleting patient data.
    add_column_if_missing(conn, "patients", "user_id", "INTEGER")
    add_column_if_missing(conn, "patients", "patient_code", "TEXT")

    # ------------------------------------------------------------------------
    # CAREGIVERS
    # ------------------------------------------------------------------------

    c.execute("""
        CREATE TABLE IF NOT EXISTS caregivers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            phone TEXT,
            caregiver_code TEXT UNIQUE
        )
    """)

    add_column_if_missing(conn, "users", "patient_id", "INTEGER")
    add_column_if_missing(conn, "caregivers", "caregiver_code", "TEXT")

    # ------------------------------------------------------------------------
    # CAREGIVER-PATIENT RELATIONSHIPS
    # ------------------------------------------------------------------------
    c.execute("""
        CREATE TABLE IF NOT EXISTS caregiver_patient (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            caregiver_id INTEGER NOT NULL,
            patient_id INTEGER NOT NULL,
            linked_at TEXT NOT NULL,
            UNIQUE(caregiver_id, patient_id),
            FOREIGN KEY (caregiver_id) REFERENCES caregivers(id),
            FOREIGN KEY (patient_id) REFERENCES patients(id)
        )
    """)

    # ------------------------------------------------------------------------
    # PATIENT INVITATIONS
    # ------------------------------------------------------------------------
    c.execute("""
        CREATE TABLE IF NOT EXISTS patient_invitations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            caregiver_id INTEGER NOT NULL,
            token TEXT UNIQUE NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            used_at TEXT,
            FOREIGN KEY (caregiver_id) REFERENCES caregivers(id)
        )
    """)

    # ------------------------------------------------------------------------
    # EXISTING GAME SCORE TABLE
    # ------------------------------------------------------------------------

    c.execute("""
        CREATE TABLE IF NOT EXISTS game_scores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            patient_id INTEGER NOT NULL,
            game_name TEXT NOT NULL,
            score INTEGER,
            time_taken_seconds REAL,
            difficulty_level INTEGER,
            played_at TEXT,
            FOREIGN KEY (patient_id) REFERENCES patients (id)
        )
    """)

    # ------------------------------------------------------------------------
    # UPGRADE EXISTING GAME SCORE TABLE
    # ------------------------------------------------------------------------
    #
    # These columns make the data layer future-proof.
    #
    # Existing rows remain untouched.
    # New rows can contain richer cognitive measurements.
    # ------------------------------------------------------------------------

    add_column_if_missing(
        conn,
        "game_scores",
        "max_score",
        "REAL"
    )

    add_column_if_missing(
        conn,
        "game_scores",
        "accuracy",
        "REAL"
    )

    add_column_if_missing(
        conn,
        "game_scores",
        "expected_time_seconds",
        "REAL"
    )

    add_column_if_missing(
        conn,
        "game_scores",
        "errors",
        "INTEGER DEFAULT 0"
    )

    add_column_if_missing(
        conn,
        "game_scores",
        "hints_used",
        "INTEGER DEFAULT 0"
    )

    add_column_if_missing(
        conn,
        "game_scores",
        "completion_status",
        "TEXT DEFAULT 'completed'"
    )

    add_column_if_missing(
        conn,
        "game_scores",
        "domains_json",
        "TEXT DEFAULT '[]'"
    )

    add_column_if_missing(
        conn,
        "game_scores",
        "metrics_json",
        "TEXT DEFAULT '{}'"
    )

    # ------------------------------------------------------------------------
    # VALIDATION SESSIONS
    # ------------------------------------------------------------------------
    # Stores caregiver/game sandbox tests separately from real patient scores.
    c.execute("""
        CREATE TABLE IF NOT EXISTS validation_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            caregiver_id INTEGER NOT NULL,
            game_name TEXT NOT NULL,
            score REAL,
            max_score REAL,
            accuracy REAL,
            time_taken_seconds REAL,
            expected_time_seconds REAL,
            errors INTEGER DEFAULT 0,
            hints_used INTEGER DEFAULT 0,
            completion_status TEXT DEFAULT 'completed',
            domains_json TEXT DEFAULT '[]',
            metrics_json TEXT DEFAULT '{}',
            tested_at TEXT NOT NULL,
            FOREIGN KEY (caregiver_id) REFERENCES caregivers(id)
        )
    """)
    # ------------------------------------------------------------------------
    # PATIENT GAME PROGRESSION
    # ------------------------------------------------------------------------
    #
    # Stores independent progression for every patient + every game.
    #
    # This is separate from the patient-wide difficulty_level because
    # each game can have its own progression.
    #
    # Future games automatically get their own progression record
    # when the patient starts playing them.
    # ------------------------------------------------------------------------
    c.execute("""
        CREATE TABLE IF NOT EXISTS patient_game_progress (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            patient_id INTEGER NOT NULL,

            game_name TEXT NOT NULL,

            current_level INTEGER DEFAULT 1,

            highest_level INTEGER DEFAULT 1,

            total_sessions INTEGER DEFAULT 0,

            completed_sessions INTEGER DEFAULT 0,

            last_score REAL,

            last_accuracy REAL,

            last_time_taken_seconds REAL,

            last_played_at TEXT,

            created_at TEXT NOT NULL,

            updated_at TEXT NOT NULL,

            UNIQUE(patient_id, game_name),

            FOREIGN KEY (patient_id) REFERENCES patients(id)
        )
    """)


    # ------------------------------------------------------------------------
    # LEGACY ACCURACY CLEANUP
    # ------------------------------------------------------------------------
    # Older sessions did not record real accuracy. Do not treat their
    # migrated zero values as genuine 0% performance.
    conn.execute(
        """
        UPDATE game_scores
        SET accuracy = NULL
        WHERE accuracy = 0
          AND (domains_json IS NULL OR domains_json = '' OR domains_json = '[]')
          AND (metrics_json IS NULL OR metrics_json = '' OR metrics_json = '{}')
        """
    )

    # Backfill: sessions saved without domains (e.g. daily_routine_recall)
    # get the domains of their game so they count in domain analytics.
    for _game, _domains in DEFAULT_GAME_DOMAINS.items():
        _key = _game.replace(" ", "_")
        conn.execute(
            """
            UPDATE game_scores SET domains_json = ?
            WHERE game_name = ?
              AND (domains_json IS NULL OR domains_json = '' OR domains_json = '[]')
            """,
            (json.dumps(_domains), _key)
        )

    # ------------------------------------------------------------------------
    # CAREGIVER OBSERVATIONS
    # ------------------------------------------------------------------------
    # Manual observations complement game-performance analytics.
    c.execute("""
        CREATE TABLE IF NOT EXISTS caregiver_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            patient_id INTEGER NOT NULL,
            category TEXT NOT NULL,
            status TEXT DEFAULT 'noted',
            note TEXT,
            observed_at TEXT NOT NULL,
            FOREIGN KEY (patient_id) REFERENCES patients (id)
        )
    """)

    # ------------------------------------------------------------------------
    # REMINDERS
    # ------------------------------------------------------------------------

    c.execute("""
        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            patient_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            category TEXT,
            reminder_time TEXT,
            is_done INTEGER DEFAULT 0,
            FOREIGN KEY (patient_id) REFERENCES patients (id)
        )
    """)

    # ------------------------------------------------------------------------
    # GAME REGISTRY
    # ------------------------------------------------------------------------
    #
    # This is extremely important.
    #
    # The AI engine does NOT need to know every future game in advance.
    #
    # A game can register itself by submitting:
    #
    # game_name
    # domains
    #
    # The registry stores that information.
    # ------------------------------------------------------------------------

    c.execute("""
        CREATE TABLE IF NOT EXISTS game_registry (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            game_name TEXT UNIQUE NOT NULL,
            display_name TEXT,
            domains_json TEXT DEFAULT '[]',
            created_at TEXT
        )
    """)

    # ------------------------------------------------------------------------
    # GAME REGISTRY CONFIGURATION
    # ------------------------------------------------------------------------
    # These fields make the game registry the central configuration
    # for every current and future game.
    # ------------------------------------------------------------------------

    add_column_if_missing(
        conn,
        "game_registry",
        "template_name",
        "TEXT"
    )

    add_column_if_missing(
        conn,
        "game_registry",
        "progression_enabled",
        "INTEGER DEFAULT 1"
    )

    add_column_if_missing(
        conn,
        "game_registry",
        "validation_enabled",
        "INTEGER DEFAULT 1"
    )

    add_column_if_missing(
        conn,
        "game_registry",
        "max_level",
        "INTEGER DEFAULT 0"
    )

    conn.commit()

    # ------------------------------------------------------------------------
    # REGISTER CURRENT GAMES
    # ------------------------------------------------------------------------

    for game_name, domains in DEFAULT_GAME_DOMAINS.items():

        display_name = game_name.replace("_", " ").title()

        c.execute(
            """
            INSERT OR IGNORE INTO game_registry
            (game_name, display_name, domains_json, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                game_name,
                display_name,
                json.dumps(domains),
                datetime.now().isoformat()
            )
        )

    conn.commit()
    ensure_identity_codes(conn)
    conn.commit()

    # ------------------------------------------------------------------------
    # DEMO PATIENT + CAREGIVER
    # ------------------------------------------------------------------------

    c.execute("SELECT COUNT(*) FROM patients")

    if c.fetchone()[0] == 0:

        c.execute(
            """
            INSERT INTO caregivers (name, phone)
            VALUES (?, ?)
            """,
            (
                "Demo Caregiver",
                "9999999999"
            )
        )

        caregiver_id = c.lastrowid

        c.execute(
            """
            INSERT INTO patients
            (name, age, language, caregiver_id, difficulty_level)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                "Demo Patient",
                68,
                "en",
                caregiver_id,
                1
            )
        )

        conn.commit()

    # ------------------------------------------------------------------------
    # DEMO USER + SAFE BACKFILL
    # ------------------------------------------------------------------------
    # Existing prototype data is assigned to the demo account so the old demo
    # remains usable after authentication is introduced.
    demo_user = c.execute(
        "SELECT * FROM users WHERE email = ?",
        ("demo@nercare.local",)
    ).fetchone()

    if not demo_user:
        demo_caregiver = c.execute(
            "SELECT id FROM caregivers ORDER BY id LIMIT 1"
        ).fetchone()

        demo_caregiver_id = demo_caregiver["id"] if demo_caregiver else None

        c.execute(
            """
            INSERT INTO users
            (name, email, password_hash, role, caregiver_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "Demo Caregiver",
                "demo@nercare.local",
                generate_password_hash("Demo@123"),
                "caregiver",
                demo_caregiver_id,
                datetime.now().isoformat()
            )
        )

    demo_user = c.execute(
        "SELECT id, caregiver_id FROM users WHERE email = ?",
        ("demo@nercare.local",)
    ).fetchone()

    if demo_user:
        # If an old database already had a caregiver but the user row did not,
        # connect the demo user to that caregiver.
        if demo_user["caregiver_id"] is None:
            caregiver_row = c.execute(
                "SELECT id FROM caregivers ORDER BY id LIMIT 1"
            ).fetchone()
            if caregiver_row:
                c.execute(
                    "UPDATE users SET caregiver_id = ? WHERE id = ?",
                    (caregiver_row["id"], demo_user["id"])
                )
                demo_caregiver_id = caregiver_row["id"]
            else:
                demo_caregiver_id = None
        else:
            demo_caregiver_id = demo_user["caregiver_id"]

        if demo_caregiver_id:
            # Legacy prototype patients remain caregiver-linked. They are not
            # assigned to the caregiver's login as a patient account.
            c.execute(
                "UPDATE patients SET user_id = NULL WHERE caregiver_id = ? AND user_id = ?",
                (demo_caregiver_id, demo_user["id"])
            )

    ensure_identity_codes(conn)
    conn.commit()
    conn.close()


# ============================================================================
# AUTHENTICATION HELPERS
# ============================================================================


def current_user():
    """Return the logged-in user row, or None when no session exists."""
    user_id = session.get("user_id")
    if not user_id:
        return None

    conn = get_db()
    user = conn.execute(
        "SELECT * FROM users WHERE id = ?",
        (user_id,)
    ).fetchone()
    conn.close()
    return user


def login_required(view):
    """Protect application pages and APIs behind authentication."""
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if not current_user():
            if request.path.startswith("/api/"):
                return jsonify({
                    "status": "error",
                    "message": "Authentication required"
                }), 401
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped_view


def patient_belongs_to_user(patient_id, user):
    """Check that the logged-in user may access this patient's data."""
    if not user:
        return False

    conn = get_db()
    if user["role"] == "patient":
        row = conn.execute(
            "SELECT id FROM patients WHERE id = ? AND user_id = ?",
            (patient_id, user["id"])
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT p.id
            FROM patients p
            JOIN caregiver_patient cp ON cp.patient_id = p.id
            WHERE p.id = ? AND cp.caregiver_id = ?
            """,
            (patient_id, user["caregiver_id"])
        ).fetchone()
    conn.close()
    return row is not None


def caregiver_belongs_to_user(caregiver_id, user):
    """Check that a caregiver profile belongs to the logged-in user."""
    return bool(
        user
        and user["caregiver_id"] is not None
        and int(caregiver_id) == int(user["caregiver_id"])
    )

def caregiver_validation_mode(user):
    """True only while a caregiver is testing a game in the validation sandbox."""
    return bool(
        user
        and user["role"] == "caregiver"
        and session.get("caregiver_validation_mode") is True
    )

def get_patient_ai_alert(patient_id):
    """
    Returns a simple caregiver-facing alert based on recent
    cognitive-game performance.

    This is a monitoring signal, not a medical diagnosis.
    """
    conn = get_db()

    rows = conn.execute(
        """
        SELECT
            score,
            max_score,
            accuracy,
            time_taken_seconds,
            played_at
        FROM game_scores
        WHERE patient_id = ?
        ORDER BY played_at DESC
        LIMIT 6
        """,
        (patient_id,)
    ).fetchall()

    conn.close()

    if len(rows) < 4:  # need 3 recent + at least 1 earlier session
        return {
            "has_alert": False,
            "status": "INSUFFICIENT DATA",
            "message": "More game sessions are needed before an alert can be generated."
        }

    # Convert newest sessions into normalized performance values.
    performances = []

    for row in rows:
        accuracy = row["accuracy"]

        if accuracy is None:
            score = row["score"] or 0
            max_score = row["max_score"] or 0
            accuracy = (
                score / max_score
                if max_score > 0
                else 0
            )

        if accuracy > 1:
            accuracy = accuracy / 100

        performances.append(float(accuracy))

    recent = sum(performances[:3]) / 3
    previous = sum(performances[3:]) / len(performances[3:])

    change = recent - previous

    if change <= -0.20:
        return {
            "has_alert": True,
            "status": "REVIEW RECOMMENDED",
            "message": "A meaningful decline was detected in recent game performance. Review the patient's AI Cognitive Center."
        }

    if change <= -0.10:
        return {
            "has_alert": True,
            "status": "MONITOR",
            "message": "Recent performance is lower than the previous sessions. Continue monitoring the patient's cognitive-game activity."
        }

    return {
        "has_alert": False,
        "status": "STABLE",
        "message": "No significant recent performance change detected."
    }


# ============================================================================
# AUTHENTICATION ROUTES
# ============================================================================

@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user():
        user = current_user()
        if user["role"] == "patient" and user["patient_id"]:
            return redirect(url_for("patient_dashboard", patient_id=user["patient_id"]))
        return redirect(url_for("caregiver_dashboard", caregiver_id=user["caregiver_id"]))

    if request.method == "POST":
        role = str(request.form.get("role", "caregiver")).strip().lower()
        name = str(request.form.get("name", "")).strip()
        email = str(request.form.get("email", "")).strip().lower()
        password = str(request.form.get("password", ""))

        if role not in {"caregiver", "patient"}:
            role = "caregiver"

        if not name or not email or not password:
            flash("Please fill in all required fields.")
            return render_template("register.html", selected_role=role)

        if len(password) < 8:
            flash("Password must contain at least 8 characters.")
            return render_template("register.html", selected_role=role)

        conn = get_db()
        existing = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
        if existing:
            conn.close()
            flash("An account with this email already exists.")
            return render_template("register.html", selected_role=role)

        try:
            if role == "caregiver":
                phone = str(request.form.get("phone", "")).strip()
                conn.execute("INSERT INTO caregivers (name, phone) VALUES (?, ?)", (name, phone))
                caregiver_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                caregiver_code = generate_unique_code(conn, "CARE-NC-", "caregivers", "caregiver_code")
                conn.execute("UPDATE caregivers SET caregiver_code = ? WHERE id = ?", (caregiver_code, caregiver_id))

                conn.execute(
                    """INSERT INTO users (name, email, password_hash, role, caregiver_id, patient_id, created_at)
                       VALUES (?, ?, ?, 'caregiver', ?, NULL, ?)""",
                    (name, email, generate_password_hash(password), caregiver_id, datetime.now().isoformat())
                )
                user_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                conn.commit()
                session.clear()
                session["user_id"] = user_id
                session["role"] = "caregiver"
                return redirect(url_for("caregiver_dashboard", caregiver_id=caregiver_id))

            patient_age_raw = str(request.form.get("patient_age", "")).strip()
            language = str(request.form.get("language", "en")).strip() or "en"
            caregiver_code = str(request.form.get("caregiver_code", "")).strip().upper()
            invite_token = str(request.form.get("invite_token", "")).strip()

            try:
                patient_age = int(patient_age_raw) if patient_age_raw else None
            except ValueError:
                conn.close()
                flash("Age must be a valid number.")
                return render_template("register.html", selected_role=role)

            caregiver = None
            if invite_token:
                caregiver = conn.execute(
                    """SELECT c.* FROM caregivers c
                       JOIN patient_invitations pi ON pi.caregiver_id = c.id
                       WHERE pi.token = ? AND pi.used_at IS NULL AND pi.expires_at > ?""",
                    (invite_token, datetime.now().isoformat())
                ).fetchone()
                if not caregiver:
                    conn.close()
                    flash("This patient invitation is invalid or has expired.")
                    return render_template("register.html", selected_role=role)
            elif caregiver_code:
                caregiver = conn.execute(
                    "SELECT * FROM caregivers WHERE caregiver_code = ?", (caregiver_code,)
                ).fetchone()
                if not caregiver:
                    conn.close()
                    flash("Caregiver code not found. Please check the code and try again.")
                    return render_template("register.html", selected_role=role)

            conn.execute(
                """INSERT INTO patients (name, age, language, caregiver_id, difficulty_level, user_id, patient_code)
                   VALUES (?, ?, ?, ?, 1, NULL, NULL)""",
                (name, patient_age, language, caregiver["id"] if caregiver else None)
            )
            patient_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            patient_code = generate_unique_code(conn, "NER-P-", "patients", "patient_code")
            conn.execute("UPDATE patients SET patient_code = ? WHERE id = ?", (patient_code, patient_id))

            conn.execute(
                """INSERT INTO users (name, email, password_hash, role, caregiver_id, patient_id, created_at)
                   VALUES (?, ?, ?, 'patient', ?, ?, ?)""",
                (name, email, generate_password_hash(password), caregiver["id"] if caregiver else None, patient_id, datetime.now().isoformat())
            )
            user_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute("UPDATE patients SET user_id = ? WHERE id = ?", (user_id, patient_id))

            if caregiver:
                conn.execute(
                    "INSERT OR IGNORE INTO caregiver_patient (caregiver_id, patient_id, linked_at) VALUES (?, ?, ?)",
                    (caregiver["id"], patient_id, datetime.now().isoformat())
                )
                if invite_token:
                    conn.execute(
                        "UPDATE patient_invitations SET used_at = ? WHERE token = ?",
                        (datetime.now().isoformat(), invite_token)
                    )

            conn.commit()
        except sqlite3.IntegrityError:
            conn.rollback()
            conn.close()
            flash("Unable to create the account. Please check the details and try again.")
            return render_template("register.html", selected_role=role)

        conn.close()
        session.clear()
        session["user_id"] = user_id
        session["role"] = "patient"
        return redirect(url_for("patient_dashboard", patient_id=patient_id))

    return render_template(
        "register.html",
        selected_role=request.args.get("role", "caregiver"),
        invite_token=request.args.get("invite", "")
    )


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user():
        user = current_user()
        if user["role"] == "patient" and user["patient_id"]:
            return redirect(url_for("patient_dashboard", patient_id=user["patient_id"]))
        return redirect(url_for("caregiver_dashboard", caregiver_id=user["caregiver_id"]))

    if request.method == "POST":
        email = str(request.form.get("email", "")).strip().lower()
        password = str(request.form.get("password", ""))
        requested_role = str(request.form.get("role", "")).strip().lower()

        conn = get_db()
        user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        conn.close()

        if not user or not check_password_hash(user["password_hash"], password):
            flash("Invalid email or password.")
            return render_template("login.html", selected_role=requested_role or "caregiver")

        if requested_role and user["role"] != requested_role:
            flash(f"This email is registered as a {user['role']}, not a {requested_role}.")
            return render_template("login.html", selected_role=requested_role)

        session.clear()
        session["user_id"] = user["id"]
        session["role"] = user["role"]

        next_url = request.args.get("next") or request.form.get("next")
        if next_url and next_url.startswith("/") and not next_url.startswith("//"):
            return redirect(next_url)

        if user["role"] == "patient" and user["patient_id"]:
            return redirect(url_for("patient_dashboard", patient_id=user["patient_id"]))
        return redirect(url_for("caregiver_dashboard", caregiver_id=user["caregiver_id"]))

    return render_template("login.html", selected_role=request.args.get("role", "caregiver"), next=request.args.get("next", ""))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))


@app.before_request
def leave_validation_mode():
    """The validation sandbox flag must only last while the caregiver is on
    a sandbox game page. Any other page view clears it, so real patient
    scores are never redirected into the sandbox by mistake."""
    if request.endpoint in ("caregiver_validation_game", "submit_score", "static"):
        return
    if request.path.startswith("/api/"):
        return
    session.pop("caregiver_validation_mode", None)
    session.pop("validation_caregiver_id", None)


@app.context_processor
def inject_auth_user():
    return {"logged_in_user": current_user()}


# ============================================================================
# GAME REGISTRY HELPERS
# ============================================================================

def normalise_game_name(game_name):
    """
    Converts game names into a predictable internal identifier.
    """

    if not game_name:
        return "unknown_game"

    return (
        str(game_name)
        .strip()
        .lower()
        .replace("-", "_")
        .replace(" ", "_")
    )


def get_game_domains(conn, game_name, submitted_domains=None):
    """
    Determine the cognitive domains associated with a game.

    Priority:

    1. Domains explicitly supplied by the game.
    2. Previously registered domains.
    3. Built-in mapping for existing games.
    4. Empty list if unknown.

    This means future games can introduce themselves without
    modifying the AI engine.
    """

    normalized = normalise_game_name(game_name)

    # ------------------------------------------------------------------------
    # 1. Explicit domains sent by the game
    # ------------------------------------------------------------------------

    if submitted_domains:

        valid_domains = [
            domain
            for domain in submitted_domains
            if domain in COGNITIVE_DOMAINS
        ]

        if valid_domains:

            display_name = normalized.replace("_", " ").title()

            conn.execute(
                """
                INSERT INTO game_registry
                (game_name, display_name, domains_json, created_at)
                VALUES (?, ?, ?, ?)

                ON CONFLICT(game_name)
                DO UPDATE SET
                    display_name = excluded.display_name,
                    domains_json = excluded.domains_json
                """,
                (
                    normalized,
                    display_name,
                    json.dumps(valid_domains),
                    datetime.now().isoformat()
                )
            )

            return valid_domains

    # ------------------------------------------------------------------------
    # 2. Existing registry
    # ------------------------------------------------------------------------

    row = conn.execute(
        """
        SELECT domains_json
        FROM game_registry
        WHERE game_name = ?
        """,
        (normalized,)
    ).fetchone()

    if row:

        try:
            domains = json.loads(row["domains_json"])

            if isinstance(domains, list):
                return domains

        except Exception:
            pass

    # ------------------------------------------------------------------------
    # 3. Built-in mapping
    # ------------------------------------------------------------------------

    return DEFAULT_GAME_DOMAINS.get(normalized, [])


# ============================================================================
# ADAPTIVE DIFFICULTY ENGINE
# ============================================================================

def compute_next_difficulty(
    current_level,
    score,
    max_score,
    time_taken,
    expected_time
):
    """
    Explainable adaptive difficulty engine.

    This remains independent from the analytics system.

    Later, this function can be replaced with a trained ML model
    without rebuilding the rest of NER-Care.
    """

    accuracy = score / max_score if max_score else 0

    speed_ratio = (
        expected_time / time_taken
        if time_taken and time_taken > 0
        else 1
    )

    level = current_level

    if accuracy >= 0.8 and speed_ratio >= 0.9:

        level = min(
            current_level + 1,
            5
        )

    elif accuracy < 0.4 or speed_ratio < 0.5:

        level = max(
            current_level - 1,
            1
        )

    return level


# ============================================================================
# ANALYTICS HELPERS
# ============================================================================

def safe_float(value, default=0.0):

    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_int(value, default=0):

    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def average(values):

    clean_values = [
        safe_float(v)
        for v in values
        if v is not None
    ]

    if not clean_values:
        return 0

    return sum(clean_values) / len(clean_values)


def percentage_change(old_value, new_value):

    old_value = safe_float(old_value)
    new_value = safe_float(new_value)

    if old_value == 0:
        return 0

    return ((new_value - old_value) / old_value) * 100


def calculate_confidence(session_count):

    """
    Confidence here means DATA SUFFICIENCY for analytics.

    It is NOT diagnostic confidence.
    """

    if session_count < 5:
        return {
            "score": 25,
            "label": "INSUFFICIENT DATA"
        }

    if session_count < 10:
        return {
            "score": 50,
            "label": "EARLY SIGNAL"
        }

    if session_count < 20:
        return {
            "score": 70,
            "label": "MODERATE DATA"
        }

    if session_count < 40:
        return {
            "score": 85,
            "label": "STRONG DATA"
        }

    return {
        "score": 95,
        "label": "HIGH DATA COVERAGE"
    }


# ============================================================================
# UNIVERSAL COGNITIVE ANALYTICS ENGINE
# ============================================================================

def calculate_cognitive_analytics(scores):
    """
    Universal longitudinal cognitive analytics engine.

    Performance comparisons are normalized wherever possible so that games
    with different score scales are not compared using raw points.

    Priority for a comparable performance value:
      1. Stored accuracy (0.0-1.0)
      2. score / max_score when max_score is available

    Response-time analysis remains independent from performance percentage.
    This is a monitoring signal, not a medical diagnosis.
    """

    total_sessions = len(scores)

    if total_sessions == 0:
        return {
            "total_sessions": 0,
            "structured_sessions": 0,
            "accuracy_data_coverage": 0,
            "average_score": 0,
            "latest_score": 0,
            "average_accuracy": 0,
            "latest_accuracy": None,
            "average_response_time": 0,
            "latest_response_time": 0,
            "confidence": calculate_confidence(0),
            "signal": "INSUFFICIENT DATA",
            "signal_class": "insufficient",
            "score_change": 0,
            "performance_change": 0,
            "speed_change": 0,
            "baseline_score": 0,
            "recent_score": 0,
            "domains": {},
            "games": {},
            "recent_sessions": [],
            "insights": []
        }

    records = []

    for row in scores:
        record = dict(row)
        record["score"] = safe_float(record.get("score"))
        record["max_score"] = safe_float(record.get("max_score"), 0)

        raw_accuracy = record.get("accuracy")
        if raw_accuracy is None:
            record["accuracy"] = None
        else:
            try:
                value = float(raw_accuracy)
                if value > 1:
                    value = value / 100.0
                record["accuracy"] = max(0.0, min(value, 1.0))
            except (TypeError, ValueError):
                record["accuracy"] = None

        record["time_taken_seconds"] = safe_float(
            record.get("time_taken_seconds")
        )
        record["difficulty_level"] = safe_int(
            record.get("difficulty_level"), 1
        )
        record["errors"] = safe_int(record.get("errors"), 0)
        record["hints_used"] = safe_int(record.get("hints_used"), 0)

        try:
            record["domains"] = json.loads(
                record.get("domains_json") or "[]"
            )
            if not isinstance(record["domains"], list):
                record["domains"] = []
        except Exception:
            record["domains"] = []

        record["accuracy_available"] = record["accuracy"] is not None

        # Normalized performance for cross-game comparisons.
        if record["accuracy"] is not None:
            record["normalized_performance"] = record["accuracy"]
            record["performance_source"] = "accuracy"
        elif record["max_score"] > 0:
            record["normalized_performance"] = max(
                0.0,
                min(record["score"] / record["max_score"], 1.0)
            )
            record["performance_source"] = "score_ratio"
        else:
            record["normalized_performance"] = None
            record["performance_source"] = None

        record["performance_available"] = (
            record["normalized_performance"] is not None
        )

        records.append(record)

    accuracy_records = [
        r for r in records if r["accuracy_available"]
    ]
    performance_records = [
        r for r in records if r["performance_available"]
    ]

    structured_sessions = len(accuracy_records)
    coverage = (
        (structured_sessions / total_sessions) * 100
        if total_sessions else 0
    )

    average_score = average([r["score"] for r in records])
    average_performance = (
        average([r["normalized_performance"] for r in performance_records]) * 100
        if performance_records else 0
    )
    average_accuracy = (
        average([r["accuracy"] for r in accuracy_records]) * 100
        if accuracy_records else 0
    )

    latest = records[-1]
    latest_score = latest["score"]
    latest_accuracy = (
        latest["accuracy"] * 100
        if latest["accuracy_available"] else None
    )

    average_response_time = average(
        [r["time_taken_seconds"] for r in records]
    )
    latest_response_time = latest["time_taken_seconds"]

    # Use comparable normalized performance for longitudinal change.
    if len(performance_records) >= 10:
        half = max(5, len(performance_records) // 2)
        baseline_records = performance_records[:half]
        recent_records = performance_records[-half:]
    else:
        half = max(1, len(performance_records) // 2)
        baseline_records = performance_records[:half]
        recent_records = performance_records[-half:]

    baseline_performance = average(
        [r["normalized_performance"] for r in baseline_records]
    )
    recent_performance = average(
        [r["normalized_performance"] for r in recent_records]
    )

    baseline_speed = average(
        [r["time_taken_seconds"] for r in baseline_records]
    ) if baseline_records else 0
    recent_speed = average(
        [r["time_taken_seconds"] for r in recent_records]
    ) if recent_records else 0

    performance_change = percentage_change(
        baseline_performance,
        recent_performance
    )
    speed_change = percentage_change(baseline_speed, recent_speed)

    domain_records = {
        domain: [] for domain in COGNITIVE_DOMAINS
    }

    for record in records:
        if not record["performance_available"]:
            continue
        for domain in record.get("domains", []):
            if domain in domain_records:
                domain_records[domain].append(record)

    domain_analysis = {}

    for domain, domain_sessions in domain_records.items():
        if not domain_sessions:
            domain_analysis[domain] = {
                "available": False,
                "sessions": 0,
                "accuracy_sessions": 0,
                "score": 0,
                "accuracy": 0,
                "response_time": 0,
                "trend": 0,
                "baseline_performance": 0,
                "recent_performance": 0
            }
            continue

        valid_accuracy = [
            r for r in domain_sessions if r["accuracy_available"]
        ]

        domain_score = average(
            [r["score"] for r in domain_sessions]
        )

        domain_performance = average(
            [r["normalized_performance"] for r in domain_sessions]
        ) * 100

        domain_accuracy = (
            average([r["accuracy"] for r in valid_accuracy]) * 100
            if valid_accuracy else domain_performance
        )

        domain_speed = average(
            [r["time_taken_seconds"] for r in domain_sessions]
        )

        split = max(1, len(domain_sessions) // 2)
        old_performance = average([
            r["normalized_performance"]
            for r in domain_sessions[:split]
        ])
        new_performance = average([
            r["normalized_performance"]
            for r in domain_sessions[-split:]
        ])

        domain_analysis[domain] = {
            "available": True,
            "sessions": len(domain_sessions),
            "accuracy_sessions": len(valid_accuracy),
            "score": round(domain_score, 2),
            "accuracy": round(domain_accuracy, 1),
            "response_time": round(domain_speed, 2),
            "trend": round(
                percentage_change(old_performance, new_performance), 1
            ),
            "baseline_performance": round(old_performance * 100, 1),
            "recent_performance": round(new_performance * 100, 1)
        }

    game_groups = {}
    for record in records:
        game_groups.setdefault(record["game_name"], []).append(record)

    game_analysis = {}

    for game, game_sessions in game_groups.items():
        valid_accuracy = [
            r for r in game_sessions if r["accuracy_available"]
        ]
        valid_performance = [
            r for r in game_sessions if r["performance_available"]
        ]

        avg_game_accuracy = (
            average([r["accuracy"] for r in valid_accuracy]) * 100
            if valid_accuracy else (
                average([r["normalized_performance"] for r in valid_performance]) * 100
                if valid_performance else 0
            )
        )

        latest_game = game_sessions[-1]
        latest_game_accuracy = (
            latest_game["accuracy"] * 100
            if latest_game["accuracy_available"] else (
                latest_game["normalized_performance"] * 100
                if latest_game["performance_available"] else None
            )
        )

        game_analysis[game] = {
            "sessions": len(game_sessions),
            "accuracy_sessions": len(valid_accuracy),
            "average_score": round(
                average([r["score"] for r in game_sessions]), 2
            ),
            "average_accuracy": round(avg_game_accuracy, 1),
            "average_response_time": round(
                average([r["time_taken_seconds"] for r in game_sessions]), 2
            ),
            "latest_score": latest_game["score"],
            "latest_accuracy": (
                round(latest_game_accuracy, 1)
                if latest_game_accuracy is not None else None
            )
        }

    insights = []

    if len(performance_records) < 5:
        insights.append({
            "type": "data",
            "severity": "info",
            "title": "More structured data needed",
            "message": (
                "Complete more tracked cognitive sessions before interpreting "
                "longitudinal performance changes."
            )
        })
    else:
        if performance_change >= 10:
            insights.append({
                "type": "improvement",
                "severity": "positive",
                "title": "Performance improving",
                "message": (
                    f"Normalized recent performance is {abs(performance_change):.1f}% "
                    "higher than the baseline period."
                ),
                "evidence": f"Compared with the earlier baseline period across {len(performance_records)} tracked sessions.",
                "action": "Continue regular cognitive practice and monitor whether the improvement remains consistent."
            })
        elif performance_change <= -15:
            insights.append({
                "type": "change",
                "severity": "attention",
                "title": "Performance change detected",
                "message": (
                    f"Normalized recent performance is {abs(performance_change):.1f}% "
                    "lower than the baseline period."
                ),
                "evidence": f"Recent sessions are {abs(performance_change):.1f}% below the earlier baseline period.",
                "action": "Continue structured sessions and consider caregiver review if the change persists."
            })
        else:
            insights.append({
                "type": "stable",
                "severity": "normal",
                "title": "Performance relatively stable",
                "message": (
                    "Normalized recent performance remains within the observed "
                    "baseline range."
                ),
                "evidence": "Recent performance is within the range established by earlier sessions.",
                "action": "Continue routine cognitive activities and longitudinal monitoring."
            })

        if speed_change >= 20:
            insights.append({
                "type": "speed",
                "severity": "attention",
                "title": "Response time increased",
                "message": (
                    f"Average response time increased by {speed_change:.1f}% "
                    "compared with baseline."
                ),
                "evidence": f"Average response time is {speed_change:.1f}% higher than the baseline period.",
                "action": "Monitor response speed across upcoming sessions before drawing conclusions."
            })
        elif speed_change <= -15:
            insights.append({
                "type": "speed",
                "severity": "positive",
                "title": "Response speed improved",
                "message": (
                    f"Average response time decreased by {abs(speed_change):.1f}% "
                    "compared with baseline."
                ),
                "evidence": f"Average response time is {abs(speed_change):.1f}% lower than the baseline period.",
                "action": "Continue the same cognitive practice pattern and monitor consistency."
            })

    for domain, data in domain_analysis.items():
        if not data["available"] or data["sessions"] < 3:
            continue

        if data["trend"] <= -20:
            insights.append({
                "type": "domain",
                "severity": "attention",
                "title": f"{domain.replace('_', ' ').title()} signal",
                "message": (
                    f"Recent normalized performance in this domain is "
                    f"{abs(data['trend']):.1f}% below its earlier sessions."
                ),
                "evidence": f"This domain contains {data['sessions']} tracked sessions with a {abs(data['trend']):.1f}% decline from its earlier period.",
                "action": f"Include more {domain.replace('_', ' ')}-focused activities and monitor future sessions."
            })
        elif data["trend"] >= 15:
            insights.append({
                "type": "domain",
                "severity": "positive",
                "title": f"{domain.replace('_', ' ').title()} improvement",
                "message": (
                    f"Recent normalized performance in this domain has improved "
                    f"by {data['trend']:.1f}%."
                ),
                "evidence": f"This domain contains {data['sessions']} tracked sessions showing a {data['trend']:.1f}% improvement.",
                "action": "Continue activities that engage this domain and monitor whether the gain remains consistent."
            })

    # Multi-factor cognitive change signal. This is a monitoring aid, not a diagnosis.
    usable_domains = [
        data for data in domain_analysis.values()
        if data.get("available") and data.get("sessions", 0) >= 3
    ]
    declining_domains = [
        data for data in usable_domains if data.get("trend", 0) <= -15
    ]
    improving_domains = [
        data for data in usable_domains if data.get("trend", 0) >= 15
    ]

    if len(performance_records) < 5 or structured_sessions < 3:
        signal = "INSUFFICIENT DATA"
        signal_class = "insufficient"
        signal_reason = "More structured sessions are needed for a reliable longitudinal signal."
    elif (
        performance_change <= -15
        or len(declining_domains) >= 2
        or (performance_change <= -8 and speed_change >= 15)
    ):
        signal = "REVIEW RECOMMENDED"
        signal_class = "review"
        if len(declining_domains) >= 2:
            signal_reason = "Sustained decline detected across multiple cognitive domains."
        elif performance_change <= -15:
            signal_reason = "Recent overall performance is meaningfully below the baseline period."
        else:
            signal_reason = "Performance has declined while response time has also slowed."
    elif (
        performance_change <= -8
        or len(declining_domains) == 1
        or speed_change >= 20
    ):
        signal = "MONITOR"
        signal_class = "monitor"
        if len(declining_domains) == 1:
            signal_reason = "A notable change was detected in one cognitive domain."
        elif speed_change >= 20:
            signal_reason = "Response time has slowed compared with the baseline period."
        else:
            signal_reason = "Recent performance is below the baseline period and should be followed over time."
    elif performance_change >= 10 and len(declining_domains) == 0:
        signal = "IMPROVING"
        signal_class = "improving"
        if improving_domains:
            signal_reason = "Overall performance is improving with positive movement in cognitive domains."
        else:
            signal_reason = "Recent overall performance is higher than the baseline period."
    else:
        signal = "STABLE"
        signal_class = "stable"
        signal_reason = "No major sustained change was detected in the available performance data."

    recent_sessions = []
    for record in records[-10:]:
        recent_sessions.append({
            "game_name": record["game_name"],
            "score": record["score"],
            "accuracy": (
                round(record["accuracy"] * 100, 1)
                if record["accuracy_available"] else None
            ),
            "response_time": record["time_taken_seconds"],
            "difficulty": record["difficulty_level"],
            "played_at": record["played_at"],
            "domains": record.get("domains", [])
        })

    return {
        "total_sessions": total_sessions,
        "structured_sessions": structured_sessions,
        "accuracy_data_coverage": round(coverage, 1),
        "average_score": round(average_score, 2),
        "average_performance": round(average_performance, 1),
        "latest_score": round(latest_score, 2),
        "latest_accuracy": (
            round(latest_accuracy, 1)
            if latest_accuracy is not None else None
        ),
        "average_accuracy": round(average_accuracy, 1),
        "average_response_time": round(average_response_time, 2),
        "latest_response_time": round(latest_response_time, 2),
        "confidence": calculate_confidence(structured_sessions),
        "signal": signal,
        "signal_class": signal_class,
        "signal_reason": signal_reason,
        "baseline_score": round(baseline_performance * 100, 1),
        "recent_score": round(recent_performance * 100, 1),
        "score_change": round(performance_change, 1),
        "performance_change": round(performance_change, 1),
        "speed_change": round(speed_change, 1),
        "domains": domain_analysis,
        "games": game_analysis,
        "recent_sessions": recent_sessions,
        "insights": insights
    }


# ============================================================================
# AI CONTEXT FUSION
# ============================================================================

def build_ai_context_summary(analytics, observations):
    obs = [dict(row) for row in observations] if observations else []
    concerning = [o for o in obs if o.get("status") == "concerning"]
    improved = [o for o in obs if o.get("status") == "improved"]
    resolved = [o for o in obs if o.get("status") == "resolved"]
    signal = analytics.get("signal", "INSUFFICIENT DATA")
    score_change = safe_float(analytics.get("score_change"), 0)
    speed_change = safe_float(analytics.get("speed_change"), 0)
    if signal == "INSUFFICIENT DATA": headline = "Build more evidence before interpreting change"
    elif signal == "REVIEW RECOMMENDED": headline = "Recent performance changes deserve caregiver review"
    elif signal == "MONITOR": headline = "Some changes should be followed over time"
    elif signal == "IMPROVING": headline = "Performance is showing positive movement"
    else: headline = "Performance appears relatively stable"
    evidence=[]
    if analytics.get("total_sessions",0): evidence.append(f"{analytics['total_sessions']} game sessions")
    if analytics.get("structured_sessions",0): evidence.append(f"{analytics['structured_sessions']} structured sessions")
    if score_change>0: evidence.append(f"performance increased {score_change:.1f}%")
    elif score_change<0: evidence.append(f"performance decreased {abs(score_change):.1f}%")
    if speed_change>0: evidence.append(f"response time increased {speed_change:.1f}%")
    elif speed_change<0: evidence.append(f"response time improved {abs(speed_change):.1f}%")
    context=[]
    if concerning:
        cats=[]
        for o in concerning:
            if o.get("category") not in cats: cats.append(o.get("category"))
        context.append("Needs-attention observations: " + ", ".join(cats[:4]))
    if improved: context.append(f"{len(improved)} caregiver observation(s) marked improved")
    if resolved: context.append(f"{len(resolved)} observation(s) marked resolved")
    if concerning and signal in {"MONITOR","REVIEW RECOMMENDED"}:
        interpretation="Game-based changes and caregiver observations point to areas worth reviewing together."
        action="Review recent sessions and caregiver notes, continue observation, and seek professional assessment if changes persist or affect daily functioning."
    elif concerning:
        interpretation="Caregiver observations add real-world context that may be useful when reviewing game data."
        action="Continue documenting the observation and compare it with future sessions rather than drawing a conclusion from one observation."
    elif improved and signal=="IMPROVING":
        interpretation="Both performance data and caregiver context contain positive signals."
        action="Continue regular, familiar cognitive activities and record whether the improvement remains consistent."
    elif not obs:
        interpretation="No caregiver context has been recorded yet, so this summary currently relies on game-performance data."
        action="Add observations when relevant to provide additional real-world context."
    else:
        interpretation="Caregiver context is available, but it should be interpreted alongside longitudinal performance and daily functioning."
        action="Continue regular sessions and update observations when meaningful changes occur."
    return {"headline":headline,"performance_signal":signal,"evidence":evidence,"context":context,"interpretation":interpretation,"action":action,"observation_count":len(obs),"concerning_count":len(concerning),"improved_count":len(improved)}


# ============================================================================
# PERSONALIZED AI PRACTICE PLAN
# ============================================================================

def build_personalized_practice_plan(analytics, registered_games):
    """
    Creates an explainable, non-diagnostic practice plan from recent
    cognitive-domain performance. The plan recommends existing registered
    games rather than inventing activities.
    """
    domains = analytics.get("domains", {}) or {}
    available = []

    for domain_key, data in domains.items():
        if data.get("available"):
            available.append({
                "key": domain_key,
                "label": domain_key.replace("_", " ").title(),
                "accuracy": safe_float(data.get("accuracy"), 0),
                "trend": safe_float(data.get("trend"), 0),
                "sessions": safe_int(data.get("sessions"), 0),
            })

    # Prefer a sustained decline, otherwise the lowest current accuracy.
    declining = [d for d in available if d["sessions"] >= 3 and d["trend"] <= -10]
    if declining:
        target = sorted(declining, key=lambda d: d["trend"])[0]
        reason = "This domain shows a notable downward movement in recent sessions."
    elif available:
        target = sorted(available, key=lambda d: d["accuracy"])[0]
        reason = "This domain currently has the lowest available performance level."
    else:
        target = {"key": "memory", "label": "Memory", "accuracy": 0, "trend": 0, "sessions": 0}
        reason = "More structured sessions are needed before selecting a targeted domain."

    matching_games = []
    for game in registered_games or []:
        if target["key"] in (game.get("domains") or []):
            matching_games.append(game.get("display_name") or game.get("game_name", "Cognitive Game"))

    if not matching_games:
        matching_games = ["Cognitive games mapped to this domain"]

    if target["sessions"] < 3:
        intensity = "Foundation"
        duration = "5–10 minutes"
        instruction = "Use short, familiar sessions and collect more structured data."
    elif target["trend"] <= -20:
        intensity = "Targeted"
        duration = "5–10 minutes"
        instruction = "Use short targeted practice and compare the next few sessions."
    else:
        intensity = "Balanced"
        duration = "10–15 minutes"
        instruction = "Maintain regular practice while watching the domain trend."

    return {
        "domain": target["label"],
        "domain_key": target["key"],
        "accuracy": target["accuracy"],
        "trend": target["trend"],
        "sessions": target["sessions"],
        "reason": reason,
        "games": matching_games[:3],
        "intensity": intensity,
        "duration": duration,
        "instruction": instruction,
    }


# ============================================================================
# HOME
# ============================================================================

@app.route("/")
def home():

    return render_template(
        "home.html"
    )


# ============================================================================
# CAREGIVER HOME / WORKSPACE
# ============================================================================
#
# This is the caregiver's landing page.
# It keeps frequently used caregiver functions visible:
# Home, Dashboard, My Patients, and Game Validation Center.
# Settings / Help / Logout remain in the hamburger menu.
# ============================================================================

@app.route("/caregiver/<int:caregiver_id>/home")
@login_required
def caregiver_home(caregiver_id):
    user = current_user()

    if user["role"] != "caregiver" or not caregiver_belongs_to_user(caregiver_id, user):
        return "Access denied", 403

    conn = get_db()
    caregiver = conn.execute(
        "SELECT * FROM caregivers WHERE id = ?",
        (caregiver_id,)
    ).fetchone()
    conn.close()

    if not caregiver:
        return "Caregiver not found", 404

    return render_template(
        "caregiver_home.html",
        caregiver=caregiver
    )

@app.route("/caregiver/<int:caregiver_id>/game-validation")
@login_required
def caregiver_game_validation(caregiver_id):
    user = current_user()

    if user["role"] != "caregiver" or not caregiver_belongs_to_user(
        caregiver_id, user
    ):
        return "Access denied", 403

    conn = get_db()

    caregiver = conn.execute(
        "SELECT * FROM caregivers WHERE id = ?",
        (caregiver_id,)
    ).fetchone()

    validation_sessions = conn.execute(
        """
        SELECT COUNT(*) AS total
        FROM validation_sessions
        WHERE caregiver_id = ?
        """,
        (caregiver_id,)
    ).fetchone()["total"]

    conn.close()

    if not caregiver:
        return "Caregiver not found", 404

    return render_template(
        "caregiver_game_validation_center.html",
        caregiver=caregiver,
        validation_sessions=validation_sessions
    )

@app.route("/caregiver/<int:caregiver_id>/game-validation/analysis")
@login_required
def caregiver_validation_analysis(caregiver_id):
    user = current_user()

    if user["role"] != "caregiver" or not caregiver_belongs_to_user(caregiver_id, user):
        return "Access denied", 403

    conn = get_db()

    caregiver = conn.execute(
        "SELECT * FROM caregivers WHERE id = ?",
        (caregiver_id,)
    ).fetchone()

    if not caregiver:
        conn.close()
        return "Caregiver not found", 404

    summary = conn.execute("""
        SELECT
            COUNT(*) AS total_sessions,
            COUNT(DISTINCT game_name) AS games_tested,
            AVG(score) AS avg_score,
            AVG(accuracy) AS avg_accuracy,
            AVG(time_taken_seconds) AS avg_time,
            SUM(errors) AS total_errors,
            SUM(hints_used) AS total_hints
        FROM validation_sessions
        WHERE caregiver_id = ?
    """, (caregiver_id,)).fetchone()

    game_rows = conn.execute("""
        SELECT
            game_name,
            COUNT(*) AS sessions,
            AVG(score) AS avg_score,
            AVG(accuracy) AS avg_accuracy,
            AVG(time_taken_seconds) AS avg_time,
            SUM(errors) AS errors,
            MAX(tested_at) AS last_tested
        FROM validation_sessions
        WHERE caregiver_id = ?
        GROUP BY game_name
        ORDER BY last_tested DESC
    """, (caregiver_id,)).fetchall()

    recent_rows = conn.execute("""
        SELECT
            game_name,
            score,
            max_score,
            accuracy,
            time_taken_seconds,
            errors,
            hints_used,
            completion_status,
            tested_at
        FROM validation_sessions
        WHERE caregiver_id = ?
        ORDER BY tested_at DESC
        LIMIT 10
    """, (caregiver_id,)).fetchall()

    conn.close()

    return render_template(
        "caregiver_validation_analysis.html",
        caregiver=caregiver,
        summary=summary,
        game_rows=game_rows,
        recent_rows=recent_rows
    )

@app.route("/caregiver/<int:caregiver_id>/game-validation/ai")
@login_required
def caregiver_ai_validation(caregiver_id):
    user = current_user()

    if user["role"] != "caregiver" or not caregiver_belongs_to_user(
        caregiver_id, user
    ):
        return "Access denied", 403

    conn = get_db()

    caregiver = conn.execute(
        "SELECT * FROM caregivers WHERE id = ?",
        (caregiver_id,)
    ).fetchone()

    if not caregiver:
        conn.close()
        return "Caregiver not found", 404

    # ------------------------------------------------------------
    # Validation-only AI data
    # ------------------------------------------------------------
    rows = conn.execute(
        """
        SELECT *
        FROM validation_sessions
        WHERE caregiver_id = ?
        ORDER BY tested_at ASC
        """,
        (caregiver_id,)
    ).fetchall()

    conn.close()

    return render_template(
        "caregiver_ai_validation.html",
        caregiver=caregiver,
        validation_rows=rows
    )

@app.route("/api/caregiver/<int:caregiver_id>/game-validation/ai-analysis")
@login_required
def caregiver_ai_validation_analysis(caregiver_id):
    user = current_user()

    if user["role"] != "caregiver" or not caregiver_belongs_to_user(
        caregiver_id, user
    ):
        return jsonify({
            "status": "error",
            "message": "Access denied"
        }), 403

    conn = get_db()

    rows = conn.execute(
        """
        SELECT
            game_name,
            score,
            max_score,
            accuracy,
            time_taken_seconds,
            expected_time_seconds,
            errors,
            hints_used,
            completion_status,
            domains_json,
            metrics_json,
            tested_at
        FROM validation_sessions
        WHERE caregiver_id = ?
        ORDER BY tested_at ASC
        """,
        (caregiver_id,)
    ).fetchall()

    conn.close()

    # ------------------------------------------------------------
    # No validation data
    # ------------------------------------------------------------

    if not rows:
        return jsonify({
            "status": "ok",
            "data_available": False,
            "message": "No validation data available yet."
        })

    # ------------------------------------------------------------
    # Adapt validation_sessions to the SAME AI engine format
    # ------------------------------------------------------------

    validation_records = []

    for row in rows:
        record = dict(row)

        # The universal engine expects played_at.
        # Validation sessions use tested_at instead.
        record["played_at"] = record.get("tested_at")

        # Validation sandbox does not use patient difficulty.
        # Give the engine a neutral default.
        record["difficulty_level"] = 1

        validation_records.append(record)

    # ------------------------------------------------------------
    # SAME NER-Care cognitive analytics engine
    # ------------------------------------------------------------

    analytics = calculate_cognitive_analytics(validation_records)

    # ------------------------------------------------------------
    # Return AI validation result
    # ------------------------------------------------------------

    return jsonify({
        "status": "ok",
        "data_available": True,
        "test_mode": True,

        "summary": {
            "sessions": analytics["total_sessions"],
            "structured_sessions": analytics["structured_sessions"],
            "accuracy_data_coverage": analytics["accuracy_data_coverage"],
            "average_score": analytics["average_score"],
            "average_performance": analytics["average_performance"],
            "average_accuracy": analytics["average_accuracy"],
            "latest_score": analytics["latest_score"],
            "latest_accuracy": analytics["latest_accuracy"],
            "average_response_time": analytics["average_response_time"],
            "latest_response_time": analytics["latest_response_time"]
        },

        "ai_validation": {
            "performance_signal": analytics["signal"],
            "signal_class": analytics["signal_class"],
            "signal_reason": analytics["signal_reason"],
            "confidence": analytics["confidence"]
        },

        "cognitive_domains": analytics["domains"],

        "games": analytics["games"],

        "explainable_insights": analytics["insights"],

        "recent_sessions": analytics["recent_sessions"],

        "validation_boundary": (
            "Software and data-pipeline validation only. "
            "This output is not a medical diagnosis or clinical assessment."
        )
    })



@app.route("/caregiver/<int:caregiver_id>/game-validation/play/<game_name>")
@login_required
def caregiver_validation_game(caregiver_id, game_name):
    user = current_user()

    if user["role"] != "caregiver" or not caregiver_belongs_to_user(
        caregiver_id, user
    ):
        return "Access denied", 403

    allowed_games = {
        "memory-match": "memory_match.html",
        "pattern-recognition": "pattern_recognition.html",
        "daily-routine": "daily_routine.html",
        "object-recognition": "object_recognition.html",
    }

    template_name = allowed_games.get(game_name)

    if not template_name:
        return "Game not available for validation", 404

    validation_patient = {
        "id": 0,
        "name": "Validation Sandbox",
        "difficulty_level": 1,
        "age": None,
        "language": "en",
    }

    session["caregiver_validation_mode"] = True
    session["validation_caregiver_id"] = caregiver_id

    return render_template(
        template_name,
        patient=validation_patient,
        validation_mode=True,
        validation_caregiver_id=caregiver_id,
    )

@app.route("/patient/<int:patient_id>/edit-profile", methods=["GET", "POST"])
@login_required
def patient_edit_profile(patient_id):

    user = current_user()

    # Only the patient themselves can edit their profile.
    if user["role"] != "patient" or not patient_belongs_to_user(patient_id, user):
        return "Access denied", 403

    conn = get_db()

    patient = conn.execute(
        "SELECT * FROM patients WHERE id = ?",
        (patient_id,)
    ).fetchone()

    if not patient:
        conn.close()
        return "Patient not found", 404

    if request.method == "POST":
        name = str(request.form.get("name", "")).strip()
        age_raw = str(request.form.get("age", "")).strip()
        language = str(request.form.get("language", "en")).strip() or "en"

        if not name:
            conn.close()
            flash("Name cannot be empty.")
            return redirect(url_for(
                "patient_edit_profile",
                patient_id=patient_id
            ))

        try:
            age = int(age_raw) if age_raw else None
        except ValueError:
            conn.close()
            flash("Age must be a valid number.")
            return redirect(url_for(
                "patient_edit_profile",
                patient_id=patient_id
            ))

        if age is not None and (age < 1 or age > 120):
            conn.close()
            flash("Please enter an age between 1 and 120.")
            return redirect(url_for(
                "patient_edit_profile",
                patient_id=patient_id
            ))

        conn.execute(
            """
            UPDATE patients
            SET name = ?, age = ?, language = ?
            WHERE id = ?
            """,
            (name, age, language, patient_id)
        )

        # Keep the login account name synchronized with the patient profile.
        conn.execute(
            """
            UPDATE users
            SET name = ?
            WHERE patient_id = ? AND role = 'patient'
            """,
            (name, patient_id)
        )

        conn.commit()

        patient = conn.execute(
            "SELECT * FROM patients WHERE id = ?",
            (patient_id,)
        ).fetchone()

        conn.close()

        flash("Profile updated successfully.")
        return redirect(url_for(
            "patient_dashboard",
            patient_id=patient_id
        ))

    conn.close()

    return render_template(
        "patient_edit_profile.html",
        patient=patient
    )

@app.route("/access-notice/<requested_role>")
@login_required
def access_notice(requested_role):

    user = current_user()

    if not user:
        return redirect(url_for("login"))

    if requested_role not in ["patient", "caregiver"]:
        return "Invalid access request", 404

    return render_template(
        "access_notice.html",
        user=user,
        requested_role=requested_role
    )

# ============================================================================
# PATIENT DASHBOARD
# ============================================================================

@app.route("/patient/<int:patient_id>")
@login_required
def patient_dashboard(patient_id):

    user = current_user()
    if not patient_belongs_to_user(patient_id, user):
        return "Access denied", 403

    conn = get_db()

    patient = conn.execute(
        """
        SELECT p.*, u.email AS account_email
        FROM patients p
        LEFT JOIN users u
            ON u.patient_id = p.id
           AND u.role = 'patient'
        WHERE p.id = ?
        """,
        (patient_id,)
    ).fetchone()

    reminders = conn.execute(
        """
        SELECT *
        FROM reminders
        WHERE patient_id = ?
        AND is_done = 0
        ORDER BY reminder_time
        """,
        (patient_id,)
    ).fetchall()

    conn.close()

    return render_template(
        "patient_dashboard.html",
        patient=patient,
        reminders=reminders
    )


# ============================================================================
# MEMORY MATCH
# ============================================================================

@app.route("/game/memory-match/<int:patient_id>")
@login_required
def memory_match(patient_id):

    user = current_user()
    if not patient_belongs_to_user(patient_id, user):
        return "Access denied", 403

    conn = get_db()

    patient = conn.execute(
        """
        SELECT p.*, u.email AS account_email
        FROM patients p
        LEFT JOIN users u
            ON u.patient_id = p.id
           AND u.role = 'patient'
        WHERE p.id = ?
        """,
        (patient_id,)
    ).fetchone()

    conn.close()

    return render_template(
        "memory_match.html",
        patient=patient
    )


# ============================================================================
# PATTERN RECOGNITION
# ============================================================================

@app.route("/game/pattern-recognition/<int:patient_id>")
@login_required
def pattern_recognition(patient_id):

    user = current_user()
    if not patient_belongs_to_user(patient_id, user):
        return "Access denied", 403

    conn = get_db()

    patient = conn.execute(
        """
        SELECT p.*, u.email AS account_email
        FROM patients p
        LEFT JOIN users u
            ON u.patient_id = p.id
           AND u.role = 'patient'
        WHERE p.id = ?
        """,
        (patient_id,)
    ).fetchone()

    conn.close()

    return render_template(
        "pattern_recognition.html",
        patient=patient
    )


# ============================================================================
# DAILY ROUTINE RECALL
# ============================================================================

@app.route("/game/daily-routine/<int:patient_id>")
@login_required
def daily_routine(patient_id):

    user = current_user()
    if not patient_belongs_to_user(patient_id, user):
        return "Access denied", 403

    conn = get_db()

    patient = conn.execute(
        '''
        SELECT *
        FROM patients
        WHERE id = ?
        ''',
        (patient_id,)
    ).fetchone()

    conn.close()

    if not patient:
        return "Patient not found", 404

    return render_template(
        "daily_routine.html",
        patient=patient
    )


# ============================================================================
# OBJECT RECOGNITION
# ============================================================================

@app.route("/game/object-recognition/<int:patient_id>")
@login_required
def object_recognition(patient_id):

    user = current_user()
    if not patient_belongs_to_user(patient_id, user):
        return "Access denied", 403

    conn = get_db()

    patient = conn.execute(
        '''
        SELECT *
        FROM patients
        WHERE id = ?
        ''',
        (patient_id,)
    ).fetchone()

    conn.close()

    if not patient:
        return "Patient not found", 404

    return render_template(
        "object_recognition.html",
        patient=patient
    )


# ============================================================================
# UNIVERSAL GAME SCORE API
# ============================================================================
#
# This is the most important part of today's upgrade.
#
# Existing game can continue sending:
#
# {
#   patient_id,
#   game_name,
#   score,
#   max_score,
#   time_taken_seconds
# }
#
# Future game can send much richer information:
#
# {
#   patient_id: 1,
#   game_name: "sequence_recall",
#   score: 82,
#   max_score: 100,
#   time_taken_seconds: 4.2,
#   expected_time_seconds: 5,
#   difficulty_level: 3,
#   errors: 2,
#   hints_used: 1,
#   completion_status: "completed",
#   domains: ["memory", "attention"],
#   metrics: {
#       "sequence_length": 7,
#       "repetitions": 2
#   }
# }
#
# The AI engine doesn't need to be rebuilt.
# ============================================================================

@app.route(
    "/api/submit-score",
    methods=["POST"]
)
@login_required
def submit_score():

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}

    if caregiver_validation_mode(current_user()):
        game_name = data.get("game_name", "unknown_game")
        game_name = normalise_game_name(game_name)

        score = safe_float(data.get("score"), 0)
        max_score = safe_float(data.get("max_score"), 100) or 100

        accuracy = (
            (score / max_score) * 100
            if max_score > 0
            else 0
        )

        time_taken = data.get("time_taken_seconds")
        expected_time = data.get("expected_time_seconds")

        errors = safe_int(data.get("errors"), 0)
        hints_used = safe_int(data.get("hints_used"), 0)

        completion_status = data.get(
            "completion_status",
            "completed"
        )

        domains = data.get("domains", [])
        metrics = data.get("metrics", {})
        if not isinstance(domains, list):
            domains = []
        if not isinstance(metrics, dict):
            metrics = {}

        conn = get_db()

        conn.execute(
            """
            INSERT INTO validation_sessions (
                caregiver_id,
                game_name,
                score,
                max_score,
                accuracy,
                time_taken_seconds,
                expected_time_seconds,
                errors,
                hints_used,
                completion_status,
                domains_json,
                metrics_json,
                tested_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session.get("validation_caregiver_id"),
                game_name,
                score,
                max_score,
                accuracy,
                time_taken,
                expected_time,
                errors,
                hints_used,
                completion_status,
                json.dumps(domains),
                json.dumps(metrics),
                datetime.now().isoformat()
            )
        )

        conn.commit()
        conn.close()

        return jsonify({
            "status": "ok",
            "test_mode": True,
            "recorded_game": game_name,
            "next_difficulty_level": 1,
            "domains": domains,
            "analytics_signal": "VALIDATION SANDBOX",
            "analytics_confidence": "Validation data stored separately"
        })

    # ------------------------------------------------------------------------
    # REQUIRED DATA
    # ------------------------------------------------------------------------

    patient_id = safe_int(data.get("patient_id"), 0)

    game_name = str(data.get(
        "game_name",
        "unknown_game"
    ))[:60]

    score = safe_float(
        data.get("score"),
        0
    )

    max_score = safe_float(
        data.get("max_score"),
        0
    )

    time_taken = safe_float(
        data.get("time_taken_seconds"),
        0
    )

    # ------------------------------------------------------------------------
    # OPTIONAL DATA
    # ------------------------------------------------------------------------

    expected_time = safe_float(
        data.get(
            "expected_time_seconds",
            time_taken
        ),
        time_taken
    )

    errors = safe_int(
        data.get(
            "errors",
            0
        )
    )

    hints_used = safe_int(
        data.get(
            "hints_used",
            0
        )
    )

    completion_status = data.get(
        "completion_status",
        "completed"
    )

    submitted_domains = data.get(
        "domains",
        []
    )

    metrics = data.get(
        "metrics",
        {}
    )

    # ------------------------------------------------------------------------
    # VALIDATE PATIENT
    # ------------------------------------------------------------------------

    if not patient_id:

        return jsonify({
            "status": "error",
            "message": "patient_id is required"
        }), 400

    if not patient_belongs_to_user(patient_id, current_user()):
        return jsonify({
            "status": "error",
            "message": "Access denied"
        }), 403

    # ------------------------------------------------------------------------
    # NORMALIZE DOMAINS
    # ------------------------------------------------------------------------

    if not isinstance(
        submitted_domains,
        list
    ):

        submitted_domains = []

    conn = get_db()

    patient = conn.execute(
        """
        SELECT p.*, u.email AS account_email
        FROM patients p
        LEFT JOIN users u
            ON u.patient_id = p.id
           AND u.role = 'patient'
        WHERE p.id = ?
        """,
        (patient_id,)
    ).fetchone()

    if not patient:

        conn.close()

        return jsonify({
            "status": "error",
            "message": "Patient not found"
        }), 404

    # ------------------------------------------------------------------------
    # DETERMINE GAME DOMAINS
    # ------------------------------------------------------------------------

    domains = get_game_domains(
        conn,
        game_name,
        submitted_domains
    )

    # ------------------------------------------------------------------------
    # CALCULATE / NORMALIZE ACCURACY
    # ------------------------------------------------------------------------
    # Explicit accuracy is preserved, including a genuine 0%.
    # If a game does not send accuracy, derive it from score/max_score.
    # If neither is available, keep accuracy as NULL/unavailable.

    if "accuracy" in data and data.get("accuracy") is not None:
        accuracy = safe_float(data.get("accuracy"), None)

        if accuracy is not None and accuracy > 1:
            accuracy = accuracy / 100.0

        if accuracy is not None:
            accuracy = max(0.0, min(accuracy, 1.0))
    elif max_score > 0:
        accuracy = max(0.0, min(score / max_score, 1.0))
    else:
        accuracy = None

    # ------------------------------------------------------------------------
    # ADAPTIVE DIFFICULTY
    # ------------------------------------------------------------------------

    current_level = safe_int(
        patient["difficulty_level"],
        1
    )

    next_level = compute_next_difficulty(
        current_level,
        score,
        max_score,
        time_taken,
        expected_time
    )

    # ------------------------------------------------------------------------
    # STORE UNIVERSAL RESULT
    # ------------------------------------------------------------------------

    conn.execute(
        """
        INSERT INTO game_scores
        (
            patient_id,
            game_name,
            score,
            time_taken_seconds,
            difficulty_level,
            played_at,
            max_score,
            accuracy,
            expected_time_seconds,
            errors,
            hints_used,
            completion_status,
            domains_json,
            metrics_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            patient_id,
            normalise_game_name(game_name),
            score,
            time_taken,
            current_level,
            datetime.now().isoformat(),
            max_score,
            accuracy,
            expected_time,
            errors,
            hints_used,
            completion_status,
            json.dumps(domains),
            json.dumps(metrics)
        )
    )

    # ------------------------------------------------------------------------
    # UPDATE PATIENT'S ADAPTIVE LEVEL
    # ------------------------------------------------------------------------

    conn.execute(
        """
        UPDATE patients
        SET difficulty_level = ?
        WHERE id = ?
        """,
        (
            next_level,
            patient_id
        )
    )

    conn.commit()

    # ------------------------------------------------------------------------
    # CALCULATE UPDATED ANALYTICS
    # ------------------------------------------------------------------------

    all_scores = conn.execute(
        """
        SELECT *
        FROM game_scores
        WHERE patient_id = ?
        ORDER BY played_at ASC
        """,
        (patient_id,)
    ).fetchall()

    analytics = calculate_cognitive_analytics(
        all_scores
    )

    conn.close()

    return jsonify({

        "status": "ok",

        "next_difficulty_level": next_level,

        "recorded_game": normalise_game_name(
            game_name
        ),

        "domains": domains,

        "analytics_signal": analytics[
            "signal"
        ],

        "analytics_confidence": analytics[
            "confidence"
        ]

    })


# ============================================================================
# AI COGNITIVE ANALYTICS PAGE
# ============================================================================

@app.route(
    "/ai-analytics/<int:patient_id>"
)
@login_required
def ai_analytics(patient_id):

    user = current_user()
    if not patient_belongs_to_user(patient_id, user):
        return "Access denied", 403

    conn = get_db()

    patient = conn.execute(
        """
        SELECT p.*, u.email AS account_email
        FROM patients p
        LEFT JOIN users u
            ON u.patient_id = p.id
           AND u.role = 'patient'
        WHERE p.id = ?
        """,
        (patient_id,)
    ).fetchone()

    if not patient:

        conn.close()

        return "Patient not found", 404

    scores = conn.execute(
        """
        SELECT *
        FROM game_scores
        WHERE patient_id = ?
        ORDER BY played_at ASC
        """,
        (patient_id,)
    ).fetchall()

    analytics = calculate_cognitive_analytics(
        scores
    )

    # ------------------------------------------------------------------------
    # GAME REGISTRY
    # ------------------------------------------------------------------------

    registered_games = conn.execute(
        """
        SELECT *
        FROM game_registry
        ORDER BY display_name
        """
    ).fetchall()

    games = []

    for game in registered_games:

        game_dict = dict(game)

        try:
            game_dict["domains"] = json.loads(
                game_dict["domains_json"] or "[]"
            )
        except Exception:
            game_dict["domains"] = []

        games.append(game_dict)

    observations = conn.execute(
        """
        SELECT * FROM caregiver_observations
        WHERE patient_id = ?
        ORDER BY observed_at DESC
        LIMIT 12
        """,
        (patient_id,)
    ).fetchall()

    context_summary = build_ai_context_summary(analytics, observations)
    practice_plan = build_personalized_practice_plan(analytics, games)

    conn.close()

    return render_template(
        "ai_analytics.html",
        patient=patient,
        scores=scores,
        analytics=analytics,
        cognitive_domains=COGNITIVE_DOMAINS,
        registered_games=games,
        observations=observations,
        context_summary=context_summary,
        practice_plan=practice_plan
    )


# ============================================================================
# CAREGIVER OBSERVATIONS API
# ============================================================================

@app.route("/api/caregiver-observations/<int:patient_id>", methods=["GET", "POST"])
@login_required
def caregiver_observations_api(patient_id):
    if not patient_belongs_to_user(patient_id, current_user()):
        return jsonify({"status": "error", "message": "Access denied"}), 403

    conn = get_db()

    patient = conn.execute(
        "SELECT id FROM patients WHERE id = ?",
        (patient_id,)
    ).fetchone()

    if not patient:
        conn.close()
        return jsonify({"status": "error", "message": "Patient not found"}), 404

    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        category = str(data.get("category", "")).strip()
        status = str(data.get("status", "noted")).strip().lower()
        note = str(data.get("note", "")).strip()

        allowed_categories = {
            "Memory / Forgetfulness", "Routine / Daily Activities",
            "Mood / Behaviour", "Medication", "Sleep",
            "Mobility / Activity", "Communication", "Other"
        }
        allowed_statuses = {"noted", "improved", "concerning", "resolved"}

        if category not in allowed_categories:
            conn.close(); return jsonify({"status":"error","message":"Invalid observation category"}), 400
        if status not in allowed_statuses:
            status = "noted"
        if not note:
            conn.close(); return jsonify({"status":"error","message":"Please enter an observation note"}), 400
        if len(note) > 500:
            conn.close(); return jsonify({"status":"error","message":"Observation is too long"}), 400

        conn.execute(
            "INSERT INTO caregiver_observations (patient_id, category, status, note, observed_at) VALUES (?, ?, ?, ?, ?)",
            (patient_id, category, status, note, datetime.now().isoformat())
        )
        conn.commit()

    rows = conn.execute(
        "SELECT id, category, status, note, observed_at FROM caregiver_observations WHERE patient_id = ? ORDER BY observed_at DESC LIMIT 12",
        (patient_id,)
    ).fetchall()
    conn.close()

    return jsonify({"status":"ok", "observations":[dict(row) for row in rows]})


# ============================================================================
# AI ANALYTICS API
# ============================================================================
#
# Useful later for charts, live dashboards and mobile applications.
# ============================================================================

@app.route(
    "/api/ai-analytics/<int:patient_id>"
)
@login_required
def ai_analytics_api(patient_id):

    if not patient_belongs_to_user(patient_id, current_user()):
        return jsonify({"status": "error", "message": "Access denied"}), 403

    conn = get_db()

    patient = conn.execute(
        """
        SELECT p.*, u.email AS account_email
        FROM patients p
        LEFT JOIN users u
            ON u.patient_id = p.id
           AND u.role = 'patient'
        WHERE p.id = ?
        """,
        (patient_id,)
    ).fetchone()

    if not patient:

        conn.close()

        return jsonify({
            "status": "error",
            "message": "Patient not found"
        }), 404

    scores = conn.execute(
        """
        SELECT *
        FROM game_scores
        WHERE patient_id = ?
        ORDER BY played_at ASC
        """,
        (patient_id,)
    ).fetchall()

    analytics = calculate_cognitive_analytics(
        scores
    )

    conn.close()

    return jsonify({

        "status": "ok",

        "patient": {
            "id": patient["id"],
            "name": patient["name"],
            "age": patient["age"]
        },

        "analytics": analytics

    })


# ============================================================================
# REMINDERS API
# ============================================================================

@app.route(
    "/api/reminders/<int:patient_id>",
    methods=["GET", "POST"]
)
@login_required
def reminders_api(patient_id):

    if not patient_belongs_to_user(patient_id, current_user()):
        return jsonify({"status": "error", "message": "Access denied"}), 403

    conn = get_db()

    if request.method == "POST":

        data = request.get_json(silent=True) or {}
        title = str(data.get("title", "")).strip()[:200]
        if not title:
            conn.close()
            return jsonify({
                "status": "error",
                "message": "Reminder title is required"
            }), 400

        conn.execute(
            """
            INSERT INTO reminders
            (
                patient_id,
                title,
                category,
                reminder_time
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                patient_id,
                title,
                str(data.get("category", "general"))[:50],
                str(data.get("reminder_time", ""))[:50]
            )
        )

        conn.commit()
        conn.close()

        return jsonify({
            "status": "created"
        })

    reminders = conn.execute(
        """
        SELECT *
        FROM reminders
        WHERE patient_id = ?
        ORDER BY reminder_time
        """,
        (patient_id,)
    ).fetchall()

    conn.close()

    return jsonify([
        dict(r)
        for r in reminders
    ])


# ============================================================================
# COMPLETE REMINDER
# ============================================================================

@app.route(
    "/api/reminders/<int:reminder_id>/done",
    methods=["POST"]
)
@login_required
def mark_reminder_done(reminder_id):

    conn = get_db()
    reminder = conn.execute(
        "SELECT patient_id FROM reminders WHERE id = ?",
        (reminder_id,)
    ).fetchone()

    if not reminder or not patient_belongs_to_user(reminder["patient_id"], current_user()):
        conn.close()
        return jsonify({"status": "error", "message": "Access denied"}), 403

    conn.execute(
        """
        UPDATE reminders
        SET is_done = 1
        WHERE id = ?
        """,
        (reminder_id,)
    )

    conn.commit()
    conn.close()

    return jsonify({
        "status": "ok"
    })


# ============================================================================
# CAREGIVER PATIENT INVITATION
# ============================================================================

@app.route("/caregiver/invite", methods=["GET", "POST"])
@login_required
def caregiver_invite():
    user = current_user()
    if user["role"] != "caregiver" or not user["caregiver_id"]:
        return "Access denied", 403

    conn = get_db()
    caregiver = conn.execute("SELECT * FROM caregivers WHERE id = ?", (user["caregiver_id"],)).fetchone()
    if not caregiver:
        conn.close()
        return "Caregiver not found", 404

    token = generate_invite_token(conn)
    now = datetime.now()
    expires = now + timedelta(days=7)
    conn.execute(
        "INSERT INTO patient_invitations (caregiver_id, token, created_at, expires_at) VALUES (?, ?, ?, ?)",
        (user["caregiver_id"], token, now.isoformat(), expires.isoformat())
    )
    conn.commit()
    conn.close()

    invite_url = url_for("register", role="patient", invite=token, _external=True)
    return jsonify({
        "status": "ok",
        "caregiver_code": caregiver["caregiver_code"],
        "invite_url": invite_url,
        "expires_at": expires.isoformat()
    })


# ============================================================================
# CAREGIVER DASHBOARD
# ============================================================================
#
# IMPORTANT:
# This remains the MANUAL caregiver dashboard.
#
# AI analytics stays on its own page.
# ============================================================================

@app.route("/caregiver/<int:caregiver_id>/patients")
@login_required
def caregiver_patients(caregiver_id):
    """Dedicated caregiver patient-management workspace."""
    user = current_user()

    if user["role"] != "caregiver" or not caregiver_belongs_to_user(caregiver_id, user):
        return "Access denied", 403

    search = request.args.get("q", "").strip()

    conn = get_db()

    caregiver = conn.execute(
        "SELECT * FROM caregivers WHERE id = ?",
        (caregiver_id,)
    ).fetchone()

    if not caregiver:
        conn.close()
        return "Caregiver not found", 404

    if search:
        like = f"%{search}%"
        patients = conn.execute(
            """
            SELECT
                p.*,
                u.email AS account_email,
                cp.linked_at
            FROM patients p
            JOIN caregiver_patient cp
                ON cp.patient_id = p.id
            LEFT JOIN users u
                ON u.patient_id = p.id
               AND u.role = 'patient'
            WHERE cp.caregiver_id = ?
              AND (
                    p.name LIKE ?
                    OR p.patient_code LIKE ?
                    OR u.email LIKE ?
              )
            ORDER BY p.name COLLATE NOCASE
            """,
            (caregiver_id, like, like, like)
        ).fetchall()
    else:
        patients = conn.execute(
            """
            SELECT
                p.*,
                u.email AS account_email,
                cp.linked_at
            FROM patients p
            JOIN caregiver_patient cp
                ON cp.patient_id = p.id
            LEFT JOIN users u
                ON u.patient_id = p.id
               AND u.role = 'patient'
            WHERE cp.caregiver_id = ?
            ORDER BY p.name COLLATE NOCASE
            """,
            (caregiver_id,)
        ).fetchall()

    # Lightweight activity summary for each patient.
    patient_rows = []
    for patient in patients:
        last_score = conn.execute(
            """
            SELECT played_at, game_name, score
            FROM game_scores
            WHERE patient_id = ?
            ORDER BY played_at DESC
            LIMIT 1
            """,
            (patient["id"],)
        ).fetchone()

        session_count = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM game_scores
            WHERE patient_id = ?
            """,
            (patient["id"],)
        ).fetchone()["count"]

        alert = get_patient_ai_alert(patient["id"])

        patient_rows.append({
            "patient": dict(patient),
            "last_score": dict(last_score) if last_score else None,
            "session_count": session_count,
            "alert": alert
        })

    conn.close()

    return render_template(
        "caregiver_patients.html",
        caregiver=caregiver,
        patients=patient_rows,
        search=search
    )

@app.route(
    "/caregiver/<int:caregiver_id>/patient/<int:patient_id>/remove",
    methods=["POST"]
)
@login_required
def caregiver_remove_patient(caregiver_id, patient_id):
    """Remove the caregiver-patient connection without deleting patient data."""
    user = current_user()

    if user["role"] != "caregiver" or not caregiver_belongs_to_user(
        caregiver_id, user
    ):
        return "Access denied", 403

    conn = get_db()

    # Only remove the relationship.
    # Patient account, game history, AI analytics and reminders remain intact.
    conn.execute(
        """
        DELETE FROM caregiver_patient
        WHERE caregiver_id = ?
          AND patient_id = ?
        """,
        (caregiver_id, patient_id)
    )

    conn.commit()
    conn.close()

    return redirect(
        url_for(
            "caregiver_patients",
            caregiver_id=caregiver_id
        )
    )


@app.route(
    "/caregiver/<int:caregiver_id>/patient/<int:patient_id>"
)
@login_required
def caregiver_patient_view(caregiver_id, patient_id):
    """Open one connected patient's manual caregiver dashboard."""
    user = current_user()

    if user["role"] != "caregiver" or not caregiver_belongs_to_user(caregiver_id, user):
        return "Access denied", 403

    if not patient_belongs_to_user(patient_id, user):
        return "Access denied", 403

    conn = get_db()

    caregiver = conn.execute(
        """
        SELECT *
        FROM caregivers
        WHERE id = ?
        """,
        (caregiver_id,)
    ).fetchone()

    patient = conn.execute(
        """
        SELECT p.*, u.email AS account_email
        FROM patients p
        LEFT JOIN users u
            ON u.patient_id = p.id
           AND u.role = 'patient'
        WHERE p.id = ?
        """,
        (patient_id,)
    ).fetchone()

    if not caregiver:
        conn.close()
        return "Caregiver not found", 404

    if not patient:
        conn.close()
        return "Patient not found", 404

    scores = conn.execute(
        """
        SELECT *
        FROM game_scores
        WHERE patient_id = ?
        ORDER BY played_at ASC
        """,
        (patient_id,)
    ).fetchall()

    reminders = conn.execute(
        """
        SELECT *
        FROM reminders
        WHERE patient_id = ?
        ORDER BY reminder_time
        LIMIT 8
        """,
        (patient_id,)
    ).fetchall()

    observations = conn.execute(
        """
        SELECT *
        FROM caregiver_observations
        WHERE patient_id = ?
        ORDER BY observed_at DESC
        LIMIT 8
        """,
        (patient_id,)
    ).fetchall()

    score_dicts = [dict(score) for score in scores]
    completed_scores = [
        s for s in score_dicts
        if (s.get("completion_status") or "completed") == "completed"
    ]

    def _number(value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    valid_scores = [v for v in (_number(s.get("score")) for s in score_dicts) if v is not None]
    valid_accuracy = [v for v in (_number(s.get("accuracy")) for s in score_dicts) if v is not None]
    valid_times = [v for v in (_number(s.get("time_taken_seconds")) for s in score_dicts) if v is not None and v > 0]

    latest_score = valid_scores[-1] if valid_scores else None
    avg_score = (sum(valid_scores) / len(valid_scores)) if valid_scores else None
    avg_accuracy = (sum(valid_accuracy) / len(valid_accuracy)) if valid_accuracy else None
    latest_time = valid_times[-1] if valid_times else None

    game_summary = []
    seen_games = []
    for s in reversed(score_dicts):
        game_name = s.get("game_name") or "Game"
        if game_name in seen_games:
            continue
        seen_games.append(game_name)
        game_scores = [x for x in score_dicts if (x.get("game_name") or "Game") == game_name]
        gs = [_number(x.get("score")) for x in game_scores]
        gs = [x for x in gs if x is not None]
        ga = [_number(x.get("accuracy")) for x in game_scores]
        ga = [x for x in ga if x is not None]
        game_summary.append({
            "game_name": game_name,
            "sessions": len(game_scores),
            "latest_score": gs[-1] if gs else None,
            "average_score": (sum(gs) / len(gs)) if gs else None,
            "average_accuracy": (sum(ga) / len(ga)) if ga else None,
            "last_played": game_scores[-1].get("played_at") if game_scores else None
        })

    conn.close()

    return render_template(
        "caregiver_patient_profile.html",
        caregiver=caregiver,
        patient=patient,
        scores=score_dicts,
        reminders=reminders,
        observations=observations,
        completed_sessions=len(completed_scores),
        latest_score=latest_score,
        avg_score=avg_score,
        avg_accuracy=avg_accuracy,
        latest_time=latest_time,
        game_summary=game_summary
    )


@app.route(
    "/caregiver/<int:caregiver_id>"
)
@login_required
def caregiver_dashboard(caregiver_id):

    user = current_user()
    if not caregiver_belongs_to_user(caregiver_id, user):
        return "Access denied", 403

    conn = get_db()

    caregiver = conn.execute(
        """
        SELECT *
        FROM caregivers
        WHERE id = ?
        """,
        (caregiver_id,)
    ).fetchone()

    patients = conn.execute(
        """
        SELECT p.*
        FROM patients p
        JOIN caregiver_patient cp ON cp.patient_id = p.id
        WHERE cp.caregiver_id = ?
        ORDER BY p.name COLLATE NOCASE
        """,
        (caregiver_id,)
    ).fetchall()

    patient_data = []

    for patient in patients:

        scores = conn.execute(
            """
            SELECT *
            FROM game_scores
            WHERE patient_id = ?
            ORDER BY played_at
            """,
            (patient["id"],)
        ).fetchall()

        patient_data.append({

            "patient": patient,

            "scores": [
                dict(score)
                for score in scores
            ]

        })

    conn.close()

    return render_template(
        "caregiver_dashboard.html",
        caregiver=caregiver,
        patient_data=patient_data
    )


# ============================================================================
# START APPLICATION
# ============================================================================
# Initialize the database on import so Gunicorn/Render also gets the schema.
init_db()

if __name__ == "__main__":
    app.run(
        debug=False,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5000))
    )