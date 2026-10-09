from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, time as clock_time
import logging
from pathlib import Path
import re
import time
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Connection, Engine, Result
from sqlalchemy.exc import SQLAlchemyError

from app.config import settings

logger = logging.getLogger(__name__)

SQLITE_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS employees(id INTEGER PRIMARY KEY AUTOINCREMENT,staff_id TEXT NOT NULL UNIQUE COLLATE NOCASE,name TEXT NOT NULL,phone TEXT UNIQUE,department TEXT,shift TEXT NOT NULL DEFAULT 'morning',registration_status TEXT NOT NULL DEFAULT 'unregistered',whatsapp_phone TEXT UNIQUE,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS conversation_states(phone TEXT PRIMARY KEY,state TEXT NOT NULL,updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS attendance(id INTEGER PRIMARY KEY AUTOINCREMENT,employee_id INTEGER NOT NULL,work_date TEXT NOT NULL,check_in TEXT,check_out TEXT,attendance_shift TEXT NOT NULL DEFAULT 'first',late_minutes INTEGER NOT NULL DEFAULT 0,early_leave_minutes INTEGER NOT NULL DEFAULT 0,overtime_minutes INTEGER NOT NULL DEFAULT 0,source TEXT NOT NULL DEFAULT 'whatsapp',status TEXT NOT NULL DEFAULT 'present',created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,UNIQUE(employee_id,work_date),FOREIGN KEY(employee_id) REFERENCES employees(id));
CREATE TABLE IF NOT EXISTS whatsapp_logs(id INTEGER PRIMARY KEY AUTOINCREMENT,direction TEXT NOT NULL,phone TEXT,message_type TEXT,content TEXT,message_id TEXT,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE UNIQUE INDEX IF NOT EXISTS ux_whatsapp_incoming_message_id ON whatsapp_logs(message_id) WHERE direction='incoming' AND message_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS pending_registrations(id INTEGER PRIMARY KEY AUTOINCREMENT,employee_id INTEGER NOT NULL,whatsapp_phone TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,FOREIGN KEY(employee_id) REFERENCES employees(id));
CREATE TABLE IF NOT EXISTS system_settings(key TEXT PRIMARY KEY,value TEXT NOT NULL,updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
"""

POSTGRES_STATEMENTS = [
    """CREATE TABLE IF NOT EXISTS employees(id BIGSERIAL PRIMARY KEY,staff_id TEXT NOT NULL UNIQUE,name TEXT NOT NULL,phone TEXT UNIQUE,department TEXT,shift TEXT NOT NULL DEFAULT 'morning',registration_status TEXT NOT NULL DEFAULT 'unregistered',whatsapp_phone TEXT UNIQUE,created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)""",
    """CREATE UNIQUE INDEX IF NOT EXISTS ux_employees_staff_id_lower ON employees(LOWER(staff_id))""",
    """CREATE TABLE IF NOT EXISTS conversation_states(phone TEXT PRIMARY KEY,state TEXT NOT NULL,updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)""",
    """CREATE TABLE IF NOT EXISTS attendance(id BIGSERIAL PRIMARY KEY,employee_id BIGINT NOT NULL REFERENCES employees(id),work_date TEXT NOT NULL,check_in TEXT,check_out TEXT,attendance_shift TEXT NOT NULL DEFAULT 'first',late_minutes INTEGER NOT NULL DEFAULT 0,early_leave_minutes INTEGER NOT NULL DEFAULT 0,overtime_minutes INTEGER NOT NULL DEFAULT 0,source TEXT NOT NULL DEFAULT 'whatsapp',status TEXT NOT NULL DEFAULT 'present',created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,UNIQUE(employee_id,work_date))""",
    """CREATE TABLE IF NOT EXISTS whatsapp_logs(id BIGSERIAL PRIMARY KEY,direction TEXT NOT NULL,phone TEXT,message_type TEXT,content TEXT,message_id TEXT,created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)""",
    """CREATE UNIQUE INDEX IF NOT EXISTS ux_whatsapp_incoming_message_id ON whatsapp_logs(message_id) WHERE direction='incoming' AND message_id IS NOT NULL""",
    """CREATE TABLE IF NOT EXISTS pending_registrations(id BIGSERIAL PRIMARY KEY,employee_id BIGINT NOT NULL REFERENCES employees(id),whatsapp_phone TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)""",
    """CREATE TABLE IF NOT EXISTS system_settings(key TEXT PRIMARY KEY,value TEXT NOT NULL,updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)""",
]


def _make_engine(url: str) -> Engine:
    kwargs = {
        "pool_pre_ping": True,
        "future": True,
    }
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
    else:
        kwargs.update({"pool_size": 5, "max_overflow": 10, "pool_recycle": 300,
                       "pool_timeout": 15, "connect_args": {"connect_timeout": 10}})
    return create_engine(url, **kwargs)


engine: Engine = _make_engine(settings.database_url)
_active_url = settings.database_url
_startup_error = ""


class DBResult:
    def __init__(self, result: Result):
        self.result = result

    def fetchone(self):
        return self.result.mappings().fetchone()

    def fetchall(self):
        return self.result.mappings().fetchall()


class DBConnection:
    def __init__(self, conn: Connection):
        self.conn = conn

    def execute(self, sql: str, params=()):
        if isinstance(params, dict):
            result = self.conn.execute(text(sql), params)
        else:
            params = tuple(params or ())
            names: list[str] = []

            def repl(_match):
                name = f"p{len(names)}"
                names.append(name)
                return f":{name}"

            converted = re.sub(r"\?", repl, sql)
            if len(names) != len(params):
                raise ValueError(f"SQL parameter mismatch: expected {len(names)}, got {len(params)}")
            result = self.conn.execute(text(converted), {name: params[i] for i, name in enumerate(names)})
        return DBResult(result)


@contextmanager
def get_db():
    with engine.begin() as conn:
        yield DBConnection(conn)


def _create_schema() -> None:
    if _active_url.startswith("sqlite"):
        raw = engine.raw_connection()
        try:
            raw.executescript(SQLITE_SCHEMA)
            raw.commit()
        finally:
            raw.close()
    else:
        with engine.begin() as conn:
            for statement in POSTGRES_STATEMENTS:
                conn.execute(text(statement))


def _fallback_to_sqlite(reason: Exception) -> None:
    global engine, _active_url, _startup_error
    _startup_error = str(reason)
    fallback_path = Path("/tmp/buraq_attendance.db")
    fallback_path.parent.mkdir(parents=True, exist_ok=True)
    fallback_url = f"sqlite:///{fallback_path}"
    logger.error("Primary database unavailable; starting with temporary SQLite fallback: %s", reason)
    try:
        engine.dispose()
    except Exception:
        pass
    engine = _make_engine(fallback_url)
    _active_url = fallback_url
    _create_schema()
    apply_feature_migrations()


def init_db(max_attempts: int = 5) -> None:
    global _startup_error
    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            _create_schema()
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            _startup_error = ""
            apply_feature_migrations()
            logger.info("Database ready (%s)", database_kind())
            return
        except Exception as exc:
            last_error = exc
            logger.warning("Database startup attempt %s/%s failed: %s", attempt, max_attempts, exc)
            if attempt < max_attempts:
                time.sleep(min(attempt * 2, 8))
    if _active_url.startswith("sqlite"):
        raise RuntimeError(f"Could not initialize SQLite database: {last_error}") from last_error
    if not settings.allow_temp_db_fallback:
        raise RuntimeError(f"Primary database unavailable and temporary fallback is disabled: {last_error}") from last_error
    _fallback_to_sqlite(last_error or RuntimeError("Unknown database error"))


def database_ok() -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except SQLAlchemyError:
        return False


def database_kind() -> str:
    if _active_url.startswith("postgresql"):
        return "postgresql"
    if _startup_error:
        return "temporary-sqlite-fallback"
    return "sqlite"


def database_warning() -> str:
    if _startup_error:
        return "PostgreSQL পাওয়া যায়নি; app temporary storage-এ চলছে। Railway PostgreSQL যুক্ত করলে data স্থায়ী হবে।"
    return ""


def ensure_migration_table() -> None:
    statement = (
        "CREATE TABLE IF NOT EXISTS schema_migrations(version TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        if _active_url.startswith("sqlite") else
        "CREATE TABLE IF NOT EXISTS schema_migrations(version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    with engine.begin() as conn:
        conn.execute(text(statement))


def migration_applied(version: str) -> bool:
    ensure_migration_table()
    with engine.connect() as conn:
        row = conn.execute(text("SELECT 1 FROM schema_migrations WHERE version=:version"), {"version": version}).first()
    return bool(row)


def mark_migration(version: str) -> None:
    ensure_migration_table()
    sql = (
        "INSERT OR IGNORE INTO schema_migrations(version) VALUES (:version)"
        if _active_url.startswith("sqlite") else
        "INSERT INTO schema_migrations(version) VALUES (:version) ON CONFLICT (version) DO NOTHING"
    )
    with engine.begin() as conn:
        conn.execute(text(sql), {"version": version})


def apply_feature_migrations() -> None:
    """Add v5.2 guided-flow tables without deleting existing Railway data."""
    statements = [
        """CREATE TABLE IF NOT EXISTS face_profiles(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id INTEGER NOT NULL UNIQUE,
            reference_media_id TEXT NOT NULL,
            registered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(employee_id) REFERENCES employees(id)
        )""" if _active_url.startswith("sqlite") else """CREATE TABLE IF NOT EXISTS face_profiles(
            id BIGSERIAL PRIMARY KEY,
            employee_id BIGINT NOT NULL UNIQUE REFERENCES employees(id),
            reference_media_id TEXT NOT NULL,
            registered_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS face_samples(
            id INTEGER PRIMARY KEY AUTOINCREMENT, employee_id INTEGER NOT NULL, media_id TEXT, embedding TEXT NOT NULL, quality REAL NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(employee_id) REFERENCES employees(id)
        )""" if _active_url.startswith("sqlite") else """CREATE TABLE IF NOT EXISTS face_samples(
            id BIGSERIAL PRIMARY KEY, employee_id BIGINT NOT NULL REFERENCES employees(id), media_id TEXT, embedding TEXT NOT NULL, quality DOUBLE PRECISION NOT NULL DEFAULT 0, created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS attendance_evidence(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            latitude REAL,
            longitude REAL,
            distance_meters REAL,
            image_media_id TEXT,
            verified INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(employee_id) REFERENCES employees(id)
        )""" if _active_url.startswith("sqlite") else """CREATE TABLE IF NOT EXISTS attendance_evidence(
            id BIGSERIAL PRIMARY KEY,
            employee_id BIGINT NOT NULL REFERENCES employees(id),
            action TEXT NOT NULL,
            latitude DOUBLE PRECISION,
            longitude DOUBLE PRECISION,
            distance_meters DOUBLE PRECISION,
            image_media_id TEXT,
            verified BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS hr_accounts(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL, email TEXT NOT NULL UNIQUE COLLATE NOCASE,
            password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'hr_officer',
            is_active INTEGER NOT NULL DEFAULT 1,
            last_login_at TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""" if _active_url.startswith("sqlite") else """CREATE TABLE IF NOT EXISTS hr_accounts(
            id BIGSERIAL PRIMARY KEY, name TEXT NOT NULL, email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'hr_officer',
            is_active BOOLEAN NOT NULL DEFAULT TRUE, last_login_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE UNIQUE INDEX IF NOT EXISTS ux_hr_accounts_email_lower ON hr_accounts(LOWER(email))""",
        """CREATE TABLE IF NOT EXISTS account_permissions(
            account_id INTEGER NOT NULL, permission TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(account_id, permission),
            FOREIGN KEY(account_id) REFERENCES hr_accounts(id) ON DELETE CASCADE
        )""" if _active_url.startswith("sqlite") else """CREATE TABLE IF NOT EXISTS account_permissions(
            account_id BIGINT NOT NULL REFERENCES hr_accounts(id) ON DELETE CASCADE,
            permission TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(account_id, permission)
        )""",
        """CREATE TABLE IF NOT EXISTS audit_logs(
            id INTEGER PRIMARY KEY AUTOINCREMENT, actor_type TEXT NOT NULL, actor_id TEXT,
            actor_name TEXT, action TEXT NOT NULL, target_type TEXT, target_id TEXT,
            details TEXT, ip_address TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""" if _active_url.startswith("sqlite") else """CREATE TABLE IF NOT EXISTS audit_logs(
            id BIGSERIAL PRIMARY KEY, actor_type TEXT NOT NULL, actor_id TEXT, actor_name TEXT,
            action TEXT NOT NULL, target_type TEXT, target_id TEXT, details TEXT, ip_address TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS leave_requests(id INTEGER PRIMARY KEY AUTOINCREMENT,employee_id INTEGER NOT NULL,leave_type TEXT NOT NULL,start_date TEXT NOT NULL,end_date TEXT NOT NULL,reason TEXT,status TEXT NOT NULL DEFAULT 'pending',requested_by TEXT,decided_by TEXT,decided_at TEXT,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,FOREIGN KEY(employee_id) REFERENCES employees(id))""" if _active_url.startswith("sqlite") else """CREATE TABLE IF NOT EXISTS leave_requests(id BIGSERIAL PRIMARY KEY,employee_id BIGINT NOT NULL REFERENCES employees(id),leave_type TEXT NOT NULL,start_date TEXT NOT NULL,end_date TEXT NOT NULL,reason TEXT,status TEXT NOT NULL DEFAULT 'pending',requested_by TEXT,decided_by TEXT,decided_at TIMESTAMPTZ,created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)""",
        """CREATE TABLE IF NOT EXISTS attendance_corrections(id INTEGER PRIMARY KEY AUTOINCREMENT,employee_id INTEGER NOT NULL,work_date TEXT NOT NULL,requested_check_in TEXT,requested_check_out TEXT,reason TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',requested_by TEXT,decided_by TEXT,decided_at TEXT,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,FOREIGN KEY(employee_id) REFERENCES employees(id))""" if _active_url.startswith("sqlite") else """CREATE TABLE IF NOT EXISTS attendance_corrections(id BIGSERIAL PRIMARY KEY,employee_id BIGINT NOT NULL REFERENCES employees(id),work_date TEXT NOT NULL,requested_check_in TEXT,requested_check_out TEXT,reason TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',requested_by TEXT,decided_by TEXT,decided_at TIMESTAMPTZ,created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)""",
        """CREATE TABLE IF NOT EXISTS shifts(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL UNIQUE,start_time TEXT NOT NULL,end_time TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""" if _active_url.startswith("sqlite") else """CREATE TABLE IF NOT EXISTS shifts(id BIGSERIAL PRIMARY KEY,name TEXT NOT NULL UNIQUE,start_time TEXT NOT NULL,end_time TEXT NOT NULL,created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)""",
        """CREATE TABLE IF NOT EXISTS departments(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL UNIQUE,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""" if _active_url.startswith("sqlite") else """CREATE TABLE IF NOT EXISTS departments(id BIGSERIAL PRIMARY KEY,name TEXT NOT NULL UNIQUE,created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)""",
        """CREATE TABLE IF NOT EXISTS employee_notes(id INTEGER PRIMARY KEY AUTOINCREMENT,employee_id INTEGER NOT NULL,note_type TEXT NOT NULL DEFAULT 'general',note TEXT NOT NULL,created_by TEXT,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,FOREIGN KEY(employee_id) REFERENCES employees(id) ON DELETE CASCADE)""" if _active_url.startswith("sqlite") else """CREATE TABLE IF NOT EXISTS employee_notes(id BIGSERIAL PRIMARY KEY,employee_id BIGINT NOT NULL REFERENCES employees(id) ON DELETE CASCADE,note_type TEXT NOT NULL DEFAULT 'general',note TEXT NOT NULL,created_by TEXT,created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)""",
        """CREATE TABLE IF NOT EXISTS performance_reviews(id INTEGER PRIMARY KEY AUTOINCREMENT,employee_id INTEGER NOT NULL,review_period TEXT NOT NULL,attendance_rating INTEGER NOT NULL,discipline_rating INTEGER NOT NULL,work_quality_rating INTEGER NOT NULL,teamwork_rating INTEGER NOT NULL,communication_rating INTEGER NOT NULL,responsibility_rating INTEGER NOT NULL,overall_rating REAL NOT NULL,comments TEXT,goals TEXT,reviewed_by TEXT,reviewed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,FOREIGN KEY(employee_id) REFERENCES employees(id) ON DELETE CASCADE)""" if _active_url.startswith("sqlite") else """CREATE TABLE IF NOT EXISTS performance_reviews(id BIGSERIAL PRIMARY KEY,employee_id BIGINT NOT NULL REFERENCES employees(id) ON DELETE CASCADE,review_period TEXT NOT NULL,attendance_rating INTEGER NOT NULL,discipline_rating INTEGER NOT NULL,work_quality_rating INTEGER NOT NULL,teamwork_rating INTEGER NOT NULL,communication_rating INTEGER NOT NULL,responsibility_rating INTEGER NOT NULL,overall_rating DOUBLE PRECISION NOT NULL,comments TEXT,goals TEXT,reviewed_by TEXT,reviewed_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)""",
        """CREATE TABLE IF NOT EXISTS performance_notices(id INTEGER PRIMARY KEY AUTOINCREMENT,employee_id INTEGER NOT NULL,period TEXT NOT NULL,notice_type TEXT NOT NULL,score REAL NOT NULL DEFAULT 0,rank_position INTEGER NOT NULL DEFAULT 0,message TEXT,sent_by TEXT,sent_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,UNIQUE(employee_id,period,notice_type),FOREIGN KEY(employee_id) REFERENCES employees(id) ON DELETE CASCADE)""" if _active_url.startswith("sqlite") else """CREATE TABLE IF NOT EXISTS performance_notices(id BIGSERIAL PRIMARY KEY,employee_id BIGINT NOT NULL REFERENCES employees(id) ON DELETE CASCADE,period TEXT NOT NULL,notice_type TEXT NOT NULL,score DOUBLE PRECISION NOT NULL DEFAULT 0,rank_position INTEGER NOT NULL DEFAULT 0,message TEXT,sent_by TEXT,sent_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,UNIQUE(employee_id,period,notice_type))""",
        """CREATE TABLE IF NOT EXISTS attendance_fingerprints(
            id INTEGER PRIMARY KEY AUTOINCREMENT, employee_id INTEGER NOT NULL, action TEXT NOT NULL,
            media_id TEXT, phash TEXT NOT NULL, ahash TEXT NOT NULL, dhash TEXT NOT NULL,
            image_data TEXT, latitude REAL, longitude REAL, distance_meters REAL,
            embedding TEXT NOT NULL, pose TEXT, yaw REAL NOT NULL DEFAULT 0, landmarks TEXT,
            duplicate_score REAL NOT NULL DEFAULT 0, hash_score REAL NOT NULL DEFAULT 0,
            face_score REAL NOT NULL DEFAULT 0, pose_score REAL NOT NULL DEFAULT 0,
            landmark_score REAL NOT NULL DEFAULT 0, matched_fingerprint_id INTEGER,
            decision TEXT NOT NULL, review_status TEXT NOT NULL DEFAULT 'none', reviewed_by TEXT,
            reviewed_at TEXT, attendance_applied INTEGER NOT NULL DEFAULT 0,
            attendance_result TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(employee_id) REFERENCES employees(id), FOREIGN KEY(matched_fingerprint_id) REFERENCES attendance_fingerprints(id)
        )""" if _active_url.startswith("sqlite") else """CREATE TABLE IF NOT EXISTS attendance_fingerprints(
            id BIGSERIAL PRIMARY KEY, employee_id BIGINT NOT NULL REFERENCES employees(id), action TEXT NOT NULL,
            media_id TEXT, phash TEXT NOT NULL, ahash TEXT NOT NULL, dhash TEXT NOT NULL,
            image_data TEXT, latitude DOUBLE PRECISION, longitude DOUBLE PRECISION, distance_meters DOUBLE PRECISION,
            embedding TEXT NOT NULL, pose TEXT, yaw DOUBLE PRECISION NOT NULL DEFAULT 0, landmarks TEXT,
            duplicate_score DOUBLE PRECISION NOT NULL DEFAULT 0, hash_score DOUBLE PRECISION NOT NULL DEFAULT 0,
            face_score DOUBLE PRECISION NOT NULL DEFAULT 0, pose_score DOUBLE PRECISION NOT NULL DEFAULT 0,
            landmark_score DOUBLE PRECISION NOT NULL DEFAULT 0, matched_fingerprint_id BIGINT REFERENCES attendance_fingerprints(id),
            decision TEXT NOT NULL, review_status TEXT NOT NULL DEFAULT 'none', reviewed_by TEXT,
            reviewed_at TIMESTAMPTZ, attendance_applied BOOLEAN NOT NULL DEFAULT FALSE,
            attendance_result TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
        "CREATE INDEX IF NOT EXISTS ix_attendance_fingerprints_employee_created ON attendance_fingerprints(employee_id, created_at)",
        "CREATE INDEX IF NOT EXISTS ix_attendance_fingerprints_review ON attendance_fingerprints(decision, review_status)",
        """CREATE TABLE IF NOT EXISTS payroll_records(
            id INTEGER PRIMARY KEY AUTOINCREMENT, employee_id INTEGER NOT NULL, salary_month TEXT NOT NULL,
            fixed_salary REAL NOT NULL DEFAULT 0, overtime_hours REAL NOT NULL DEFAULT 0,
            overtime_rate REAL NOT NULL DEFAULT 0, overtime_amount REAL NOT NULL DEFAULT 0,
            bonus REAL NOT NULL DEFAULT 0, deduction REAL NOT NULL DEFAULT 0,
            net_salary REAL NOT NULL DEFAULT 0, payment_status TEXT NOT NULL DEFAULT 'unpaid',
            note TEXT, created_by TEXT, updated_by TEXT, finalized_at TEXT, paid_at TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(employee_id,salary_month), FOREIGN KEY(employee_id) REFERENCES employees(id)
        )""" if _active_url.startswith("sqlite") else """CREATE TABLE IF NOT EXISTS payroll_records(
            id BIGSERIAL PRIMARY KEY, employee_id BIGINT NOT NULL REFERENCES employees(id), salary_month TEXT NOT NULL,
            fixed_salary DOUBLE PRECISION NOT NULL DEFAULT 0, overtime_hours DOUBLE PRECISION NOT NULL DEFAULT 0,
            overtime_rate DOUBLE PRECISION NOT NULL DEFAULT 0, overtime_amount DOUBLE PRECISION NOT NULL DEFAULT 0,
            bonus DOUBLE PRECISION NOT NULL DEFAULT 0, deduction DOUBLE PRECISION NOT NULL DEFAULT 0,
            net_salary DOUBLE PRECISION NOT NULL DEFAULT 0, payment_status TEXT NOT NULL DEFAULT 'unpaid',
            note TEXT, created_by TEXT, updated_by TEXT, finalized_at TIMESTAMPTZ, paid_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(employee_id,salary_month)
        )""",
        "CREATE INDEX IF NOT EXISTS ix_payroll_month_status ON payroll_records(salary_month,payment_status)",
        """CREATE TABLE IF NOT EXISTS payroll_change_logs(
            id INTEGER PRIMARY KEY AUTOINCREMENT, payroll_id INTEGER NOT NULL, action TEXT NOT NULL,
            actor TEXT, reason TEXT, snapshot TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(payroll_id) REFERENCES payroll_records(id) ON DELETE CASCADE
        )""" if _active_url.startswith("sqlite") else """CREATE TABLE IF NOT EXISTS payroll_change_logs(
            id BIGSERIAL PRIMARY KEY, payroll_id BIGINT NOT NULL REFERENCES payroll_records(id) ON DELETE CASCADE,
            action TEXT NOT NULL, actor TEXT, reason TEXT, snapshot TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
        "CREATE INDEX IF NOT EXISTS ix_payroll_changes_payroll ON payroll_change_logs(payroll_id,created_at)",
        "CREATE INDEX IF NOT EXISTS ix_face_samples_employee ON face_samples(employee_id)",
        "CREATE INDEX IF NOT EXISTS ix_attendance_work_date ON attendance(work_date)",
        "CREATE INDEX IF NOT EXISTS ix_attendance_evidence_employee_created ON attendance_evidence(employee_id,created_at)",
        "CREATE INDEX IF NOT EXISTS ix_pending_registrations_status ON pending_registrations(status)",
        "CREATE INDEX IF NOT EXISTS ix_leave_requests_status_dates ON leave_requests(status,start_date,end_date)",
        "CREATE INDEX IF NOT EXISTS ix_corrections_status ON attendance_corrections(status)",
        "CREATE INDEX IF NOT EXISTS ix_performance_employee_reviewed ON performance_reviews(employee_id,reviewed_at)",
        "CREATE INDEX IF NOT EXISTS ix_fingerprints_phash ON attendance_fingerprints(phash)",
        "CREATE INDEX IF NOT EXISTS ix_fingerprints_ahash ON attendance_fingerprints(ahash)",
        "CREATE INDEX IF NOT EXISTS ix_fingerprints_dhash ON attendance_fingerprints(dhash)",
        """CREATE TABLE IF NOT EXISTS duty_schedules(
            id INTEGER PRIMARY KEY AUTOINCREMENT, employee_id INTEGER NOT NULL, weekday INTEGER NOT NULL,
            start_time TEXT NOT NULL, end_time TEXT NOT NULL, break_minutes INTEGER NOT NULL DEFAULT 0,
            office_name TEXT, is_active INTEGER NOT NULL DEFAULT 1,
            created_by TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(employee_id,weekday), FOREIGN KEY(employee_id) REFERENCES employees(id) ON DELETE CASCADE
        )""" if _active_url.startswith("sqlite") else """CREATE TABLE IF NOT EXISTS duty_schedules(
            id BIGSERIAL PRIMARY KEY, employee_id BIGINT NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
            weekday INTEGER NOT NULL, start_time TEXT NOT NULL, end_time TEXT NOT NULL,
            break_minutes INTEGER NOT NULL DEFAULT 0, office_name TEXT,
            is_active BOOLEAN NOT NULL DEFAULT TRUE, created_by TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(employee_id,weekday)
        )""",
        """CREATE TABLE IF NOT EXISTS duty_reminder_logs(
            id INTEGER PRIMARY KEY AUTOINCREMENT, employee_id INTEGER NOT NULL, duty_date TEXT NOT NULL,
            reminder_type TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'sent', details TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(employee_id,duty_date,reminder_type),
            FOREIGN KEY(employee_id) REFERENCES employees(id) ON DELETE CASCADE
        )""" if _active_url.startswith("sqlite") else """CREATE TABLE IF NOT EXISTS duty_reminder_logs(
            id BIGSERIAL PRIMARY KEY, employee_id BIGINT NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
            duty_date TEXT NOT NULL, reminder_type TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'sent', details TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(employee_id,duty_date,reminder_type)
        )""",
        "CREATE INDEX IF NOT EXISTS ix_duty_schedules_weekday_active ON duty_schedules(weekday,is_active)",
        "CREATE INDEX IF NOT EXISTS ix_duty_reminders_date ON duty_reminder_logs(duty_date)",
        """CREATE TABLE IF NOT EXISTS custom_duties(
            id INTEGER PRIMARY KEY AUTOINCREMENT, employee_id INTEGER NOT NULL, duty_date TEXT NOT NULL,
            start_time TEXT NOT NULL, end_time TEXT NOT NULL, break_minutes INTEGER NOT NULL DEFAULT 0,
            office_name TEXT, note TEXT,
            is_active INTEGER NOT NULL DEFAULT 1, created_by TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(employee_id,duty_date), FOREIGN KEY(employee_id) REFERENCES employees(id) ON DELETE CASCADE
        )""" if _active_url.startswith("sqlite") else """CREATE TABLE IF NOT EXISTS custom_duties(
            id BIGSERIAL PRIMARY KEY, employee_id BIGINT NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
            duty_date TEXT NOT NULL, start_time TEXT NOT NULL, end_time TEXT NOT NULL,
            break_minutes INTEGER NOT NULL DEFAULT 0, office_name TEXT, note TEXT,
            is_active BOOLEAN NOT NULL DEFAULT TRUE, created_by TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(employee_id,duty_date)
        )""",
        "CREATE INDEX IF NOT EXISTS ix_custom_duties_date_active ON custom_duties(duty_date,is_active)",
    ]
    with engine.begin() as conn:
        for statement in statements:
            conn.execute(text(statement))

    # Older v9.5 deployments may already have attendance_fingerprints with a
    # smaller column set. CREATE TABLE IF NOT EXISTS does not upgrade that
    # table, so later INSERTs could fail only when a selfie arrived.
    sqlite = _active_url.startswith("sqlite")
    real = "REAL NOT NULL DEFAULT 0" if sqlite else "DOUBLE PRECISION NOT NULL DEFAULT 0"
    fingerprint_columns = [
        ("media_id", "TEXT"), ("image_data", "TEXT"),
        ("latitude", "REAL" if sqlite else "DOUBLE PRECISION"),
        ("longitude", "REAL" if sqlite else "DOUBLE PRECISION"),
        ("distance_meters", "REAL" if sqlite else "DOUBLE PRECISION"),
        ("pose", "TEXT"), ("yaw", real),
        ("landmarks", "TEXT"), ("duplicate_score", real),
        ("hash_score", real), ("face_score", real), ("pose_score", real),
        ("landmark_score", real),
        ("matched_fingerprint_id", "INTEGER" if sqlite else "BIGINT"),
        ("decision", "TEXT NOT NULL DEFAULT 'accept'"),
        ("review_status", "TEXT NOT NULL DEFAULT 'none'"),
        ("reviewed_by", "TEXT"),
        ("reviewed_at", "TEXT" if sqlite else "TIMESTAMPTZ"),
        ("attendance_applied", "INTEGER NOT NULL DEFAULT 0" if sqlite else "BOOLEAN NOT NULL DEFAULT FALSE"),
        ("attendance_result", "TEXT"),
    ]
    existing_fingerprints = {col["name"] for col in inspect(engine).get_columns("attendance_fingerprints")}
    for column, definition in fingerprint_columns:
        if column in existing_fingerprints:
            continue
        with engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE attendance_fingerprints ADD COLUMN {column} {definition}"))

    for table_name in ("duty_schedules", "custom_duties"):
        duty_columns = {col["name"] for col in inspect(engine).get_columns(table_name)}
        if "break_minutes" not in duty_columns:
            with engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN break_minutes INTEGER NOT NULL DEFAULT 0"))
    attendance_columns = {col["name"] for col in inspect(engine).get_columns("attendance")}
    if "attendance_shift" not in attendance_columns:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE attendance ADD COLUMN attendance_shift TEXT NOT NULL DEFAULT 'first'"))
    mark_migration("v9.5-attendance-fingerprints")
    mark_migration("v9.6-private-payroll")
    mark_migration("v9.8-performance-optimization")
    mark_migration("v9.9-zero-touch-duty-reminders")
    mark_migration("v9.9.1-custom-duty")
    mark_migration("v9.19.7-fingerprint-schema-repair")
    mark_migration("v9.20-mandatory-selfie-review")
    mark_migration("v9.21-duty-break-payroll")
    mark_migration("v9.23-auto-attendance-shift")
    # v9.24 keeps the configurable shift rules as system_settings rows only, so
    # no table is created, altered, dropped or truncated for this release.
    mark_migration("v9.24-configurable-shift-rules")
    mark_migration("v9.26-leave-performance")
    # v9.24.0 accidentally shipped the complete default rule set with a
    # 10:00 AM Second Shift end and a 3:00 PM cutoff. Correct only that exact
    # generated set. Any HR-customized combination is deliberately preserved.
    shift_fix_migration = "v9.24.1-correct-shift-defaults"
    if not migration_applied(shift_fix_migration):
        shift_keys = (
            "shift_first_start", "shift_first_end", "shift_second_start",
            "shift_second_end", "shift_second_cutoff",
        )
        with engine.begin() as conn:
            rows = conn.execute(
                text("SELECT key,value FROM system_settings WHERE key IN "
                     "(:first_start,:first_end,:second_start,:second_end,:cutoff)"),
                {
                    "first_start": shift_keys[0], "first_end": shift_keys[1],
                    "second_start": shift_keys[2], "second_end": shift_keys[3],
                    "cutoff": shift_keys[4],
                },
            ).mappings().all()
            stored = {str(row["key"]): str(row["value"]) for row in rows}
            broken_defaults = {
                "shift_first_start": "08:30", "shift_first_end": "16:00",
                "shift_second_start": "16:00", "shift_second_end": "10:00",
                "shift_second_cutoff": "15:00",
            }
            if stored == broken_defaults:
                conn.execute(
                    text("UPDATE system_settings SET value=:value,updated_at=CURRENT_TIMESTAMP "
                         "WHERE key=:key"),
                    {"key": "shift_second_end", "value": "22:00"},
                )
                conn.execute(
                    text("UPDATE system_settings SET value=:value,updated_at=CURRENT_TIMESTAMP "
                         "WHERE key=:key"),
                    {"key": "shift_second_cutoff", "value": "16:00"},
                )
        mark_migration(shift_fix_migration)

    # Existing deployments received attendance_shift='first' when the column
    # was introduced. Repair those labels without deleting attendance, payroll,
    # employees, or user-configured shift rules.
    shift_backfill_migration = "v9.26.1-correct-existing-attendance-shifts"
    if not migration_applied(shift_backfill_migration):
        corrected = 0
        with engine.begin() as conn:
            cutoff_row = conn.execute(
                text("SELECT value FROM system_settings WHERE key=:key"),
                {"key": "shift_second_cutoff"},
            ).mappings().first()
            try:
                cutoff = clock_time.fromisoformat(
                    str(cutoff_row["value"] if cutoff_row else settings.second_shift_from)
                )
            except ValueError:
                cutoff = clock_time(16, 0)
            rows = conn.execute(text(
                "SELECT a.id,a.employee_id,a.work_date,a.check_in,a.attendance_shift,"
                "e.shift employee_shift FROM attendance a "
                "JOIN employees e ON e.id=a.employee_id WHERE a.check_in IS NOT NULL"
            )).mappings().all()
            local_zone = ZoneInfo(settings.timezone)
            for row in rows:
                try:
                    work_date = date.fromisoformat(str(row["work_date"]))
                    checked_in = datetime.fromisoformat(
                        str(row["check_in"]).replace("Z", "+00:00")
                    )
                    checked_in = (
                        checked_in.replace(tzinfo=local_zone)
                        if checked_in.tzinfo is None
                        else checked_in.astimezone(local_zone)
                    )
                except (TypeError, ValueError):
                    continue

                duty = conn.execute(text(
                    "SELECT start_time FROM custom_duties WHERE employee_id=:employee_id "
                    "AND duty_date=:duty_date AND is_active"
                ), {"employee_id": row["employee_id"], "duty_date": work_date.isoformat()}).mappings().first()
                if not duty:
                    duty = conn.execute(text(
                        "SELECT start_time FROM duty_schedules WHERE employee_id=:employee_id "
                        "AND weekday=:weekday AND is_active"
                    ), {"employee_id": row["employee_id"], "weekday": work_date.weekday()}).mappings().first()
                assigned_start = None
                if duty:
                    try:
                        assigned_start = clock_time.fromisoformat(str(duty["start_time"]))
                    except ValueError:
                        pass
                explicit_second = str(row["employee_shift"] or "").strip().lower() in {
                    "second", "second shift", "evening", "night", "2", "2nd"
                }
                if assigned_start is not None:
                    expected = "second" if assigned_start >= cutoff else "first"
                else:
                    expected = "second" if explicit_second or checked_in.time() >= cutoff else "first"
                if str(row["attendance_shift"] or "").lower() != expected:
                    conn.execute(text(
                        "UPDATE attendance SET attendance_shift=:shift,"
                        "updated_at=CURRENT_TIMESTAMP WHERE id=:attendance_id"
                    ), {"shift": expected, "attendance_id": row["id"]})
                    corrected += 1
        mark_migration(shift_backfill_migration)
        logger.info("Attendance shift correction complete: %s existing records updated", corrected)

    # v9.2 employee profile fields. Each ALTER is independent so existing databases
    # upgrade safely and duplicate-column errors do not interrupt startup.
    employee_columns = [
        ("designation", "TEXT"), ("reporting_manager", "TEXT"),
        ("office_name", "TEXT"), ("join_date", "TEXT"),
        ("profile_photo_url", "TEXT"), ("emergency_name", "TEXT"),
        ("emergency_relation", "TEXT"), ("emergency_phone", "TEXT"),
        ("is_active", "INTEGER NOT NULL DEFAULT 1" if _active_url.startswith("sqlite") else "BOOLEAN NOT NULL DEFAULT TRUE"),
        ("fixed_salary", "REAL NOT NULL DEFAULT 0" if _active_url.startswith("sqlite") else "DOUBLE PRECISION NOT NULL DEFAULT 0"),
        ("default_overtime_rate", "REAL NOT NULL DEFAULT 0" if _active_url.startswith("sqlite") else "DOUBLE PRECISION NOT NULL DEFAULT 0"),
    ]
    existing_columns = {col["name"] for col in inspect(engine).get_columns("employees")}
    for column, definition in employee_columns:
        if column in existing_columns:
            continue
        with engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE employees ADD COLUMN {column} {definition}"))

    payroll_columns = [
        ("scheduled_duty_days", "INTEGER NOT NULL DEFAULT 0"),
        ("worked_duty_days", "INTEGER NOT NULL DEFAULT 0"),
        ("paid_leave_days", "INTEGER NOT NULL DEFAULT 0"),
        ("absent_days", "INTEGER NOT NULL DEFAULT 0"),
        ("absent_deduction", "REAL NOT NULL DEFAULT 0" if _active_url.startswith("sqlite") else "DOUBLE PRECISION NOT NULL DEFAULT 0"),
        ("worked_duty_units", "REAL NOT NULL DEFAULT 0" if _active_url.startswith("sqlite") else "DOUBLE PRECISION NOT NULL DEFAULT 0"),
        ("paid_leave_units", "REAL NOT NULL DEFAULT 0" if _active_url.startswith("sqlite") else "DOUBLE PRECISION NOT NULL DEFAULT 0"),
        ("unpaid_leave_units", "REAL NOT NULL DEFAULT 0" if _active_url.startswith("sqlite") else "DOUBLE PRECISION NOT NULL DEFAULT 0"),
        ("absent_duty_units", "REAL NOT NULL DEFAULT 0" if _active_url.startswith("sqlite") else "DOUBLE PRECISION NOT NULL DEFAULT 0"),
        ("unpaid_leave_deduction", "REAL NOT NULL DEFAULT 0" if _active_url.startswith("sqlite") else "DOUBLE PRECISION NOT NULL DEFAULT 0"),
        ("earned_basic_salary", "REAL NOT NULL DEFAULT 0" if _active_url.startswith("sqlite") else "DOUBLE PRECISION NOT NULL DEFAULT 0"),
        ("late_minutes", "INTEGER NOT NULL DEFAULT 0"),
        ("late_deduction", "REAL NOT NULL DEFAULT 0" if _active_url.startswith("sqlite") else "DOUBLE PRECISION NOT NULL DEFAULT 0"),
        ("payable_duty_minutes", "INTEGER NOT NULL DEFAULT 0"),
        ("advance_amount", "REAL NOT NULL DEFAULT 0" if _active_url.startswith("sqlite") else "DOUBLE PRECISION NOT NULL DEFAULT 0"),
        ("fine_amount", "REAL NOT NULL DEFAULT 0" if _active_url.startswith("sqlite") else "DOUBLE PRECISION NOT NULL DEFAULT 0"),
        ("gross_salary", "REAL NOT NULL DEFAULT 0" if _active_url.startswith("sqlite") else "DOUBLE PRECISION NOT NULL DEFAULT 0"),
        ("total_deduction", "REAL NOT NULL DEFAULT 0" if _active_url.startswith("sqlite") else "DOUBLE PRECISION NOT NULL DEFAULT 0"),
        # Earning components the salary sheet has always carried by hand. Each
        # is a plain amount entered by HR and added to gross pay; none of them
        # is derived from attendance.
        ("night_allowance", "REAL NOT NULL DEFAULT 0" if _active_url.startswith("sqlite") else "DOUBLE PRECISION NOT NULL DEFAULT 0"),
        ("friday_allowance", "REAL NOT NULL DEFAULT 0" if _active_url.startswith("sqlite") else "DOUBLE PRECISION NOT NULL DEFAULT 0"),
        ("eid_duty_allowance", "REAL NOT NULL DEFAULT 0" if _active_url.startswith("sqlite") else "DOUBLE PRECISION NOT NULL DEFAULT 0"),
        # Every write path stores 'manual' — overtime is never derived
        # automatically. A default of 'auto' left legacy rows whose overtime
        # hours were then silently read as zero. See scripts/migrate_overtime_mode.py
        ("overtime_mode", "TEXT NOT NULL DEFAULT 'manual'"),
        ("adjustment_reason", "TEXT"),
        ("payment_method", "TEXT"),
        ("payment_reference", "TEXT"),
        ("locked_at", "TEXT" if _active_url.startswith("sqlite") else "TIMESTAMPTZ"),
        ("locked_by", "TEXT"),
        ("reopened_at", "TEXT" if _active_url.startswith("sqlite") else "TIMESTAMPTZ"),
        ("reopen_reason", "TEXT"),
        ("calculation_snapshot", "TEXT"),
    ]
    existing_payroll = {col["name"] for col in inspect(engine).get_columns("payroll_records")}
    for column, definition in payroll_columns:
        if column in existing_payroll:
            continue
        with engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE payroll_records ADD COLUMN {column} {definition}"))
    with engine.begin() as conn:
        conn.execute(text("UPDATE payroll_records SET payment_status='draft' WHERE payment_status='unpaid'"))
    mark_migration("v9.11-duty-based-salary")
    mark_migration("v9.12-payroll-pro")
    sqlite = _active_url.startswith("sqlite")
    pk = "INTEGER PRIMARY KEY AUTOINCREMENT" if sqlite else "BIGSERIAL PRIMARY KEY"
    fk = "INTEGER" if sqlite else "BIGINT"
    stamp = "TEXT" if sqlite else "TIMESTAMPTZ"
    with engine.begin() as conn:
        conn.execute(text(f"""CREATE TABLE IF NOT EXISTS special_duties(
            id {pk}, employee_id {fk} NOT NULL REFERENCES employees(id),
            duty_date TEXT NOT NULL, duty_type TEXT NOT NULL CHECK(duty_type IN ('night','friday','eid','other')),
            start_time TEXT NOT NULL, end_time TEXT NOT NULL,
            payment_amount NUMERIC(14,2) NOT NULL CHECK(payment_amount>0),
            note TEXT NOT NULL, created_by TEXT NOT NULL,
            created_at {stamp} NOT NULL DEFAULT CURRENT_TIMESTAMP,
            cancelled_at {stamp}, cancelled_by TEXT, cancel_reason TEXT
        )"""))
        conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ux_special_duty_active ON special_duties(employee_id,duty_date,duty_type) WHERE cancelled_at IS NULL"))
    mark_migration("payroll-special-duties-v1")
    apply_face_ai_migrations()


def apply_face_ai_migrations() -> None:
    """Telemetry for face verification, so thresholds can be tuned from evidence."""
    sqlite = _active_url.startswith("sqlite")
    real = "REAL" if sqlite else "DOUBLE PRECISION"
    statements = [
        f"""CREATE TABLE IF NOT EXISTS face_events(
            id {"INTEGER PRIMARY KEY AUTOINCREMENT" if sqlite else "BIGSERIAL PRIMARY KEY"},
            employee_id {"INTEGER" if sqlite else "BIGINT"},
            stage TEXT NOT NULL,
            action TEXT,
            decision TEXT NOT NULL,
            reason TEXT,
            match_score {real} NOT NULL DEFAULT 0,
            impostor_score {real} NOT NULL DEFAULT 0,
            margin {real} NOT NULL DEFAULT 0,
            impostor_employee_id {"INTEGER" if sqlite else "BIGINT"},
            quality {real} NOT NULL DEFAULT 0,
            blur {real} NOT NULL DEFAULT 0,
            brightness {real} NOT NULL DEFAULT 0,
            face_ratio {real} NOT NULL DEFAULT 0,
            pose TEXT,
            liveness_score {real} NOT NULL DEFAULT 0,
            liveness_verdict TEXT,
            liveness_detail TEXT,
            liveness_model {"INTEGER NOT NULL DEFAULT 0" if sqlite else "BOOLEAN NOT NULL DEFAULT FALSE"},
            elapsed_ms {real} NOT NULL DEFAULT 0,
            created_at {"TEXT" if sqlite else "TIMESTAMPTZ"} NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
        "CREATE INDEX IF NOT EXISTS ix_face_events_created ON face_events(created_at)",
        "CREATE INDEX IF NOT EXISTS ix_face_events_stage_decision ON face_events(stage, decision)",
    ]
    with engine.begin() as conn:
        for statement in statements:
            conn.execute(text(statement))
    existing_samples = {col["name"] for col in inspect(engine).get_columns("face_samples")}
    if "source" not in existing_samples:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE face_samples ADD COLUMN source TEXT NOT NULL DEFAULT 'enroll'"))
    mark_migration("v9.20-face-ai-telemetry")
    from app.browsing import apply_browsing_migrations
    apply_browsing_migrations(engine, sqlite)
    mark_migration("browsing-tracker-v1")
    from app.ai_insights import apply_ai_migrations
    apply_ai_migrations(engine, sqlite)
    mark_migration("ai-insights-v1")
    from app.ai_commands import apply_ai_command_migrations
    apply_ai_command_migrations(engine, sqlite)
    mark_migration("ai-commands-v1")
    from app.web_checkin import apply_web_checkin_migrations
    apply_web_checkin_migrations(engine, sqlite)
    from app.salary_sheet import apply_salary_sheet_migrations
    apply_salary_sheet_migrations(engine, sqlite)
    mark_migration("web-checkin-v1")
    mark_migration("salary-sheet-v1")
