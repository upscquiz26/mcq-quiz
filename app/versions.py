"""
Version history for questions, so a bad edit can be undone.

A snapshot of the question's content is stored just BEFORE every change. The
very first edit therefore preserves the question exactly as it was imported.
Restoring an old version never makes a question look reviewed: the restored
content goes back to `needs_review` and has to be confirmed again.
"""
import json
from datetime import datetime

from app import audit, models

# The parts of a question that a reviewer can change. Status is deliberately not here.
CONTENT_FIELDS = (
    "text", "option_a", "option_b", "option_c", "option_d",
    "correct_answer", "subject_id", "topic_id", "difficulty", "explanation", "has_image", "explanation_status",
    "question_hi", "option_a_hi", "option_b_hi", "option_c_hi", "option_d_hi", "explanation_hi", "explanation_hi_status",
)


def content_of(q: models.Question) -> dict:
    return {f: getattr(q, f) for f in CONTENT_FIELDS}


def changed_fields(q: models.Question, new_values: dict) -> list[str]:
    """Which of `new_values` actually differ from what the question holds now."""
    current = content_of(q)
    return [f for f, v in new_values.items() if f in current and (current[f] or None) != (v or None)]


def next_version_no(db, question_id: int) -> int:
    last = (
        db.query(models.QuestionVersion.version_no)
        .filter(models.QuestionVersion.question_id == question_id)
        .order_by(models.QuestionVersion.version_no.desc())
        .first()
    )
    return (last[0] if last else 0) + 1


def snapshot(db, q: models.Question, user, reason: str) -> models.QuestionVersion:
    version = models.QuestionVersion(
        question_id=q.id,
        version_no=next_version_no(db, q.id),
        snapshot_json=json.dumps(content_of(q), ensure_ascii=False),
        reason=reason,
        changed_by=user.id if user else None,
    )
    db.add(version)
    db.flush()          # so the next snapshot in this same transaction gets the next number
    return version


def history(db, question_id: int) -> list[models.QuestionVersion]:
    return (
        db.query(models.QuestionVersion)
        .filter(models.QuestionVersion.question_id == question_id)
        .order_by(models.QuestionVersion.version_no.desc())
        .all()
    )


def restore(db, q: models.Question, version: models.QuestionVersion, user) -> list[str]:
    """Puts an old version's content back. Returns the fields that changed."""
    old = json.loads(version.snapshot_json)
    changed = changed_fields(q, old)
    snapshot(db, q, user, f"before restoring version {version.version_no}")
    for field in CONTENT_FIELDS:
        if field in old:
            setattr(q, field, old[field])
    if q.status != models.QStatus.QUARANTINED:
        q.status = models.QStatus.NEEDS_REVIEW     # restored content must be re-confirmed
    q.flags_acknowledged = False
    q.reviewed_by = q.reviewed_at = None
    audit.log(db, user, "question.version_restore", "question", q.id, paper_id=q.paper_id,
              detail={"version": version.version_no, "fields": changed})
    return changed
