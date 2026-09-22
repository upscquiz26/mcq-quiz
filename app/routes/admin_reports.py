"""Admin: the queue of problems students have reported on questions.

Closing a report never edits the question — fixing it happens on the paper's review page (or by quarantining it); this page
only records what was decided. Every decision is audited."""
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app import audit, models
from app.database import get_db
from app.practice import reports
from app.web import flash, require_admin, templates

router = APIRouter(dependencies=[Depends(require_admin)])

FILTERS = {"open": "Open", "resolved": "Resolved", "dismissed": "Dismissed", "all": "All"}
CLOSING = {"resolved": "resolve", "dismissed": "dismiss"}
MAX_GROUPS = 100


@router.get("/admin/reports")
def reports_queue(request: Request, show: str = "open", db: Session = Depends(get_db)):
    show = show if show in FILTERS else "open"
    query = db.query(models.QuestionReport)
    if show != "all":
        query = query.filter(models.QuestionReport.status == show)
    rows = query.order_by(models.QuestionReport.created_at.desc(), models.QuestionReport.id.desc()).all()

    groups: dict = {}                                                       # one block per question, most recent first
    for r in rows:
        groups.setdefault(r.question_id, []).append(r)
    questions = {q.id: q for q in db.query(models.Question).filter(models.Question.id.in_(list(groups) or [0])).all()}
    papers = {p.id: p.title for p in db.query(models.Paper).all()}
    users = {u.id: u.display_name or u.username for u in db.query(models.User).filter(
        models.User.id.in_({r.user_id for r in rows} | {r.resolved_by for r in rows if r.resolved_by} or {0})).all()}
    counts = {key: db.query(models.QuestionReport).filter_by(status=key).count() for key in reports.STATUSES}
    return templates.TemplateResponse(
        "admin_reports.html",
        {
            "request": request, "show": show, "filters": FILTERS, "counts": counts,
            "groups": [(questions[qid], items) for qid, items in list(groups.items())[:MAX_GROUPS] if qid in questions],
            "hidden_groups": max(0, len(groups) - MAX_GROUPS), "papers": papers, "users": users,
            "kinds": reports.KINDS, "statuses": models.QStatus,
            "flash": request.session.pop("flash", None),
        },
    )


def _close(request: Request, db: Session, report: models.QuestionReport, status: str, note: str) -> None:
    reports.close(db, report, request.state.user, status, note)
    question = db.get(models.Question, report.question_id)
    audit.log(db, request.state.user, f"report.{CLOSING[status]}", "question", report.question_id,
              paper_id=question.paper_id if question else None,
              detail={"report": report.id, "kind": report.kind, "note": report.resolution_note})


@router.post("/admin/reports/question/{question_id}/close")
def close_reports_for_question(request: Request, question_id: int, status: str = Form(""), note: str = Form(""),
                               db: Session = Depends(get_db)):
    """Closes every open report on one question at once (usually just one) — e.g. after fixing an answer key that five
    students reported."""
    if status not in CLOSING:
        raise HTTPException(status_code=400, detail="Choose resolve or dismiss")
    open_ones = db.query(models.QuestionReport).filter_by(question_id=question_id, status="open").all()
    if not open_ones:
        raise HTTPException(status_code=404, detail="No open reports on that question")
    for report in open_ones:
        _close(request, db, report, status, note)
    db.commit()
    flash(request, f"{len(open_ones)} report{'s' if len(open_ones) != 1 else ''} "
                   f"{'resolved' if status == 'resolved' else 'dismissed'}.", "notice")
    return RedirectResponse(url="/admin/reports", status_code=303)
