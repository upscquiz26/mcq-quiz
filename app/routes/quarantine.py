"""Quarantine: questions taken out of circulation. Nothing is deleted; restoring sends a question back to review."""
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app import audit, duplicates, models
from app.database import get_db
from app.models import QStatus
from app.web import flash, require_admin, templates

router = APIRouter(dependencies=[Depends(require_admin)])


@router.get("/quarantine")
def quarantine_list(request: Request, paper_id: int | None = None, db: Session = Depends(get_db)):
    query = (
        db.query(models.Question, models.Paper)
        .join(models.Paper, models.Paper.id == models.Question.paper_id)
        .filter(models.Question.status == QStatus.QUARANTINED)
        .order_by(models.Question.quarantined_at.desc())
    )
    if paper_id:
        query = query.filter(models.Question.paper_id == paper_id)
    return templates.TemplateResponse(
        "quarantine.html",
        {"request": request, "rows": query.all(), "paper_id": paper_id, "flash": request.session.pop("flash", None)},
    )


@router.post("/quarantine/{question_id}/restore")
def restore_question(request: Request, question_id: int, db: Session = Depends(get_db)):
    q = db.get(models.Question, question_id)
    if not q:
        raise HTTPException(status_code=404, detail="Question not found")
    if q.status == QStatus.QUARANTINED:
        reason = q.quarantine_reason
        q.status = QStatus.NEEDS_REVIEW          # it has to be confirmed again before it can be used
        q.quarantine_reason = None
        q.quarantined_at = None
        q.reviewed_by = q.reviewed_at = None
        q.flags_acknowledged = False
        undone = duplicates.undo_merge(db, q)                 # a merged copy is its own question again
        duplicates.scan(db, only=[q.id])
        audit.log(db, request.state.user, "question.restore", "question", q.id, paper_id=q.paper_id,
                  detail={"number": q.question_number, "was_quarantined_for": reason, "duplicate_pairs_reopened": undone})
        db.commit()
    flash(request, f"Q{q.question_number} was restored and is waiting for review again.", "notice")
    return RedirectResponse(url="/quarantine", status_code=303)
