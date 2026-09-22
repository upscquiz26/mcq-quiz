"""The results screen for a finished practice session or test, and editing mistake reasons.

Open to any signed-in user; every attempt is checked to belong to the person asking (a 404 otherwise)."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app import models, settings
from app.database import get_db
from app.models import AttemptKind, AttemptStatus, Confidence
from app.practice import attempts as engine
from app.practice import grading, leaderboard, pool
from app.web import flash, templates

router = APIRouter()

FILTERS = {
    "all": "All",
    "wrong": "Wrong",
    "skipped": "Skipped",
    "shaky": "Guessed / no idea",
}


def _matches(response: models.Response, show: str) -> bool:
    if show == "wrong":
        return grading.is_wrong(response)
    if show == "skipped":
        return not grading.is_answered(response)
    if show == "shaky":
        return grading.is_answered(response) and response.confidence in (Confidence.guessed, Confidence.no_idea)
    return True


@router.get("/attempts/{attempt_id}/result")
def attempt_result(request: Request, attempt_id: int, show: str = "all", db: Session = Depends(get_db)):
    attempt = engine.own_attempt(db, request.state.user, attempt_id)
    if attempt.status == AttemptStatus.IN_PROGRESS:
        return RedirectResponse(url=f"/attempts/{attempt.id}", status_code=303)
    if show not in FILTERS:
        show = "all"

    timed = engine.is_timed(attempt)
    questions = {q.id: q for q in db.query(models.Question)
                 .filter(models.Question.id.in_([r.question_id for r in attempt.responses])).all()}
    visible = {r.question_id for r in attempt.responses if pool.can_view_question(db, r.question_id)}
    answered = engine.answered_count(attempt)
    gained = sum(r.marks_awarded for r in attempt.responses if r.marks_awarded and r.marks_awarded > 0)
    lost = -sum(r.marks_awarded for r in attempt.responses if r.marks_awarded and r.marks_awarded < 0)

    guessing = grading.guessing_report(attempt)
    rank, board_url = None, None
    if timed and settings.get_bool(db, "leaderboard_enabled") and attempt.paper_id:
        rank = leaderboard.rank_of_attempt(db, attempt)
        board_url = f"/leaderboard/paper/{attempt.paper_id}" + (f"/subject/{attempt.subject_id}" if attempt.kind == AttemptKind.SECTIONAL else "")
    return templates.TemplateResponse(
        "attempt_result.html",
        {
            "request": request, "attempt": attempt, "questions": questions, "visible": visible,
            "answered": answered, "timed": timed, "gained": gained, "lost": lost,
            "accuracy": round(100 * attempt.correct_count / answered) if answered else None,
            "attempt_rate": round(100 * answered / attempt.total_questions) if attempt.total_questions else None,
            "kind_label": AttemptKind.LABELS.get(attempt.kind, attempt.kind),
            "guessing": guessing, "rank": rank, "board_url": board_url,
            "has_confidence": any(guessing["levels"][k]["answered"] for k in ("sure", "guessed", "no_idea")),
            "reason_counts": grading.reason_counts(attempt),
            "subjects": grading.subject_breakdown(db, attempt) if timed else [],
            "worth": grading.worth_attempting(db, attempt) if timed else None,
            "rows": [r for r in attempt.responses if _matches(r, show)],
            "filters": FILTERS, "show": show,
            "reason_labels": grading.REASON_LABELS, "confidence_labels": engine.CONFIDENCE_LABELS,
        },
    )


@router.post("/attempts/{attempt_id}/q/{position}/reason")
def save_reason(
    request: Request, attempt_id: int, position: int,
    reason: str = Form(""), note: str = Form(""), reset: str = Form(""),
    db: Session = Depends(get_db),
):
    """The student's own answer to 'why did I get this wrong?'. It overrides the suggestion and is kept."""
    attempt = engine.own_attempt(db, request.state.user, attempt_id)
    response = engine.response_at(attempt, position)
    back = RedirectResponse(url=f"/attempts/{attempt.id}/q/{position}", status_code=303)
    if attempt.status == AttemptStatus.IN_PROGRESS:
        flash(request, "You can set a mistake reason once the session is finished.")
        return back
    try:
        if reset:
            grading.reset_reason(attempt, response)
            message = "Back to the suggested reason."
        else:
            grading.set_reason(response, reason, note)
            message = "Saved."
        db.commit()
        flash(request, message, "notice")
    except grading.ReasonRejected as e:
        db.rollback()
        flash(request, str(e))
    return back
