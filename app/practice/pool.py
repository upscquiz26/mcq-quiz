"""
The pool of questions students may practise.

live_questions() is the ONE place that decides what a student can see. Every
student-facing query must start from it (or from can_view_question). A question
is in the pool only if ALL of these hold:

  * its status is `live` (drafts, unreviewed and quarantined questions never are)
  * its paper is published, finished reading, and not archived
  * it has a valid answer (A-D)
  * if it depends on an image (has_image), the printed-page snapshot exists to show

Unpublishing or archiving a paper therefore removes its questions from the pool
immediately, without touching the questions themselves.
"""
from dataclasses import asdict, dataclass

from sqlalchemy import func, or_

from app import models
from app.models import QStatus

ANSWER_LETTERS = ("A", "B", "C", "D", "E")
DIFFICULTIES = ("easy", "medium", "hard", "tricky")


def live_questions(db):
    """A query for Question rows a student is allowed to see."""
    return (
        db.query(models.Question)
        .join(models.Paper, models.Paper.id == models.Question.paper_id)
        .filter(
            models.Question.status == QStatus.LIVE,
            models.Question.correct_answer.in_(ANSWER_LETTERS),
            models.Paper.archived_at.is_(None),
            models.Paper.status == "ready",
            models.Paper.publish_status == "published",
            or_(
                models.Question.has_image.is_(None),
                models.Question.has_image.is_(False),
                models.Question.source_image_path.isnot(None),
            ),
        )
    )


@dataclass
class Filters:
    """What a student chose to practise. Every field is optional; empty means "any"."""
    source_type: str | None = None
    year: int | None = None
    subject_id: int | None = None
    topic_id: int | None = None
    difficulty: str | None = None
    unattempted: bool = False        # only questions this user has never answered

    def as_dict(self) -> dict:
        return asdict(self)


def parse_filters(db, source_type="", year="", subject_id="", topic_id="", difficulty="", unattempted="") -> Filters:
    """Turns raw form/query strings into validated Filters. Raises ValueError with a readable message."""
    def as_int(value, label):
        value = (value or "").strip()
        if not value:
            return None
        try:
            return int(value)
        except ValueError:
            raise ValueError(f"{label} must be a number.")

    f = Filters()
    source_type = (source_type or "").strip()
    if source_type:
        if source_type not in models.SourceType.ALL:
            raise ValueError("Unknown source.")
        f.source_type = source_type
    f.year = as_int(year, "Year")
    f.subject_id = as_int(subject_id, "Subject")
    if f.subject_id is not None and db.get(models.Subject, f.subject_id) is None:
        raise ValueError("Unknown subject.")
    f.topic_id = as_int(topic_id, "Topic")
    if f.topic_id is not None:
        topic = db.get(models.Topic, f.topic_id)
        if topic is None:
            raise ValueError("Unknown topic.")
        if f.subject_id is not None and topic.subject_id != f.subject_id:
            raise ValueError("That topic isn't in the chosen subject.")
    difficulty = (difficulty or "").strip().lower()
    if difficulty:
        if difficulty not in DIFFICULTIES:
            raise ValueError("Unknown difficulty.")
        f.difficulty = difficulty
    f.unattempted = (unattempted or "").strip().lower() in ("1", "true", "on", "yes")
    return f


def filtered_questions(db, user_id: int, f: Filters):
    """live_questions() narrowed by the student's filters."""
    query = live_questions(db)
    if f.source_type:
        query = query.filter(models.Paper.source_type == f.source_type)
    if f.year is not None:
        query = query.filter(models.Paper.year == f.year)
    if f.subject_id is not None:
        query = query.filter(models.Question.subject_id == f.subject_id)
    if f.topic_id is not None:
        query = query.filter(models.Question.topic_id == f.topic_id)
    if f.difficulty:
        query = query.filter(models.Question.difficulty == f.difficulty)
    if f.unattempted:
        answered = (
            db.query(models.Response.question_id)
            .join(models.Attempt, models.Attempt.id == models.Response.attempt_id)
            .filter(models.Attempt.user_id == user_id, models.Response.selected_answer.isnot(None))
        )
        query = query.filter(~models.Question.id.in_(answered))
    return query


def filter_options(db) -> dict:
    """The choices to offer on the practice page: only things that actually have live questions."""
    live = live_questions(db)
    sources = [s for (s,) in live.with_entities(models.Paper.source_type).distinct().all() if s]
    years = sorted({y for (y,) in live.with_entities(models.Paper.year).distinct().all() if y}, reverse=True)
    subjects = (
        live.join(models.Subject, models.Subject.id == models.Question.subject_id)
        .with_entities(models.Subject.id, models.Subject.name, func.count(models.Question.id))
        .group_by(models.Subject.id, models.Subject.name)
        .order_by(models.Subject.id)
        .all()
    )
    topics = (
        live.join(models.Topic, models.Topic.id == models.Question.topic_id)
        .join(models.Subject, models.Subject.id == models.Topic.subject_id)
        .with_entities(models.Topic.id, models.Topic.name, models.Subject.id, models.Subject.name,
                       func.count(models.Question.id))
        .group_by(models.Topic.id, models.Topic.name, models.Subject.id, models.Subject.name)
        .order_by(models.Subject.id, models.Topic.name)
        .all()
    )
    difficulties = {d for (d,) in live.with_entities(models.Question.difficulty).distinct().all() if d}
    return {
        "sources": [{"value": s, "label": models.SourceType.LABELS[s]} for s in models.SourceType.ALL if s in sources],
        "years": years,
        "subjects": [{"id": i, "name": n, "count": c} for i, n, c in subjects],
        "topics": [{"id": i, "name": n, "subject_id": si, "subject": sn, "count": c} for i, n, si, sn, c in topics],
        "difficulties": [d for d in DIFFICULTIES if d in difficulties],
    }


def can_view_question(db, question_id: int) -> bool:
    return live_questions(db).filter(models.Question.id == question_id).first() is not None


def publish_blockers(db, paper: models.Paper) -> dict:
    """What stops this paper being published. Empty dict means it can be published."""
    problems = {}
    if paper.status != "ready":
        problems["not_ready"] = "The paper hasn't finished being read."
    if paper.archived_at is not None:
        problems["archived"] = "The paper is archived."
    active = [q for q in paper.questions if q.status != QStatus.QUARANTINED]
    if not active:
        problems["empty"] = "The paper has no questions."
    to_confirm = [q for q in active if q.status in (QStatus.DRAFT, QStatus.NEEDS_REVIEW)]
    if to_confirm:
        problems["to_confirm"] = f"{len(to_confirm)} question{'s' if len(to_confirm) != 1 else ''} still to confirm."
    no_answer = [q for q in active if q.correct_answer not in ANSWER_LETTERS]
    if no_answer:
        problems["no_answer"] = f"{len(no_answer)} question{'s' if len(no_answer) != 1 else ''} without an answer."
    flagged = [q for q in active if q.status in (QStatus.VERIFIED, QStatus.LIVE) and q.ocr_flags and not q.flags_acknowledged]
    if flagged:
        problems["flagged"] = (f"{len(flagged)} confirmed question{'s' if len(flagged) != 1 else ''} still carry warnings nobody has "
                               "looked at — open each one and confirm it again.")
    from app import sample_audit
    reason = sample_audit.blocker(db, paper)
    if reason:
        problems["audit"] = reason
    return problems


def needs_snapshot(q: models.Question) -> bool:
    """True if the question depends on an image but has no page snapshot to show."""
    return bool(q.has_image) and not q.source_image_path
