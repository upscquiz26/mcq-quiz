"""
Temporary lockout after repeated login failures.

  * 5 failures for the same username within 15 minutes  -> that username is locked for 15 minutes
  * 20 failures from the same IP within 15 minutes      -> that IP is locked for 15 minutes

The lock is measured from the most recent failure. While locked, the password is
not even checked, so a lockout can't be used to test guesses, and further
attempts don't extend it. The check is keyed on what was TYPED, so an unknown
username locks exactly like a real one — nothing here reveals which accounts exist.

The username is stored only as a short hash (people sometimes type a password
into the username box). The same mechanism guards the "current password" box on
the change-password form, under the key "pw:<user id>".
"""
import hashlib
import math
from datetime import datetime, timedelta

from sqlalchemy import func

from app import models

WINDOW = timedelta(minutes=15)
LOCKOUT = timedelta(minutes=15)
MAX_PER_USERNAME = 5
MAX_PER_IP = 20


def key_for_username(username: str) -> str:
    return hashlib.sha256((username or "").strip().lower().encode()).hexdigest()[:16]


def key_for_password_check(user_id: int) -> str:
    return f"pw:{user_id}"


def _locked_for(db, column, value, limit: int, now: datetime) -> int:
    """Seconds left on a lock for this key/ip, or 0.

    Locked means: the most recent failure was the `limit`-th within one WINDOW, and LOCKOUT has not
    yet passed since it. Measured from that latest failure, so the wait is exactly what we tell the user.
    """
    if value is None:
        return 0
    last = db.query(func.max(models.LoginFailure.at)).filter(column == value).scalar()
    if last is None:
        return 0
    burst = (
        db.query(func.count(models.LoginFailure.id))
        .filter(column == value, models.LoginFailure.at >= last - WINDOW, models.LoginFailure.at <= last)
        .scalar()
    )
    if burst < limit:
        return 0
    remaining = (last + LOCKOUT - now).total_seconds()
    return math.ceil(remaining) if remaining > 0 else 0


def seconds_locked(db, key: str, ip: str | None = None, now: datetime | None = None) -> int:
    """How long, in seconds, this key/IP must wait before trying again (0 = not locked)."""
    now = now or datetime.utcnow()
    return max(
        _locked_for(db, models.LoginFailure.key, key, MAX_PER_USERNAME, now),
        _locked_for(db, models.LoginFailure.ip, ip, MAX_PER_IP, now),
    )


def record_failure(db, key: str, ip: str | None = None) -> None:
    now = datetime.utcnow()
    db.add(models.LoginFailure(key=key, ip=ip, at=now))
    # Tidy old rows while we're here; nothing older than a day matters.
    db.query(models.LoginFailure).filter(models.LoginFailure.at < now - timedelta(days=1)).delete()


def clear(db, key: str) -> None:
    """Forget a key's failures (after a successful login, or when an admin resets that account)."""
    db.query(models.LoginFailure).filter(models.LoginFailure.key == key).delete()


def describe_wait(seconds: int) -> str:
    minutes = max(1, -(-seconds // 60))          # round up
    return f"{minutes} minute{'s' if minutes != 1 else ''}"
