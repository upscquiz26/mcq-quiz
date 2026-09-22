"""
Today's progress against the student's daily target, and their practice streak.

Definitions
  * an answer counts on the (server-local) day it was last given: every answered question in practice, in mistake practice and in
    tests counts once, including answers saved in a test that is still running;
  * the daily target is a number of answered questions per day (optional; set in the profile);
  * a streak is the number of days in a row, ending today, on which the student answered at least ONE question. If nothing has
    been answered yet today the streak still counts up to yesterday — it only breaks when a whole day passes with no answer.
    The streak does not depend on the target, so changing the target never rewrites history;
  * "best" is the longest such run within the last WINDOW_DAYS days.
"""
from datetime import date, datetime, timedelta, timezone, tzinfo

from app import models

WINDOW_DAYS = 400
MAX_TARGET = 500


def _local_day(moment: datetime, tz: tzinfo | None) -> date:
    """`moment` is a naive UTC time (how the app stores times); the day it falls on in the given (default: server) timezone."""
    return moment.replace(tzinfo=timezone.utc).astimezone(tz).date()


def answers_by_day(db, user_id: int, today: date | None = None, tz: tzinfo | None = None) -> dict[date, int]:
    today = today or date.today()
    since = datetime.utcnow() - timedelta(days=WINDOW_DAYS + 2)
    rows = (
        db.query(models.Response.answered_at)
        .join(models.Attempt, models.Attempt.id == models.Response.attempt_id)
        .filter(models.Attempt.user_id == user_id, models.Response.selected_answer.isnot(None),
                models.Response.answered_at.isnot(None), models.Response.answered_at >= since)
        .all()
    )
    days: dict[date, int] = {}
    for (moment,) in rows:
        day = _local_day(moment, tz)
        days[day] = days.get(day, 0) + 1
    return days


def streaks(days: dict[date, int], today: date) -> tuple[int, int]:
    """(current, best) run of consecutive active days."""
    active = {d for d, n in days.items() if n > 0 and d <= today}
    current, cursor = 0, today if today in active else today - timedelta(days=1)
    while cursor in active:
        current += 1
        cursor -= timedelta(days=1)
    best = run = 0
    previous = None
    for d in sorted(active):
        run = run + 1 if previous is not None and d - previous == timedelta(days=1) else 1
        best, previous = max(best, run), d
    return current, best


def summary(db, user: models.User, today: date | None = None, tz: tzinfo | None = None) -> dict:
    today = today or date.today()
    days = answers_by_day(db, user.id, today, tz)
    answered_today = days.get(today, 0)
    current, best = streaks(days, today)
    target = user.daily_target or None
    return {
        "today": answered_today, "target": target, "streak": current, "best": best,
        "percent": min(100, round(100 * answered_today / target)) if target else None,
        "remaining": max(0, target - answered_today) if target else None,
        "met": bool(target) and answered_today >= target,
        "active_today": answered_today > 0,
    }


def parse_target(text: str) -> int | None:
    """The daily-target field: blank clears it; otherwise a whole number from 1 to MAX_TARGET. Raises ValueError with a message."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        value = int(text)
    except ValueError:
        raise ValueError(f"The daily target must be a whole number of questions, from 1 to {MAX_TARGET}.")
    if not 1 <= value <= MAX_TARGET:
        raise ValueError(f"The daily target must be a whole number of questions, from 1 to {MAX_TARGET}.")
    return value
