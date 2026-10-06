"""
Practice sessions, timed tests and their answers.

Rules that hold for everything here:
  * An Attempt belongs to one user and is never overwritten; practising again starts a new one.
  * Only questions from live_questions() are ever put in an attempt, and each is re-checked whenever it
    is shown, so unpublishing a paper mid-session takes its questions away at once.
  * The answer key never leaves the server until the answer is locked (practice) or the attempt is
    finished (tests).
  * Marks are copied onto each Response when the attempt starts, so editing a paper later can't change
    an old result.

Timed tests (sectional and full-length) add:
  * The clock is the SERVER's. `deadline_at` is fixed when the test starts; refreshing, closing the
    browser or changing devices never resets or pauses it.
  * Answers are saved as the student goes and can be changed until the end. Nothing is marked until the
    test is finished, so nothing about correctness is sent to the browser during the test.
  * When time is up the test is finished automatically with whatever was saved (status `expired`). This
    happens the moment anyone next touches the attempt, and on app start, so an abandoned test is still
    graded — it can't be left open to dodge a bad score. Saves are accepted for GRACE_SECONDS past the
    deadline to allow for network delay.
  * Only whole-paper and whole-paper-subject sittings get a `rank_key`; the user's FIRST started attempt
    for a key is the one that counts for ranking, even if it ran out of time.
"""
import hashlib
import json
import math
import random
from datetime import datetime, timedelta

from fastapi import HTTPException

from app import models
from app.models import AttemptKind, AttemptStatus, Confidence
from app.practice import grading, pool, revision

DEFAULT_MARKS = 2.0             # used when a paper has no marking scheme set
DEFAULT_NEGATIVE = 1 / 3
MAX_SESSION_QUESTIONS = 100
TIME_CAP_SECONDS = 300          # one screen view never counts for more than 5 minutes (walk-away protection)

TIMED_KINDS = AttemptKind.TIMED
SECONDS_PER_QUESTION = 72       # 1.2 minutes each: the UPSC pace of 120 minutes for 100 questions
GRACE_SECONDS = 3

CONFIDENCE_CHOICES = {
    "sure": Confidence.sure,
    "guessed": Confidence.guessed,
    "no_idea": Confidence.no_idea,
}
CONFIDENCE_LABELS = {"sure": "Sure", "guessed": "Guessed", "no_idea": "No idea"}


class StartRefused(Exception):
    """A test can't be started; str(e) says why, in words for the student."""


class AnswerRejected(Exception):
    """The answer can't be accepted; str(e) says why."""


def scheme_for(paper: models.Paper) -> tuple[float, float]:
    """(marks per question, fraction of those marks lost for a wrong answer)."""
    marks = paper.marks_per_question or DEFAULT_MARKS
    negative = paper.negative_fraction if paper.negative_fraction is not None else DEFAULT_NEGATIVE
    return marks, negative


def is_timed(attempt: models.Attempt) -> bool:
    return attempt.kind in TIMED_KINDS


def is_answered(response: models.Response) -> bool:
    return response.selected_answer is not None


def time_left_label(attempt: models.Attempt) -> str:
    """' · 12 min left' for a running timed test, '' for anything else (used on the resume lists)."""
    seconds = remaining_seconds(attempt)
    if seconds is None or attempt.status != AttemptStatus.IN_PROGRESS:
        return ""
    return f" · {max(1, -(-seconds // 60))} min left"


# --------------------------------------------------------------------------- ownership and the clock

def remaining_seconds(attempt: models.Attempt, now: datetime | None = None) -> int | None:
    """Whole seconds left on a timed test (0 once time is up). None for untimed sessions."""
    if not is_timed(attempt) or attempt.deadline_at is None:
        return None
    now = now or datetime.utcnow()
    return max(0, math.ceil((attempt.deadline_at - now).total_seconds()))


def _overdue(attempt: models.Attempt, now: datetime) -> bool:
    return (
        is_timed(attempt)
        and attempt.status == AttemptStatus.IN_PROGRESS
        and attempt.deadline_at is not None
        and now > attempt.deadline_at + timedelta(seconds=GRACE_SECONDS)
    )


def settle(db, attempt: models.Attempt, now: datetime | None = None) -> bool:
    """If the attempt's time ran out, finish it now. Returns True if it did."""
    now = now or datetime.utcnow()
    if not _overdue(attempt, now):
        return False
    finish_attempt(db, attempt, now=now)
    db.commit()
    return True


def expire_overdue(db, user_id: int | None = None) -> int:
    """Finish every timed attempt (optionally just one user's) whose time has run out."""
    now = datetime.utcnow()
    query = db.query(models.Attempt).filter(
        models.Attempt.status == AttemptStatus.IN_PROGRESS,
        models.Attempt.kind.in_(TIMED_KINDS),
        models.Attempt.deadline_at.isnot(None),
        models.Attempt.deadline_at < now - timedelta(seconds=GRACE_SECONDS),
    )
    if user_id is not None:
        query = query.filter(models.Attempt.user_id == user_id)
    overdue = query.all()
    for attempt in overdue:
        finish_attempt(db, attempt, now=now)
    if overdue:
        db.commit()
    return len(overdue)


def own_attempt(db, user, attempt_id: int) -> models.Attempt:
    """The attempt, only if it belongs to `user` — and finished first if its time has run out.

    Someone else's attempt is a 404, not a 403, so that changing the id in the URL reveals nothing,
    not even that the attempt exists."""
    attempt = db.get(models.Attempt, attempt_id)
    if attempt is None or attempt.user_id != user.id:
        raise HTTPException(status_code=404, detail="Not found")
    settle(db, attempt)
    return attempt


def response_at(attempt: models.Attempt, position: int) -> models.Response:
    for response in attempt.responses:
        if response.position == position:
            return response
    raise HTTPException(status_code=404, detail="Not found")


# --------------------------------------------------------------------------- starting

def _new_attempt(db, user, *, kind, ordered_ids, filters=None, paper=None, subject_id=None,
                 duration_seconds=None, rank_key=None, marking_scheme=None) -> tuple[models.Attempt, bool]:
    """Creates an attempt over `ordered_ids` (in that order). Returns (attempt, resumed) — resumed is True
    when the user already had this ranked test open, and that one is returned instead of a duplicate."""
    # A test whose clock has run out is only finished when somebody next looks at it. Do that first, so an
    # expired first attempt is seen for what it is (used up, not "open") instead of being resumed.
    expire_overdue(db, user.id)
    if rank_key:
        open_one = (
            db.query(models.Attempt)
            .filter(models.Attempt.user_id == user.id, models.Attempt.rank_key == rank_key,
                    models.Attempt.status == AttemptStatus.IN_PROGRESS)
            .first()
        )
        if open_one is not None:
            return open_one, True

    questions = {q.id: q for q in db.query(models.Question).filter(models.Question.id.in_(ordered_ids)).all()}
    paper_ids = {questions[i].paper_id for i in ordered_ids}
    started = datetime.utcnow()
    counts = False
    if rank_key:      # only the user's very first attempt at this exact test can ever be ranked
        counts = db.query(models.Attempt.id).filter(
            models.Attempt.user_id == user.id, models.Attempt.rank_key == rank_key).first() is None

    attempt = models.Attempt(
        user_id=user.id, kind=kind, status=AttemptStatus.IN_PROGRESS, started_at=started,
        paper_id=paper.id if paper is not None else (paper_ids.pop() if len(paper_ids) == 1 else None),
        subject_id=subject_id, filters_json=json.dumps(filters) if filters is not None else None,
        total_questions=len(ordered_ids), rank_key=rank_key, counts_for_rank=counts,
        mode=models.ExamType.full_length if kind == AttemptKind.FULL else models.ExamType.sectional if kind == AttemptKind.SECTIONAL else None,
        timer_strict=kind == AttemptKind.FULL,
        negative_marking=marking_scheme[1] > 0 if marking_scheme is not None else True,
    )
    if duration_seconds:
        attempt.time_limit_minutes = math.ceil(duration_seconds / 60)
        attempt.deadline_at = started + timedelta(seconds=duration_seconds)
    db.add(attempt)
    db.flush()
    for position, question_id in enumerate(ordered_ids, start=1):
        marks, negative = marking_scheme or scheme_for(questions[question_id].paper)
        db.add(models.Response(
            attempt_id=attempt.id, question_id=question_id, position=position,
            marks_if_correct=marks, penalty_if_wrong=marks * negative,
        ))
    db.flush()
    db.refresh(attempt)
    return attempt, False


def create_practice_attempt(db, user, filters: pool.Filters, count: int) -> models.Attempt | None:
    """Starts an untimed topic-practice attempt with a random selection of matching questions.
    Returns None if nothing matches the filters."""
    ids = [row[0] for row in pool.filtered_questions(db, user.id, filters)
           .with_entities(models.Question.id).all()]
    if not ids:
        return None
    count = max(1, min(count, MAX_SESSION_QUESTIONS, len(ids)))
    attempt, _ = _new_attempt(db, user, kind=AttemptKind.TOPIC, ordered_ids=random.sample(ids, count),
                              filters=filters.as_dict(), subject_id=filters.subject_id)
    return attempt


def create_weak_areas_attempt(db, user, area_kind: str, area_ids: list[int], count: int) -> models.Attempt | None:
    """Untimed practice drawn at random from the student's weak topics (or weak subjects, if topics aren't set).
    Returns None if those areas have no live questions."""
    query = pool.live_questions(db)
    if area_kind == "topics":
        query = query.filter(models.Question.topic_id.in_(area_ids))
    elif area_kind == "subjects":
        query = query.filter(models.Question.subject_id.in_(area_ids))
    else:
        return None
    ids = [row[0] for row in query.with_entities(models.Question.id).all()]
    if not ids:
        return None
    count = max(1, min(count, MAX_SESSION_QUESTIONS, len(ids)))
    attempt, _ = _new_attempt(db, user, kind=AttemptKind.TOPIC, ordered_ids=random.sample(ids, count),
                              filters={"weak_areas": area_kind, "ids": area_ids})
    return attempt


def create_mistake_attempt(db, user, ordered_ids: list[int], filters: dict) -> models.Attempt | None:
    """Untimed practice on questions the student got wrong or guessed, in the order given (due-first, or random)."""
    if not ordered_ids:
        return None
    attempt, _ = _new_attempt(db, user, kind=AttemptKind.MISTAKE, ordered_ids=ordered_ids[:MAX_SESSION_QUESTIONS],
                              filters=filters, subject_id=filters.get("subject_id"))
    return attempt


def question_set_fingerprint(ids) -> str:
    """Identifies an exact set of questions. Part of the rank key, so two people are only ever ranked
    against each other if they sat exactly the same questions (if the admin later changes which
    questions of a paper are live, a new board starts rather than mixing different tests)."""
    return hashlib.sha1(",".join(str(i) for i in sorted(ids)).encode()).hexdigest()[:8]


def paper_live_ids(db, paper_id: int, subject_id: int | None = None) -> list[int]:
    query = pool.live_questions(db).filter(models.Question.paper_id == paper_id)
    if subject_id is not None:
        query = query.filter(models.Question.subject_id == subject_id)
    rows = query.with_entities(models.Question.id).order_by(models.Question.question_number, models.Question.id).all()
    return [r[0] for r in rows]


def full_test_duration_minutes(paper: models.Paper, question_count: int) -> int:
    if paper.duration_minutes:
        return paper.duration_minutes
    return max(1, math.ceil(question_count * SECONDS_PER_QUESTION / 60))


def full_test_duration_seconds(paper: models.Paper, question_count: int) -> int:
    return full_test_duration_minutes(paper, question_count) * 60


def start_full_test(db, user, paper_id: int, *, duration_minutes: int | None = None,
                    marks_per_question: float | None = None, negative_fraction: float | None = None) -> tuple[models.Attempt, bool]:
    """A timed whole-paper sitting with student-selected settings; only the paper's standard settings rank."""
    paper = db.get(models.Paper, paper_id)
    ids = paper_live_ids(db, paper_id) if paper else []
    if not ids:
        raise StartRefused("That test isn't available.")
    expire_overdue(db, user.id)
    open_one = (db.query(models.Attempt)
                .filter_by(user_id=user.id, paper_id=paper.id, kind=AttemptKind.FULL,
                           status=AttemptStatus.IN_PROGRESS)
                .order_by(models.Attempt.id.desc()).first())
    if open_one is not None:
        return open_one, True

    if duration_minutes is not None and not 1 <= duration_minutes <= 600:
        raise StartRefused("Choose a time limit from 1 to 600 minutes.")
    paper_marks, paper_negative = scheme_for(paper)
    marks = paper_marks if marks_per_question is None else marks_per_question
    negative = paper_negative if negative_fraction is None else negative_fraction
    if not math.isfinite(marks) or marks <= 0:
        raise StartRefused("Marks per question must be a positive number.")
    if not math.isfinite(negative) or not 0 <= negative <= 1:
        raise StartRefused("Negative marking must be between 0 and 1 (for example, 1/3).")
    duration_seconds = (full_test_duration_seconds(paper, len(ids)) if duration_minutes is None
                        else duration_minutes * 60)
    standard = (
        paper.marks_per_question is not None
        and paper.negative_fraction is not None
        and marks == paper.marks_per_question
        and negative == paper.negative_fraction
        and duration_seconds == full_test_duration_seconds(paper, len(ids))
    )
    return _new_attempt(db, user, kind=AttemptKind.FULL, ordered_ids=ids, paper=paper,
                        duration_seconds=duration_seconds,
                        rank_key=f"paper:{paper.id}:full:{question_set_fingerprint(ids)}" if standard else None,
                        marking_scheme=(marks, negative))


def start_full_paper_practice(db, user, paper_id: int) -> tuple[models.Attempt, bool]:
    """An untimed, immediate-feedback full-paper practice with no negative penalty."""
    paper = db.get(models.Paper, paper_id)
    ids = paper_live_ids(db, paper_id) if paper else []
    if not ids:
        raise StartRefused("That paper isn't available.")
    open_one = (db.query(models.Attempt)
                .filter_by(user_id=user.id, paper_id=paper.id, kind=AttemptKind.PAPER_PRACTICE,
                           status=AttemptStatus.IN_PROGRESS)
                .order_by(models.Attempt.id.desc()).first())
    if open_one is not None:
        return open_one, True
    return _new_attempt(db, user, kind=AttemptKind.PAPER_PRACTICE, ordered_ids=ids, paper=paper,
                        marking_scheme=(1.0, 0.0))


def start_section_test(db, user, paper_id: int, subject_id: int) -> tuple[models.Attempt, bool]:
    """A timed sitting of one paper's whole subject section — the only kind of sectional test that is ranked."""
    paper, subject = db.get(models.Paper, paper_id), db.get(models.Subject, subject_id)
    ids = paper_live_ids(db, paper_id, subject_id) if paper and subject else []
    if not ids:
        raise StartRefused("That section isn't available.")
    return _new_attempt(db, user, kind=AttemptKind.SECTIONAL, ordered_ids=ids, paper=paper, subject_id=subject_id,
                        duration_seconds=len(ids) * SECONDS_PER_QUESTION,
                        rank_key=f"paper:{paper.id}:subject:{subject_id}:{question_set_fingerprint(ids)}")


def start_custom_test(db, user, filters: pool.Filters, count: int) -> tuple[models.Attempt, bool]:
    """A timed test over a random selection matching the filters. Never ranked (people wouldn't be
    comparing the same questions)."""
    if filters.subject_id is None:
        raise StartRefused("Choose a subject for a sectional test.")
    ids = [row[0] for row in pool.filtered_questions(db, user.id, filters)
           .filter(models.Paper.source_type != models.SourceType.BOOK)
           .with_entities(models.Question.id).all()]
    if not ids:
        raise StartRefused("No questions match those choices. Try loosening a filter.")
    count = max(1, min(count, MAX_SESSION_QUESTIONS, len(ids)))
    chosen = random.sample(ids, count)
    return _new_attempt(db, user, kind=AttemptKind.SECTIONAL, ordered_ids=chosen, filters=filters.as_dict(),
                        subject_id=filters.subject_id, duration_seconds=count * SECONDS_PER_QUESTION)


# --------------------------------------------------------------------------- time and answers

def _credit_time(db, attempt: models.Attempt, now: datetime) -> None:
    """Adds the time since the last event to the question that was on screen. In practice that only counts
    until the answer is locked; in a test answers can change, so the time on screen always counts."""
    if attempt.current_response_id is None or attempt.last_event_at is None:
        return
    on_screen = db.get(models.Response, attempt.current_response_id)
    if on_screen is None or (is_answered(on_screen) and not is_timed(attempt)):
        return
    elapsed = max(0, int((now - attempt.last_event_at).total_seconds()))
    on_screen.time_spent_seconds = (on_screen.time_spent_seconds or 0) + min(elapsed, TIME_CAP_SECONDS)


def record_view(db, attempt: models.Attempt, response: models.Response) -> None:
    """The student has opened this question. Time is measured on the server, not by the browser."""
    if attempt.status != AttemptStatus.IN_PROGRESS:
        return
    now = datetime.utcnow()
    _credit_time(db, attempt, now)
    response.visited = True
    attempt.current_response_id = response.id
    attempt.last_event_at = now


def _validated(answer: str | None, confidence: str | None) -> tuple[str | None, str | None]:
    letter = (answer or "").strip().upper() or None
    if letter is not None and letter not in pool.ANSWER_LETTERS:
        raise AnswerRejected("Choose one of the options.")
    confidence = (confidence or "").strip() or None
    if confidence is not None and confidence not in CONFIDENCE_CHOICES:
        raise AnswerRejected("Say how sure you are: sure, guessed or no idea.")
    return letter, confidence


def _check_offered(db, response: models.Response, letter: str | None) -> None:
    """E is only an answer on questions that have a fifth option (book questions)."""
    if letter == "E" and not (db.get(models.Question, response.question_id).option_e or "").strip():
        raise AnswerRejected("Choose one of the options.")


def submit_answer(db, attempt: models.Attempt, response: models.Response, answer: str, confidence: str):
    """Topic practice: grades one answer straight away and locks it."""
    if is_timed(attempt):
        raise AnswerRejected("This is a timed test — answers are saved as you go and marked at the end.")
    if attempt.status != AttemptStatus.IN_PROGRESS:
        raise AnswerRejected("This session has finished.")
    if is_answered(response):
        raise AnswerRejected("You've already answered this question.")
    letter, conf = _validated(answer, confidence)
    if letter is None:
        raise AnswerRejected("Choose one of the options.")
    if conf is None:
        raise AnswerRejected("Say how sure you are: sure, guessed or no idea.")
    _check_offered(db, response, letter)
    if not pool.can_view_question(db, response.question_id):
        raise AnswerRejected("This question is no longer available.")

    now = datetime.utcnow()
    _credit_time(db, attempt, now)
    question = db.get(models.Question, response.question_id)
    response.selected_answer = letter
    response.confidence = CONFIDENCE_CHOICES[conf]
    response.is_correct = letter == question.correct_answer
    response.marks_awarded = response.marks_if_correct if response.is_correct else -(response.penalty_if_wrong or 0.0)
    response.answered_at = now
    attempt.last_event_at = now
    revision.record_answer(db, attempt.user_id, response.question_id, correct=response.is_correct,
                           confidence=response.confidence, on_date=revision.today())
    return response


def save_test_answer(db, attempt: models.Attempt, response: models.Response, *, answer=None, confidence=None,
                     marked=None, clear=False) -> models.Response:
    """A timed test: stores whatever the student just did (pick an option, rate confidence, mark for review,
    clear). Nothing is marked here. Anything not supplied is left as it was."""
    if not is_timed(attempt):
        raise AnswerRejected("This isn't a timed test.")
    if attempt.status != AttemptStatus.IN_PROGRESS:
        raise AnswerRejected("This test has finished.")
    letter, conf = _validated(answer, confidence)
    _check_offered(db, response, letter)
    if clear:
        letter = conf = None       # "Clear response" wins over whatever options are still ticked in the form
    if (letter or conf or clear) and not pool.can_view_question(db, response.question_id):
        raise AnswerRejected("This question is no longer available.")

    now = datetime.utcnow()
    _credit_time(db, attempt, now)
    if clear:
        response.selected_answer = None
        response.confidence = Confidence.skipped
        response.answered_at = None
    if letter is not None:
        response.selected_answer = letter
        response.answered_at = now
    if conf is not None:
        response.confidence = CONFIDENCE_CHOICES[conf]
    if marked is not None:
        response.marked_for_review = bool(marked)
    attempt.current_response_id = response.id
    attempt.last_event_at = now
    return response


def _grade(db, attempt: models.Attempt) -> None:
    """Marks every answered response that hasn't been marked yet (all of them, in a timed test)."""
    for r in attempt.responses:
        if is_answered(r) and r.is_correct is None:
            question = db.get(models.Question, r.question_id)
            r.is_correct = r.selected_answer == question.correct_answer
            r.marks_awarded = r.marks_if_correct if r.is_correct else -(r.penalty_if_wrong or 0.0)
        elif not is_answered(r):
            r.is_correct = None
            r.marks_awarded = None            # skipped questions are never penalised


def finish_attempt(db, attempt: models.Attempt, status: str | None = None, now: datetime | None = None) -> models.Attempt:
    """Closes the attempt, marks it and totals it up. Safe to call twice.

    A timed test that is finished after its deadline is `expired` and counts as ending AT the deadline,
    whenever the server got round to it."""
    if attempt.status != AttemptStatus.IN_PROGRESS:
        return attempt
    now = now or datetime.utcnow()
    timed = is_timed(attempt)
    end = now
    if timed and attempt.deadline_at is not None and now >= attempt.deadline_at:
        end = attempt.deadline_at
        status = status or AttemptStatus.EXPIRED
    status = status or AttemptStatus.SUBMITTED

    _credit_time(db, attempt, end)
    _grade(db, attempt)
    grading.classify_attempt(attempt)           # suggest a mistake reason for every wrong answer
    answered = [r for r in attempt.responses if is_answered(r)]
    if timed:                                   # a test's answers reach the revision schedule when it is finished
        finished_on = revision.local_date(end)  # (practice answers already did, one by one)
        for r in answered:
            revision.record_answer(db, attempt.user_id, r.question_id, correct=bool(r.is_correct),
                                   confidence=r.confidence, on_date=finished_on)
    attempt.total_questions = len(attempt.responses)
    attempt.correct_count = sum(1 for r in answered if r.is_correct)
    attempt.wrong_count = sum(1 for r in answered if not r.is_correct)
    attempt.skipped_count = attempt.total_questions - len(answered)
    attempt.score = sum(r.marks_awarded or 0.0 for r in answered)
    attempt.max_marks = sum(r.marks_if_correct or 0.0 for r in attempt.responses)
    attempt.time_taken_seconds = (
        max(0, int((end - attempt.started_at).total_seconds())) if timed
        else sum(r.time_spent_seconds or 0 for r in attempt.responses)
    )
    attempt.status = status
    attempt.completed_at = end
    attempt.current_response_id = None
    return attempt


# --------------------------------------------------------------------------- what the screens show

def answered_count(attempt: models.Attempt) -> int:
    return sum(1 for r in attempt.responses if is_answered(r))


def next_unanswered_position(attempt: models.Attempt) -> int:
    """Where 'resume' should land. Practice: the first unanswered question. A test: where you were."""
    if is_timed(attempt) and attempt.current_response_id:
        for response in attempt.responses:
            if response.id == attempt.current_response_id:
                return response.position
    for response in attempt.responses:
        if not is_answered(response):
            return response.position
    return attempt.responses[-1].position if attempt.responses else 1


def palette(attempt: models.Attempt) -> list[dict]:
    """One entry per question for the navigation strip.

    In a running test: answered / unanswered (seen, no answer) / notvisited, plus a `marked` flag for
    "marked for review". Correctness is never shown while a test is running. Once a session is finished
    (or in practice, for answered questions) it is: correct / wrong / skipped."""
    finished = attempt.status != AttemptStatus.IN_PROGRESS
    entries = []
    for r in attempt.responses:
        if is_timed(attempt) and not finished:
            state = "answered" if is_answered(r) else ("unanswered" if r.visited else "notvisited")
        elif is_answered(r):
            state = "correct" if r.is_correct else "wrong"
        else:
            state = "skipped" if finished else "unanswered"
        entries.append({"pos": r.position, "state": state, "marked": bool(r.marked_for_review)})
    return entries


def summary_counts(attempt: models.Attempt) -> dict:
    """Counts for the submit-confirmation screen."""
    rs = attempt.responses
    answered = [r for r in rs if is_answered(r)]
    return {
        "total": len(rs),
        "answered": len(answered),
        "unanswered": len(rs) - len(answered),
        "not_visited": sum(1 for r in rs if not r.visited),
        "marked": sum(1 for r in rs if r.marked_for_review),
        "unrated": sum(1 for r in answered if r.confidence == Confidence.skipped),
    }


def explanation_label(question: models.Question) -> str:
    """How much to trust an explanation. Nothing is called verified unless an admin marked it so."""
    if question.explanation_status == "verified":
        return "Verified"
    if question.source == "ai_json":
        return "AI-supplied, unverified"
    if question.source in ("csv", "xlsx", "docx"):
        return "Unverified (imported from a file)"
    return "Unverified (as printed in the answer PDF)"
