"""Admin: the sample audit of a paper's review (see app/sample_audit.py for the rules)."""
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app import audit, models, sample_audit
from app.database import get_db
from app.web import flash, require_admin, templates

router = APIRouter(dependencies=[Depends(require_admin)])


def _paper(db: Session, paper_id: int) -> models.Paper:
    paper = db.get(models.Paper, paper_id)
    if not paper:
        raise HTTPException(status_code=404, detail="Paper not found")
    return paper


def _last_results(paper: models.Paper) -> dict:
    """Verdicts already recorded on questions (kept after an audit ends), for showing what the last audit found."""
    checked = [q for q in paper.questions if q.audit_result]
    return {"ok": sum(1 for q in checked if q.audit_result == "ok"), "wrong": sum(1 for q in checked if q.audit_result == "wrong")}


@router.get("/review/{paper_id}/audit")
def audit_page(request: Request, paper_id: int, db: Session = Depends(get_db)):
    paper = _paper(db, paper_id)
    pool = sample_audit.unedited_confirmed(db, paper)
    state = paper.audit_state or "none"
    return templates.TemplateResponse(
        "sample_audit.html",
        {
            "request": request, "paper": paper, "state": state, "round": paper.audit_round or 0,
            "pool": len(pool), "sample": sample_audit.sample_size(len(pool)) if pool else 0,
            "picked": sample_audit.picked(paper) if state == "pending" else [],
            "results": _last_results(paper), "fail_at": sample_audit.AUDIT_FAIL_AT, "minimum": sample_audit.AUDIT_MIN,
            "fraction": int(sample_audit.AUDIT_FRACTION * 100), "blocker": sample_audit.blocker(db, paper),
            "flash": request.session.pop("flash", None),
        },
    )


@router.post("/review/{paper_id}/audit/start")
def audit_start(request: Request, paper_id: int, db: Session = Depends(get_db)):
    paper = _paper(db, paper_id)
    if (paper.audit_state or "none") == "pending":
        flash(request, "An audit is already running for this paper.")
        return RedirectResponse(url=f"/review/{paper_id}/audit", status_code=303)
    try:
        sample = sample_audit.start(db, paper)
    except ValueError as e:
        flash(request, str(e))
        return RedirectResponse(url=f"/review/{paper_id}/audit", status_code=303)
    audit.log(db, request.state.user, "sample_audit.start", "paper", paper.id, paper_id=paper.id,
              detail={"round": paper.audit_round, "questions": [q.question_number for q in sample]})
    db.commit()
    return RedirectResponse(url=f"/review/{paper_id}/audit", status_code=303)


@router.post("/review/{paper_id}/audit/{question_id}/check")
def audit_check(request: Request, paper_id: int, question_id: int, verdict: str = Form(""), db: Session = Depends(get_db)):
    paper = _paper(db, paper_id)
    q = db.get(models.Question, question_id)
    if not q or q.paper_id != paper.id:
        raise HTTPException(status_code=404, detail="Question not found")
    try:
        outcome = sample_audit.check(db, paper, q, verdict)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    audit.log(db, request.state.user, "sample_audit.check", "question", q.id, paper_id=paper.id,
              detail={"number": q.question_number, "verdict": verdict, "round": paper.audit_round})
    if outcome is not None:
        audit.log(db, request.state.user, "sample_audit.result", "paper", paper.id, paper_id=paper.id, detail=outcome)
        if outcome["passed"]:
            flash(request, f"The audit passed: {outcome['wrong']} of {outcome['checked']} checked questions were wrong.", "notice")
        else:
            flash(request, f"The audit FAILED: {outcome['wrong']} of {outcome['checked']} checked questions were wrong. "
                           f"{outcome['sent_back']} question{'s' if outcome['sent_back'] != 1 else ''} confirmed without an edit "
                           "went back to review for a second pass.")
    db.commit()
    return RedirectResponse(url=f"/review/{paper_id}/audit" if outcome is None else f"/review/{paper_id}", status_code=303)
