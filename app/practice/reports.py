"""
'Report a problem' on a question.

A student who can see a question's answer (they've answered it in practice or finished a test that had it) may report that the
answer, the wording or the explanation looks wrong. Reports wait in the admin's queue (/admin/reports); nothing changes for
the student's own results — reporting never edits, hides or re-grades anything. Only the admin can act on a report.

Rules
  * one OPEN report per student per question (a second submission is told so, not duplicated);
  * at most DAILY_LIMIT reports per student per 24 hours, so the queue can't be flooded;
  * the note is plain text of at most MAX_NOTE characters and is only ever shown escaped.
"""
from datetime import datetime, timedelta

from app import models

KINDS = {
    "wrong_answer": "The marked answer looks wrong",
    "wrong_text": "The question or an option is wrong or unclear",
    "wrong_explanation": "The explanation is wrong",
    "other": "Something else",
}
STATUSES = ("open", "resolved", "dismissed")
MAX_NOTE = 500
DAILY_LIMIT = 20


class ReportRejected(Exception):
    """The report can't be filed; str(e) says why, in words for the student."""


def open_report(db, user_id: int, question_id: int) -> models.QuestionReport | None:
    return (
        db.query(models.QuestionReport)
        .filter_by(user_id=user_id, question_id=question_id, status="open")
        .order_by(models.QuestionReport.id.desc()).first()
    )


def latest_report(db, user_id: int, question_id: int) -> models.QuestionReport | None:
    """This student's most recent report on the question (any status), for the line shown under the form."""
    return (
        db.query(models.QuestionReport).filter_by(user_id=user_id, question_id=question_id)
        .order_by(models.QuestionReport.id.desc()).first()
    )


LANGUAGES = {"en": "English version", "hi": "Hindi version", "both": "Both versions"}


def file_report(db, user, question: models.Question, kind: str, note: str, now: datetime | None = None,
                language: str | None = None) -> models.QuestionReport:
    """`language` says which version of the question the report is about (a wrong translation is a different problem from a wrong fact).
    It is only kept for a question that has a Hindi version; anything else is ignored rather than refused."""
    now = now or datetime.utcnow()
    from app import language as language_module
    language = language if language in LANGUAGES and language_module.has_hindi(question) else None
    if kind not in KINDS:
        raise ReportRejected("Please choose what is wrong.")
    note = (note or "").replace("\r\n", "\n").strip()
    if len(note) > MAX_NOTE:
        raise ReportRejected(f"Keep the note under {MAX_NOTE} characters.")
    if kind == "other" and not note:
        raise ReportRejected("Please say what the problem is.")
    if open_report(db, user.id, question.id) is not None:
        raise ReportRejected("You've already reported this question. The admin hasn't looked at it yet.")
    recent = db.query(models.QuestionReport).filter(
        models.QuestionReport.user_id == user.id, models.QuestionReport.created_at >= now - timedelta(hours=24)).count()
    if recent >= DAILY_LIMIT:
        raise ReportRejected("You've sent a lot of reports today. Please try again tomorrow.")
    report = models.QuestionReport(user_id=user.id, question_id=question.id, kind=kind, note=note or None,
                                   answer_at_report=question.correct_answer, created_at=now, language=language)
    db.add(report)
    db.flush()
    return report


def open_count(db) -> int:
    return db.query(models.QuestionReport).filter_by(status="open").count()


def close(db, report: models.QuestionReport, admin, status: str, note: str = "") -> None:
    report.status = status
    report.resolved_by = admin.id
    report.resolved_at = datetime.utcnow()
    report.resolution_note = (note or "").strip()[:MAX_NOTE] or None
