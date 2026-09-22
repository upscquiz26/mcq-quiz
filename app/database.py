"""
Database connection setup.

Everything lives in one local SQLite file: data/upsc_pyq.db
That file IS your entire question bank + attempt history + mistake log.
It is backed up automatically before every bulk import (data/backups/), and
the Backups page can make and download a copy at any time.

Set UPSC_DATA_DIR to keep all data somewhere else (the automated tests do this
so they never touch your real data).
"""
import os
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker, declarative_base

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("UPSC_DATA_DIR") or os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "upsc_pyq.db")
os.makedirs(DATA_DIR, exist_ok=True)

engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# create_all() only creates missing tables, it never adds columns to existing ones.
# Columns added after a database was first created are listed here and get
# ALTER TABLE'd in on startup, so upgrading never needs a manual migration.
COLUMN_MIGRATIONS = {
    "papers": {
        "answer_pdf_path": "VARCHAR",
        "status": "VARCHAR DEFAULT 'ready'",
        "status_message": "TEXT",
        "pages_done": "INTEGER DEFAULT 0",
        "pages_total": "INTEGER DEFAULT 0",
        "source_type": "VARCHAR",
        "source_name": "VARCHAR",
        "test_name": "VARCHAR",
        "test_number": "VARCHAR",
        "series": "VARCHAR",
        "layout": "VARCHAR",
        "expected_total": "INTEGER",
        "duration_minutes": "INTEGER",
        "marks_per_question": "FLOAT",
        "negative_fraction": "FLOAT",
        "key_source": "VARCHAR",
        "key_version": "VARCHAR",
        "file_hash": "VARCHAR",
        "answer_file_hash": "VARCHAR",
        "publish_status": "VARCHAR DEFAULT 'draft'",
        "published_at": "DATETIME",
        "published_by": "INTEGER",
        "archived_at": "DATETIME",
        "audit_state": "VARCHAR DEFAULT 'none'",
        "audit_round": "INTEGER DEFAULT 0",
    },
    "questions": {
        "explanation": "TEXT",
        "source_image_path": "VARCHAR",
        "ocr_flags": "VARCHAR",
        "status": "VARCHAR NOT NULL DEFAULT 'needs_review'",
        "source": "VARCHAR",
        "page_number": "INTEGER",
        "extraction_method": "VARCHAR",
        "ocr_quality": "FLOAT",
        "norm_hash": "VARCHAR",
        "answer_source": "VARCHAR",
        "explanation_status": "VARCHAR",
        "explanation_says": "VARCHAR",
        "uncertain": "BOOLEAN DEFAULT 0",
        "suggested_subject_id": "INTEGER",
        "quarantine_reason": "TEXT",
        "quarantined_at": "DATETIME",
        "reviewed_by": "INTEGER",
        "reviewed_at": "DATETIME",
        "flags_acknowledged": "BOOLEAN DEFAULT 0",
        "audit_pick": "BOOLEAN DEFAULT 0",
        "audit_result": "VARCHAR",
        "question_hi": "TEXT",
        "option_a_hi": "TEXT",
        "option_b_hi": "TEXT",
        "option_c_hi": "TEXT",
        "option_d_hi": "TEXT",
        "explanation_hi": "TEXT",
        "explanation_hi_status": "VARCHAR",
    },
    "question_reports": {
        "language": "VARCHAR",
    },
    "subjects": {
        "is_active": "BOOLEAN DEFAULT 1",
    },
    "users": {
        "display_name": "VARCHAR",
        "show_on_leaderboard": "BOOLEAN NOT NULL DEFAULT 1",
        "must_change_password": "BOOLEAN NOT NULL DEFAULT 0",
        "password_changed_at": "DATETIME",
        "session_version": "INTEGER NOT NULL DEFAULT 0",
        "last_active_at": "DATETIME",
        "daily_target": "INTEGER",
        "language": "VARCHAR NOT NULL DEFAULT 'en'",
    },
}


def upgrade_empty_attempt_tables() -> bool:
    """Rebuild `attempts` and `responses` in their student-side shape. Returns True if it did.

    SQLite can't make a NOT NULL column nullable in place (attempts.paper_id must be nullable, because a
    practice session can mix questions from several papers), and the old tables had no user_id. Nothing
    ever wrote to them, so they are dropped and recreated by create_all(). If they somehow contain rows,
    this refuses rather than lose them.
    """
    inspector = inspect(engine)
    if not inspector.has_table("attempts"):
        return False
    if "user_id" in {c["name"] for c in inspector.get_columns("attempts")}:
        return False                                    # already the new shape
    with engine.begin() as conn:
        attempts = conn.execute(text("SELECT COUNT(*) FROM attempts")).scalar()
        responses = conn.execute(text("SELECT COUNT(*) FROM responses")).scalar() if inspector.has_table("responses") else 0
        if attempts or responses:
            raise RuntimeError(
                f"The old attempts/responses tables hold data ({attempts} attempts, {responses} responses) and cannot "
                "be upgraded automatically. Restore a backup from data/backups/ or contact the developer."
            )
        conn.execute(text("DROP TABLE IF EXISTS responses"))
        conn.execute(text("DROP TABLE attempts"))
    return True


def ensure_columns() -> list[tuple[str, str]]:
    """ALTER TABLE in any missing columns. Returns the (table, column) pairs that were added."""
    inspector = inspect(engine)
    added = []
    with engine.begin() as conn:
        for table, columns in COLUMN_MIGRATIONS.items():
            if not inspector.has_table(table):
                continue  # brand-new database: create_all already made it with every column
            existing = {c["name"] for c in inspector.get_columns(table)}
            for name, ddl in columns.items():
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
                    added.append((table, name))
        # One-off data fix when `status` first appears on an existing database: questions an
        # admin had already confirmed (needs_review = 0) are `verified`, the rest `needs_review`.
        if ("questions", "status") in added:
            conn.execute(text("UPDATE questions SET status = 'verified' WHERE needs_review = 0"))
    return added


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
