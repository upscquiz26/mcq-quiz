"""A student reports a problem with a question. The report only ever lands in the admin's queue; it changes nothing else.

Allowed only for a live question the student has already met AND can see the answer to — the same rule as the question's
own page, so the form can't be used to probe questions in a running test or ones the student hasn't seen."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app import auth, models
from app.database import get_db
from app.practice import reports, revision
from app.routes.revision import _viewable_question
from app.web import flash

router = APIRouter()


@router.post("/questions/{question_id}/report")
def report_question(request: Request, question_id: int, kind: str = Form(""), note: str = Form(""), next: str = Form(""),
                    language: str = Form(""), db: Session = Depends(get_db)):
    user = request.state.user
    question = _viewable_question(db, user, question_id)                  # 404 unless live and already met
    target = auth.safe_next(next) if next else f"/questions/{question_id}"
    if not revision.can_reveal(db, user.id, question_id):
        flash(request, "You can report a question once its answer has been shown to you.")
        return RedirectResponse(url=target, status_code=303)
    try:
        reports.file_report(db, user, question, kind, note, language=language or None)
        db.commit()
        flash(request, "Thanks — your report was sent to the admin. Your own results aren't changed.", "notice")
    except reports.ReportRejected as e:
        db.rollback()
        flash(request, str(e))
    return RedirectResponse(url=target, status_code=303)
