"""
What a finished attempt tells the student: mistake reasons, the guessing report, a per-subject breakdown and
the "would it have paid to attempt more?" estimate.

Everything here reads a finished attempt's Responses. The marks themselves are worked out in attempts.py
(when the attempt is finished); nothing here changes a score.

Mistake reasons (only for WRONG answers), in this order of priority:
  * "No idea"            -> concept gap
  * "Guessed"            -> guess
  * "Sure":
        answered in under a quarter of your typical time on this attempt (at least 8 s)  -> careless mistake
        took more than 2.5 x your typical time                                          -> time pressure / confusion
        otherwise                                                                       -> misconception (overconfidence)
  * no confidence given  -> not classified (you can still pick one yourself)
Time only matters for "sure" answers: a fast "no idea" is still a gap in knowledge, not carelessness.
"Typical time" is the median time you spent on the questions you answered in that attempt.
The suggestion is only a starting point: the student can change any reason, and their choice is kept.
"""
from statistics import median

from app import models
from app.models import Confidence, MistakeReason

QUICK_FRACTION = 0.25
QUICK_MIN_SECONDS = 8
LONG_FACTOR = 2.5
DEFAULT_PACE_SECONDS = 72          # used when there are too few timings to find a median
MIN_TIMINGS_FOR_MEDIAN = 3
MIN_SAMPLE = 5                     # fewest answers we will draw an accuracy estimate from
MAX_NOTE_LENGTH = 500

REASON_LABELS = {
    MistakeReason.knowledge_gap: "Concept gap",
    MistakeReason.guess_miss: "Guess",
    MistakeReason.conceptual_confusion: "Misconception / overconfidence",
    MistakeReason.careless: "Careless mistake",
    MistakeReason.time_pressure: "Time pressure / confusion",
    MistakeReason.unset: "Not classified",
}
REASON_HELP = {
    MistakeReason.knowledge_gap: "You said you had no idea, so the topic itself needs learning.",
    MistakeReason.guess_miss: "You guessed and it didn't come off — recall, not luck, needs building here.",
    MistakeReason.conceptual_confusion: "You were sure, and wrong: a fact or idea is mixed up. Worth fixing first.",
    MistakeReason.careless: "You were sure and answered very quickly: read the question and options once more.",
    MistakeReason.time_pressure: "You were sure but took a long time: something about it was confusing.",
    MistakeReason.unset: "No confidence rating was given, so no reason could be suggested.",
}
# What the student may choose from (never "not classified").
EDITABLE_REASONS = (
    MistakeReason.knowledge_gap, MistakeReason.conceptual_confusion, MistakeReason.guess_miss,
    MistakeReason.careless, MistakeReason.time_pressure,
)


def is_answered(response: models.Response) -> bool:
    return response.selected_answer is not None


def is_wrong(response: models.Response) -> bool:
    return is_answered(response) and response.is_correct is False


# --------------------------------------------------------------------------- mistake reasons

def typical_seconds(attempt: models.Attempt) -> float:
    """The median time spent on the questions that were answered (or the default pace if too few timings)."""
    times = [r.time_spent_seconds for r in attempt.responses if is_answered(r) and r.time_spent_seconds]
    return float(median(times)) if len(times) >= MIN_TIMINGS_FOR_MEDIAN else float(DEFAULT_PACE_SECONDS)


def suggest_reason(response: models.Response, typical: float) -> MistakeReason | None:
    """The suggested reason for a wrong answer, or None if the response isn't a wrong answer."""
    if not is_wrong(response):
        return None
    if response.confidence == Confidence.no_idea:
        return MistakeReason.knowledge_gap
    if response.confidence == Confidence.guessed:
        return MistakeReason.guess_miss
    if response.confidence != Confidence.sure:
        return MistakeReason.unset                       # answered but never rated
    seconds = response.time_spent_seconds
    if seconds:                                          # 0 / None means we have no timing to judge by
        if seconds < max(QUICK_MIN_SECONDS, QUICK_FRACTION * typical):
            return MistakeReason.careless
        if seconds > LONG_FACTOR * typical:
            return MistakeReason.time_pressure
    return MistakeReason.conceptual_confusion


def classify_attempt(attempt: models.Attempt) -> None:
    """Fills in the suggested reason on every wrong answer the student hasn't already chosen a reason for."""
    typical = typical_seconds(attempt)
    for r in attempt.responses:
        if r.reason_overridden:
            continue
        suggestion = suggest_reason(r, typical)
        r.mistake_reason = suggestion if suggestion is not None else MistakeReason.unset


class ReasonRejected(Exception):
    """The reason can't be saved; str(e) says why."""


def set_reason(response: models.Response, reason: str, note: str | None) -> None:
    """The student's own choice. It sticks (even if the attempt is re-analysed)."""
    if not is_wrong(response):
        raise ReasonRejected("Only wrong answers have a mistake reason.")
    try:
        chosen = MistakeReason[reason]
    except KeyError:
        raise ReasonRejected("Choose one of the listed reasons.")
    if chosen not in EDITABLE_REASONS:
        raise ReasonRejected("Choose one of the listed reasons.")
    note = " ".join((note or "").split())
    if len(note) > MAX_NOTE_LENGTH:
        raise ReasonRejected(f"Keep the note under {MAX_NOTE_LENGTH} characters.")
    response.mistake_reason = chosen
    response.reason_overridden = True
    response.note = note or None


def reset_reason(attempt: models.Attempt, response: models.Response) -> None:
    """Drops the student's choice and goes back to the suggestion."""
    if not is_wrong(response):
        raise ReasonRejected("Only wrong answers have a mistake reason.")
    response.reason_overridden = False
    suggestion = suggest_reason(response, typical_seconds(attempt))
    response.mistake_reason = suggestion if suggestion is not None else MistakeReason.unset


def reason_counts(attempt: models.Attempt) -> list[dict]:
    """How many wrong answers fall under each reason, biggest first."""
    counts: dict = {}
    for r in attempt.responses:
        if is_wrong(r):
            counts[r.mistake_reason] = counts.get(r.mistake_reason, 0) + 1
    return [{"reason": reason, "label": REASON_LABELS.get(reason, str(reason)), "help": REASON_HELP.get(reason, ""),
             "count": n} for reason, n in sorted(counts.items(), key=lambda kv: -kv[1])]


# --------------------------------------------------------------------------- the guessing report

def _tally(responses) -> dict:
    answered = [r for r in responses if is_answered(r)]
    right = sum(1 for r in answered if r.is_correct)
    return {
        "answered": len(answered), "right": right, "wrong": len(answered) - right,
        "marks": sum(r.marks_awarded or 0.0 for r in answered),
        "accuracy": round(100 * right / len(answered)) if answered else None,
    }


def guessing_report(attempt: models.Attempt) -> dict:
    """How the marks split by how sure the student said they were, and what skipping the shaky answers would
    have done to the score."""
    by_level = {
        "sure": _tally([r for r in attempt.responses if r.confidence == Confidence.sure]),
        "guessed": _tally([r for r in attempt.responses if r.confidence == Confidence.guessed]),
        "no_idea": _tally([r for r in attempt.responses if r.confidence == Confidence.no_idea]),
        "unrated": _tally([r for r in attempt.responses if r.confidence == Confidence.skipped]),
    }
    shaky_marks = by_level["guessed"]["marks"] + by_level["no_idea"]["marks"]
    shaky_answers = by_level["guessed"]["answered"] + by_level["no_idea"]["answered"]
    actual = attempt.score or 0.0
    return {
        "levels": by_level,
        "shaky_answers": shaky_answers,
        "shaky_marks": shaky_marks,                     # net marks from "guessed" and "no idea" answers
        "actual_score": actual,
        "score_if_skipped": actual - shaky_marks,       # the score with those answers left blank
        "helped": shaky_marks > 0,
        "cost": shaky_marks < 0,
    }


# --------------------------------------------------------------------------- a timed test's breakdown

def subject_breakdown(db, attempt: models.Attempt) -> list[dict]:
    """Attempted / right / wrong / skipped / marks per subject, in order of subject."""
    names = {s.id: s.name for s in db.query(models.Subject).all()}
    subject_of = {q.id: q.subject_id for q in
                  db.query(models.Question).filter(models.Question.id.in_([r.question_id for r in attempt.responses])).all()}
    rows: dict = {}
    for r in attempt.responses:
        row = rows.setdefault(subject_of.get(r.question_id), {"total": 0, "answered": 0, "right": 0, "wrong": 0, "marks": 0.0})
        row["total"] += 1
        if is_answered(r):
            row["answered"] += 1
            row["right" if r.is_correct else "wrong"] += 1
            row["marks"] += r.marks_awarded or 0.0
    result = []
    for subject_id, row in rows.items():
        row["subject"] = names.get(subject_id, "No subject set")
        row["skipped"] = row["total"] - row["answered"]
        row["accuracy"] = round(100 * row["right"] / row["answered"]) if row["answered"] else None
        row["_order"] = (subject_id is None, subject_id or 0)
        result.append(row)
    return sorted(result, key=lambda row: row["_order"])


def _accuracy_by_subject(db, user_id: int) -> tuple[dict, tuple[int, int]]:
    """The user's own record across every finished attempt: {subject_id: (answered, right)} and the overall pair."""
    rows = (
        db.query(models.Question.subject_id, models.Response.is_correct)
        .join(models.Response, models.Response.question_id == models.Question.id)
        .join(models.Attempt, models.Attempt.id == models.Response.attempt_id)
        .filter(models.Attempt.user_id == user_id, models.Attempt.status != "in_progress",
                models.Response.selected_answer.isnot(None), models.Response.is_correct.isnot(None))
        .all()
    )
    per_subject: dict = {}
    total = right = 0
    for subject_id, correct in rows:
        answered, hits = per_subject.get(subject_id, (0, 0))
        per_subject[subject_id] = (answered + 1, hits + (1 if correct else 0))
        total += 1
        right += 1 if correct else 0
    return per_subject, (total, right)


def worth_attempting(db, attempt: models.Attempt) -> dict | None:
    """An ESTIMATE of whether attempting the skipped questions would have paid off.

    For each skipped question: expected marks = p x marks - (1 - p) x penalty, where p is the student's own
    accuracy in that question's subject (across all their finished attempts, once they have at least
    MIN_SAMPLE answers there), otherwise their overall accuracy, otherwise their accuracy in this attempt.
    Questions with a positive expectation are 'worth attempting'. Returns None if there is nothing to say."""
    skipped = [r for r in attempt.responses if not is_answered(r)]
    answered_here = [r for r in attempt.responses if is_answered(r)]
    if not skipped or not answered_here:
        return None
    here_accuracy = sum(1 for r in answered_here if r.is_correct) / len(answered_here)
    per_subject, (total, right) = _accuracy_by_subject(db, attempt.user_id)
    overall = right / total if total >= MIN_SAMPLE else here_accuracy

    subject_of = {q.id: q.subject_id for q in
                  db.query(models.Question).filter(models.Question.id.in_([r.question_id for r in skipped])).all()}
    names = {s.id: s.name for s in db.query(models.Subject).all()}
    items = []
    for r in skipped:
        subject_id = subject_of.get(r.question_id)
        answered, hits = per_subject.get(subject_id, (0, 0))
        p = hits / answered if answered >= MIN_SAMPLE else overall
        expected = p * (r.marks_if_correct or 0.0) - (1 - p) * (r.penalty_if_wrong or 0.0)
        if expected > 0:
            items.append({"position": r.position, "subject": names.get(subject_id, "No subject set"),
                          "p": p, "expected": expected})

    marks = attempt.responses[0].marks_if_correct or 0.0
    penalty = attempt.responses[0].penalty_if_wrong or 0.0
    return {
        "skipped": len(skipped), "worth": len(items), "items": items,
        "expected_gain": sum(i["expected"] for i in items),
        "break_even_percent": round(100 * penalty / (marks + penalty)) if marks + penalty else None,
        "your_accuracy_percent": round(100 * here_accuracy),
    }
