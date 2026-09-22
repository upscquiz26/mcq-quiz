"""
Revision: the mistake notebook, the spaced-repetition schedule, bookmarks and personal notes.

THE SCHEDULE
  * A wrong answer puts the question on the student's schedule, due the next day.
  * Wrong questions come back after 1, 3, 7 and then 15 days (15 days from then on). Each time one is
    reviewed, the next gap is the next step up that ladder.
  * A question LEAVES the schedule ("mastered") after two clean right answers in a row at reviews.
    A wrong answer at a review breaks the streak but the ladder keeps moving, so a question the student
    keeps struggling with reaches the 7- and 15-day gaps.
  * Only answers given when the question is DUE count as reviews. Extra practice before then changes
    nothing (it can't speed a question out of the schedule), except that a wrong answer still breaks the streak.
  * A right answer only counts as clean if the student was sure of it or didn't rate it. A lucky guess
    ("guessed" / "no idea") on a due question is treated like a miss: it shows they don't yet know it.
  * Missed reviews roll over: anything due on or before today is simply due now, and the next gap counts
    from the day it is finally reviewed.
  * Right answers never create schedule entries. A question the student got right but only guessed appears
    in the notebook (as "guessed") without being scheduled.
  * Any graded answer to the question counts, in any mode: topic practice, a test or mistake practice.
    Tests count when they are finished.

The date is the server's local date. Everything is per user.

WHAT A STUDENT MAY LOOK UP
  A question's own page (answer + explanation) is only opened to a student who has already finished with it
  (answered it in practice, or it was in a finished attempt) and never while it is in a test they are still
  sitting, so it can't be used to peek at answers mid-test. Bookmarks and notes need the student to have
  seen the question, so they can't be used to browse questions they haven't met.
"""
from datetime import date, datetime, timedelta, timezone

from app import models
from app.models import AttemptKind, AttemptStatus, Confidence, MistakeReason
from app.practice import pool

LADDER_DAYS = (1, 3, 7, 15)
PASS_STREAK = 2
MAX_NOTE_LENGTH = 2000
SHAKY = (Confidence.guessed, Confidence.no_idea)

ACTIVE, DONE = "active", "done"


def today() -> date:
    return date.today()


def local_date(moment: datetime) -> date:
    """A stored UTC timestamp as a local calendar date."""
    return moment.replace(tzinfo=timezone.utc).astimezone().date()


# --------------------------------------------------------------------------- the schedule

def _step(item: models.RevisionItem, on_date: date) -> None:
    """One more review has happened: move up the ladder and set the next due date."""
    item.stage += 1
    item.due_date = on_date + timedelta(days=LADDER_DAYS[min(item.stage, len(LADDER_DAYS) - 1)])


def record_answer(db, user_id: int, question_id: int, *, correct: bool, confidence, on_date: date) -> str:
    """Updates the schedule for one graded answer. Returns what happened, for tests and logs:
    'created', 'reset', 'reviewed', 'mastered', 'streak_broken' or 'ignored'."""
    item = (db.query(models.RevisionItem)
            .filter(models.RevisionItem.user_id == user_id, models.RevisionItem.question_id == question_id).first())
    now = datetime.utcnow()

    if not correct:
        if item is None:
            db.add(models.RevisionItem(
                user_id=user_id, question_id=question_id, stage=0, correct_streak=0, status=ACTIVE,
                due_date=on_date + timedelta(days=LADDER_DAYS[0]), last_result="wrong"))
            return "created"
        if item.status == DONE:                       # forgotten again: start over
            item.stage, item.correct_streak, item.status, item.done_at = 0, 0, ACTIVE, None
            item.due_date = on_date + timedelta(days=LADDER_DAYS[0])
            item.last_result, item.updated_at = "wrong", now
            return "reset"
        item.correct_streak = 0
        item.last_result, item.updated_at = "wrong", now
        if item.due_date is not None and item.due_date <= on_date:
            _step(item, on_date)                      # it was due: this counts as a (failed) review
            return "reviewed"
        return "streak_broken"                        # extra practice: streak broken, schedule unchanged

    # A right answer.
    if item is None or item.status == DONE:
        return "ignored"
    if item.due_date is not None and item.due_date > on_date:
        return "ignored"                              # not due yet: extra practice changes nothing
    clean = confidence not in SHAKY
    item.updated_at = now
    if not clean:
        item.correct_streak, item.last_result = 0, "shaky"
        _step(item, on_date)
        return "reviewed"
    item.correct_streak += 1
    item.last_result = "right"
    if item.correct_streak >= PASS_STREAK:
        item.status, item.done_at = DONE, now
        return "mastered"
    _step(item, on_date)
    return "reviewed"


def due_count(db, user_id: int, on_date: date | None = None) -> int:
    """How many live questions are due for revision now."""
    on_date = on_date or today()
    return (
        pool.live_questions(db)
        .join(models.RevisionItem, models.RevisionItem.question_id == models.Question.id)
        .filter(models.RevisionItem.user_id == user_id, models.RevisionItem.status == ACTIVE,
                models.RevisionItem.due_date <= on_date)
        .count()
    )


# --------------------------------------------------------------------------- the mistake notebook

STATE_ORDER = {"due": 0, "scheduled": 1, "guessed": 2, "mastered": 3}
STATE_LABELS = {"due": "Due now", "scheduled": "Scheduled", "guessed": "Guessed", "mastered": "Mastered"}
STATE_FILTERS = {"all": "All", "revise": "To revise", "due": "Due now", "guessed": "Guessed only", "mastered": "Mastered"}


def notebook(db, user_id: int, *, subject_id: int | None = None, topic_id: int | None = None,
             reason: str | None = None, state: str = "all", on_date: date | None = None) -> list[dict]:
    """Every live question this student got wrong, or got right but only guessed — one entry per question."""
    on_date = on_date or today()
    rows = (
        pool.live_questions(db)
        .join(models.Response, models.Response.question_id == models.Question.id)
        .join(models.Attempt, models.Attempt.id == models.Response.attempt_id)
        .filter(models.Attempt.user_id == user_id, models.Response.selected_answer.isnot(None),
                models.Response.is_correct.isnot(None))
        .with_entities(models.Question.id, models.Response.id, models.Response.is_correct,
                       models.Response.confidence, models.Response.mistake_reason, models.Attempt.started_at)
        .order_by(models.Response.id)
        .all()
    )
    stats: dict = {}
    for question_id, _response_id, correct, confidence, mistake_reason, started_at in rows:
        s = stats.setdefault(question_id, {"wrong": 0, "guessed_right": 0, "reason": None, "last": None})
        if not correct:
            s["wrong"] += 1
            s["reason"] = mistake_reason                       # rows are in answer order, so this ends as the latest
        elif confidence in SHAKY:
            s["guessed_right"] += 1
        s["last"] = started_at
    stats = {qid: s for qid, s in stats.items() if s["wrong"] or s["guessed_right"]}
    if not stats:
        return []

    ids = list(stats)
    questions = {q.id: q for q in db.query(models.Question).filter(models.Question.id.in_(ids)).all()}
    items = {i.question_id: i for i in db.query(models.RevisionItem)
             .filter(models.RevisionItem.user_id == user_id, models.RevisionItem.question_id.in_(ids)).all()}
    bookmarked = {b.question_id for b in db.query(models.QuestionBookmark.question_id)
                  .filter(models.QuestionBookmark.user_id == user_id, models.QuestionBookmark.question_id.in_(ids)).all()}
    noted = {n.question_id for n in db.query(models.QuestionNote.question_id)
             .filter(models.QuestionNote.user_id == user_id, models.QuestionNote.question_id.in_(ids)).all()}

    entries = []
    for question_id, s in stats.items():
        q, item = questions[question_id], items.get(question_id)
        if item is not None and item.status == DONE:
            entry_state, due, days = "mastered", None, None
        elif item is not None and item.due_date is not None:
            due = item.due_date
            days = (due - on_date).days
            entry_state = "due" if days <= 0 else "scheduled"
        else:
            entry_state, due, days = "guessed", None, None
        reason_name = s["reason"].name if s["reason"] not in (None, MistakeReason.unset) else None
        entries.append({
            "question": q, "wrong": s["wrong"], "guessed_right": s["guessed_right"],
            "reason": s["reason"] if s["wrong"] else None, "reason_name": reason_name,
            "state": entry_state, "due_date": due, "days_until": days,
            "bookmarked": question_id in bookmarked, "has_note": question_id in noted, "last_at": s["last"],
        })

    entries.sort(key=lambda e: (STATE_ORDER[e["state"]], e["due_date"] or date.max, e["question"].id))
    return filter_entries(entries, subject_id=subject_id, topic_id=topic_id, reason=reason, state=state)


def filter_entries(entries: list[dict], *, subject_id=None, topic_id=None, reason=None, state="all") -> list[dict]:
    """Narrows notebook entries (already sorted) by subject, topic, mistake reason and state."""
    if subject_id is not None:
        entries = [e for e in entries if e["question"].subject_id == subject_id]
    if topic_id is not None:
        entries = [e for e in entries if e["question"].topic_id == topic_id]
    if reason:
        entries = [e for e in entries if e["reason_name"] == reason]
    if state == "revise":
        entries = [e for e in entries if e["state"] in ("due", "scheduled")]
    elif state in ("due", "guessed", "mastered"):
        entries = [e for e in entries if e["state"] == state]
    return entries


def mistake_practice_ids(db, user_id: int, *, mode: str, subject_id=None, topic_id=None, reason=None,
                         include_mastered: bool = False, on_date: date | None = None) -> list[int]:
    """The questions a mistake-practice session may draw from: 'due' = only what's due now (most overdue first);
    anything else = all the student's mistakes and guesses."""
    entries = notebook(db, user_id, subject_id=subject_id, topic_id=topic_id, reason=reason, on_date=on_date)
    if mode == "due":
        entries = [e for e in entries if e["state"] == "due"]
    elif not include_mastered:
        entries = [e for e in entries if e["state"] != "mastered"]
    return [e["question"].id for e in entries]


# --------------------------------------------------------------------------- who may look at what

def _mine(db, user_id: int):
    return (db.query(models.Response.id)
            .join(models.Attempt, models.Attempt.id == models.Response.attempt_id)
            .filter(models.Attempt.user_id == user_id))


def has_seen(db, user_id: int, question_id: int) -> bool:
    """The student has met this question: opened it in an attempt, or answered it."""
    return _mine(db, user_id).filter(
        models.Response.question_id == question_id,
        (models.Response.visited.is_(True)) | (models.Response.selected_answer.isnot(None)),
    ).first() is not None


def locked_by_running_test(db, user_id: int, question_id: int) -> bool:
    """The question is in a timed test the student is still sitting."""
    return _mine(db, user_id).filter(
        models.Response.question_id == question_id,
        models.Attempt.status == AttemptStatus.IN_PROGRESS, models.Attempt.kind.in_(AttemptKind.TIMED),
    ).first() is not None


def can_reveal(db, user_id: int, question_id: int) -> bool:
    """May this student see the answer and explanation on the question's own page?"""
    if locked_by_running_test(db, user_id, question_id):
        return False
    return _mine(db, user_id).filter(
        models.Response.question_id == question_id,
        (models.Attempt.status != AttemptStatus.IN_PROGRESS) |
        ((~models.Attempt.kind.in_(AttemptKind.TIMED)) & models.Response.selected_answer.isnot(None)),
    ).first() is not None


# --------------------------------------------------------------------------- bookmarks and notes

class NoteRejected(Exception):
    """The note can't be saved; str(e) says why."""


def is_bookmarked(db, user_id: int, question_id: int) -> bool:
    return db.query(models.QuestionBookmark.id).filter_by(user_id=user_id, question_id=question_id).first() is not None


def set_bookmark(db, user_id: int, question_id: int, on: bool) -> None:
    existing = db.query(models.QuestionBookmark).filter_by(user_id=user_id, question_id=question_id).first()
    if on and existing is None:
        db.add(models.QuestionBookmark(user_id=user_id, question_id=question_id))
    elif not on and existing is not None:
        db.delete(existing)


def get_note(db, user_id: int, question_id: int) -> str:
    row = db.query(models.QuestionNote).filter_by(user_id=user_id, question_id=question_id).first()
    return row.text if row else ""


def save_note(db, user_id: int, question_id: int, text: str) -> None:
    """Saves the student's note. An empty note deletes it."""
    text = (text or "").replace("\r\n", "\n").strip()
    if len(text) > MAX_NOTE_LENGTH:
        raise NoteRejected(f"Keep the note under {MAX_NOTE_LENGTH} characters.")
    row = db.query(models.QuestionNote).filter_by(user_id=user_id, question_id=question_id).first()
    if not text:
        if row is not None:
            db.delete(row)
        return
    if row is None:
        db.add(models.QuestionNote(user_id=user_id, question_id=question_id, text=text))
    else:
        row.text, row.updated_at = text, datetime.utcnow()
