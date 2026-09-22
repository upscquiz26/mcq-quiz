"""
Leaderboards: who did best on the same test, and who did best this week.

Fairness rules (each is enforced here, not in the templates):
  * Only a user's FIRST started attempt of a test counts (`Attempt.counts_for_rank`, set when the attempt is created), and
    only finished timed tests do — so retaking, or abandoning and retaking, can never improve a ranked score.
  * Two people are ranked together only if they sat exactly the same questions (the rank key contains a fingerprint of them).
  * Custom timed tests and untimed practice have no rank key, so they never appear.
  * Order: higher score first; equal scores by less time taken; equal on both = the same rank (1, 2, 2, 4 ...).
  * Weekly: the AVERAGE PERCENTAGE of maximum marks over the user's counted full-length tests finished in that week
    (Monday 00:00 UTC to the next Monday); ties by the lower average time per test.
  * Only approved, non-admin students take part. Overdue tests are finished first, so a lapsed test is ranked on what was saved.

Privacy: a board shows the top TOP_N and the viewer's own row — never the rows below. A user who opted out shows to everyone
else as "Anonymous" but still sees their own rank. No user ids leave this module.
"""
from datetime import datetime, timedelta

from app import models
from app.models import AttemptKind, AttemptStatus
from app.practice import attempts as engine

TOP_N = 10
MIN_FOR_PERCENTILE = 5          # a percentile over fewer people than this would be meaningless


def _students(db) -> dict:
    return {u.id: u for u in db.query(models.User).filter(
        models.User.is_admin.is_(False), models.User.status == models.UserStatus.approved)}


def week_bounds(which: str = "this", now: datetime | None = None) -> tuple[datetime, datetime]:
    """[start, end) of this week or last week; weeks start on Monday 00:00 UTC."""
    now = now or datetime.utcnow()
    start = datetime(now.year, now.month, now.day) - timedelta(days=now.weekday())
    if which == "last":
        start -= timedelta(days=7)
    return start, start + timedelta(days=7)


def _name(user: models.User, viewer_id: int) -> str:
    if user.id == viewer_id or user.show_on_leaderboard:
        return user.display_name or user.username
    return "Anonymous"


def _ranked(rows: list[dict]) -> list[dict]:
    """rows: {user_id, value (higher is better), seconds (lower is better)}. Adds `rank`, best first; ties share a rank."""
    def key(r):
        return (-round(r["value"], 6), r["seconds"] if r["seconds"] is not None else float("inf"))
    rows = sorted(rows, key=key)
    for i, row in enumerate(rows):
        row["rank"] = 1 if i == 0 else (rows[i - 1]["rank"] if key(row) == key(rows[i - 1]) else i + 1)
    return rows


def _board(rows: list[dict], students: dict, viewer_id: int) -> dict:
    ranked = _ranked(rows)
    entries = []
    for r in ranked:
        entry = {**r, "name": _name(students[r["user_id"]], viewer_id), "is_you": r["user_id"] == viewer_id}
        entry.pop("user_id")
        entries.append(entry)
    mine = next((e for e in entries if e["is_you"]), None)
    top = entries[:TOP_N]                                   # the board is never longer than TOP_N rows
    percentile = None
    if mine and len(entries) >= MIN_FOR_PERCENTILE:
        others = len(entries) - 1
        behind = sum(1 for e in entries if e["rank"] > mine["rank"])
        percentile = round(100 * behind / others)
    return {
        "participants": len(entries), "top": top, "you": mine, "you_in_top": bool(mine and mine in top),
        "percentile": percentile,
    }


def test_board(db, rank_key: str, viewer_id: int, settle: bool = True) -> dict:
    """The board for one exact test (a paper's full-length test or one of its subject sections)."""
    if settle:
        engine.expire_overdue(db)
    students = _students(db)
    attempts = (
        db.query(models.Attempt)
        .filter(models.Attempt.rank_key == rank_key, models.Attempt.counts_for_rank.is_(True),
                models.Attempt.status != AttemptStatus.IN_PROGRESS, models.Attempt.score.isnot(None),
                models.Attempt.max_marks > 0, models.Attempt.user_id.in_(list(students) or [0]))
        .order_by(models.Attempt.id).all()
    )
    seen, rows = set(), []
    for a in attempts:
        if a.user_id in seen:
            continue                                        # can't happen (only a first attempt counts) but never list anyone twice
        seen.add(a.user_id)
        rows.append({"user_id": a.user_id, "value": a.score, "score": a.score, "max": a.max_marks,
                     "percent": round(100 * a.score / a.max_marks, 1), "seconds": a.time_taken_seconds})
    return _board(rows, students, viewer_id)


def weekly_board(db, viewer_id: int, which: str = "this", now: datetime | None = None) -> dict:
    engine.expire_overdue(db)
    start, end = week_bounds(which, now)
    students = _students(db)
    attempts = (
        db.query(models.Attempt)
        .filter(models.Attempt.kind == AttemptKind.FULL, models.Attempt.counts_for_rank.is_(True),
                models.Attempt.status != AttemptStatus.IN_PROGRESS, models.Attempt.score.isnot(None),
                models.Attempt.max_marks > 0, models.Attempt.completed_at >= start, models.Attempt.completed_at < end,
                models.Attempt.user_id.in_(list(students) or [0]))
        .all()
    )
    per_user: dict = {}
    for a in attempts:
        per_user.setdefault(a.user_id, []).append(a)
    rows = []
    for uid, items in per_user.items():
        percents = [100 * a.score / a.max_marks for a in items]
        times = [a.time_taken_seconds for a in items if a.time_taken_seconds is not None]
        average = sum(percents) / len(percents)
        rows.append({"user_id": uid, "value": average, "percent": round(average, 1), "tests": len(items),
                     "seconds": round(sum(times) / len(times)) if times else None})
    board = _board(rows, students, viewer_id)
    board.update(start=start, end=end - timedelta(days=1), which=which)
    return board


def rank_of_attempt(db, attempt: models.Attempt) -> dict | None:
    """The owner's own row on the board for this attempt's test, if this attempt is the one that counts."""
    if not attempt.rank_key or not attempt.counts_for_rank or attempt.status == AttemptStatus.IN_PROGRESS:
        return None
    board = test_board(db, attempt.rank_key, attempt.user_id)
    return {"you": board["you"], "participants": board["participants"]} if board["you"] else None
