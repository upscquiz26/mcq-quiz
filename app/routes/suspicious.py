"""Admin: questions whose answer key the strongest students disagree with (see app/practice/suspicion.py)."""
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from app import audit, models, sample_audit
from app.database import get_db
from app.models import QStatus
from app.practice import suspicion
from app.web import flash, require_admin, templates

router = APIRouter(dependencies=[Depends(require_admin)])


def _row_or_404(db: Session, row_id: int) -> models.AnswerSuspicion:
    row = db.get(models.AnswerSuspicion, row_id)
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    return row


def _table(counts: dict, total: int) -> list[dict]:
    return [{"letter": letter, "n": counts.get(letter, 0), "pct": round(100 * counts.get(letter, 0) / total) if total else 0}
            for letter in suspicion.LETTERS]


@router.get("/admin/suspicious")
def suspicious_page(request: Request, show: str = "open", db: Session = Depends(get_db)):
    show = "handled" if show == "handled" else "open"
    query = db.query(models.AnswerSuspicion)
    query = query.filter(models.AnswerSuspicion.status == "open") if show == "open" else query.filter(models.AnswerSuspicion.status != "open")
    rows = query.order_by(models.AnswerSuspicion.high_n.desc(), models.AnswerSuspicion.id).all()
    cards = []
    for row in rows:
        q = db.get(models.Question, row.question_id)
        paper = db.get(models.Paper, q.paper_id)
        cards.append({
            "row": row, "q": q, "paper": paper,
            "high": _table(json.loads(row.high_counts_json), row.high_n),
            "everyone": _table(json.loads(row.all_counts_json), row.all_n),
            "reports": db.query(models.QuestionReport).filter_by(question_id=q.id, status="open").count(),
            "key_changed": bool(row.status != "open" and row.handled_key and row.handled_key != q.correct_answer),
        })
    last = db.query(func.max(models.AnswerSuspicion.computed_at)).scalar()
    return templates.TemplateResponse(
        "suspicious.html",
        {"request": request, "cards": cards, "show": show, "open_total": suspicion.open_count(db), "last": last,
         "rules": {"min_answers": suspicion.MIN_ANSWERS, "min_responders": suspicion.MIN_RESPONDERS,
                   "high_share": int(suspicion.HIGH_SHARE * 100), "min_high": suspicion.MIN_HIGH, "margin": suspicion.MARGIN},
         "flash": request.session.pop("flash", None)},
    )


@router.post("/admin/suspicious/analyse")
def analyse_now(request: Request, db: Session = Depends(get_db)):
    result = suspicion.analyse(db)
    audit.log(db, request.state.user, "suspicion.analyse", detail=result)
    db.commit()
    if not result["checked"]:
        message = (f"Nothing to analyse yet: a question needs answers from at least {suspicion.MIN_RESPONDERS} students who have each "
                   f"answered {suspicion.MIN_ANSWERS} or more questions.")
    else:
        message = (f"Checked {result['checked']} question{'s' if result['checked'] != 1 else ''}: {result['open']} open in the queue "
                   f"({result['new']} new, {result['cleared']} cleared).")
    flash(request, message, "notice")
    return RedirectResponse(url="/admin/suspicious", status_code=303)


@router.post("/admin/suspicious/{row_id}/dismiss")
def dismiss(request: Request, row_id: int, db: Session = Depends(get_db)):
    row = _row_or_404(db, row_id)
    if row.status == "open":
        suspicion.dismiss(db, request.state.user, row)
        audit.log(db, request.state.user, "suspicion.dismiss", "question", row.question_id,
                  detail={"key": row.handled_key, "students_preferred": row.popular_answer})
        db.commit()
        flash(request, "Marked as fine: the key stays. It comes back only if its answer is changed and students still disagree.", "notice")
    return RedirectResponse(url="/admin/suspicious", status_code=303)


@router.post("/admin/suspicious/{row_id}/send-back")
def send_back(request: Request, row_id: int, db: Session = Depends(get_db)):
    """Take the question out of students' sight and back into review, so its answer is checked against the printed paper."""
    row = _row_or_404(db, row_id)
    q = db.get(models.Question, row.question_id)
    if row.status == "open" and q.status in (QStatus.VERIFIED, QStatus.LIVE):
        was = q.status
        q.status, q.reviewed_by, q.reviewed_at, q.flags_acknowledged = QStatus.NEEDS_REVIEW, None, None, False
        sample_audit.invalidate(q.paper)
        suspicion.mark_sent_back(db, request.state.user, row)
        audit.log(db, request.state.user, "question.reopen", "question", q.id, paper_id=q.paper_id,
                  detail={"number": q.question_number, "was": was, "reason": "suspicious answer"})
        db.commit()
        flash(request, f"Q{q.question_number} is back in review; students can't see it until you confirm it again.", "notice")
        return RedirectResponse(url=f"/review/{q.paper_id}#q{q.question_number}", status_code=303)
    return RedirectResponse(url="/admin/suspicious", status_code=303)
