"""One-time work at app start: create tables, upgrade the schema, seed subjects, make sure an admin exists."""
import os
from datetime import datetime

from app import auth, ingest, models
from app.practice import attempts
from app.database import Base, SessionLocal, engine, ensure_columns, upgrade_empty_attempt_tables
from app.web import logger


def bootstrap_admin():
    """Create the first admin from APP_USERNAME / APP_PASSWORD if none exists."""
    db = SessionLocal()
    try:
        if db.query(models.User).filter(models.User.is_admin.is_(True)).first():
            return
        password = os.environ.get("APP_PASSWORD")
        username = auth.normalize_username(os.environ.get("APP_USERNAME", auth.DEFAULT_ADMIN_USERNAME))
        if not password:
            logger.warning("No admin account exists and APP_PASSWORD is not set — nobody can log in.")
            return
        if not auth.USERNAME_RE.match(username):
            logger.warning("APP_USERNAME %r is not a valid username — admin not created.", username)
            return
        if db.query(models.User).filter(models.User.username == username).first():
            logger.warning("A non-admin user named %r already exists — admin not created.", username)
            return
        db.add(models.User(
            username=username,
            password_hash=auth.hash_password(password),
            is_admin=True,
            status=models.UserStatus.approved,
            decided_at=datetime.utcnow(),
        ))
        db.commit()
    finally:
        db.close()


def init():
    upgrade_empty_attempt_tables()      # must run before create_all, which then recreates them
    Base.metadata.create_all(bind=engine)
    ensure_columns()
    db = SessionLocal()
    try:
        models.seed_subjects(db)
    finally:
        db.close()
    bootstrap_admin()
    ingest.mark_interrupted_papers()
    db = SessionLocal()
    try:
        attempts.expire_overdue(db)      # tests whose time ran out while the app was off are finished and marked now
    finally:
        db.close()
