"""Admin: possible duplicate questions — keep both, or merge one into the other (see app/duplicates.py)."""
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app import audit, duplicates, models
from app.database import get_db
from app.web import flash, require_admin, templates

router = APIRouter(dependencies=[Depends(require_admin)])

PAGE_SIZE = 40


def _back(paper_id: int | None) -> RedirectResponse:
    return RedirectResponse(url="/admin/duplicates" + (f"?paper_id={paper_id}" if paper_id else ""), status_code=303)


def _pair_or_404(db: Session, pair_id: int) -> models.QuestionDuplicate:
    row = db.get(models.QuestionDuplicate, pair_id)
    if not row:
        raise HTTPException(status_code=404, detail="Pair not found")
    return row


def _card(db: Session, q: models.Question) -> dict:
    paper = db.get(models.Paper, q.paper_id)
    return {"q": q, "paper": paper, "label": duplicates.label_of(q, paper), "sources": duplicates.sources_of(db, [q.id]).get(q.id, [])}


def _answer_shown(q: models.Question) -> str:
    letter = (q.correct_answer or "").upper()
    return f"{letter}: {getattr(q, 'option_' + letter.lower())}" if letter in ("A", "B", "C", "D", "E") else "none"


@router.get("/admin/duplicates")
def duplicates_page(request: Request, paper_id: int | None = None, show: str = "open", db: Session = Depends(get_db)):
    paper = db.get(models.Paper, paper_id) if paper_id else None
    if show == "decided":
        rows = (db.query(models.QuestionDuplicate).filter(models.QuestionDuplicate.status != "open")
                .order_by(models.QuestionDuplicate.decided_at.desc()).limit(200).all())
        if paper_id:
            ids = {q.id for q in db.query(models.Question.id).filter(models.Question.paper_id == paper_id)}
            rows = [r for r in rows if r.question_id in ids or r.other_id in ids]
    else:
        show, rows = "open", duplicates.open_pairs(db, paper_id)
    total = len(rows)
    pairs = []
    for row in rows[:PAGE_SIZE]:
        older, newer = db.get(models.Question, row.other_id), db.get(models.Question, row.question_id)
        into = db.get(models.Question, row.merged_into) if row.merged_into else None
        pairs.append({"row": row, "a": _card(db, older), "b": _card(db, newer),
                      "answers_differ": duplicates.answers_differ(older, newer),
                      "answer_a": _answer_shown(older), "answer_b": _answer_shown(newer),
                      "merged_into": duplicates.label_of(into) if into else None})
    return templates.TemplateResponse(
        "duplicates.html",
        {"request": request, "pairs": pairs, "total": total, "shown": len(pairs), "show": show, "paper": paper, "paper_id": paper_id,
         "open_total": db.query(models.QuestionDuplicate).filter_by(status="open").count(),
         "flash": request.session.pop("flash", None)},
    )


@router.post("/admin/duplicates/scan")
def scan_now(request: Request, paper_id: str = Form(""), db: Session = Depends(get_db)):
    """Look for duplicates again — of one paper's questions, or of the whole bank."""
    pid = int(paper_id) if paper_id.strip().isdigit() else None
    if pid is not None and not db.get(models.Paper, pid):
        raise HTTPException(status_code=404, detail="Paper not found")
    counts = duplicates.scan(db, pid)
    audit.log(db, request.state.user, "duplicates.scan", "paper" if pid else None, pid, paper_id=pid, detail=counts)
    db.commit()
    found = counts["exact"] + counts["near"]
    flash(request, (f"Found {found} new possible duplicate{'s' if found != 1 else ''} ({counts['exact']} identical, {counts['near']} very similar)."
                    if found else "No new duplicates found.") + (f" {counts['cleared']} that no longer match were removed." if counts["cleared"] else ""),
          "notice")
    return _back(pid)


@router.post("/admin/duplicates/{pair_id}/keep-both")
def keep_both(request: Request, pair_id: int, paper_id: str = Form(""), db: Session = Depends(get_db)):
    row = _pair_or_404(db, pair_id)
    if row.status == "open":
        duplicates.keep_both(db, request.state.user, row)
        audit.log(db, request.state.user, "duplicate.keep_both", "question", row.question_id,
                  detail={"other": row.other_id, "kind": row.kind, "score": row.score})
        db.commit()
        flash(request, "Kept both. They won't be raised as duplicates again.", "notice")
    return _back(int(paper_id) if paper_id.strip().isdigit() else None)


@router.post("/admin/duplicates/{pair_id}/merge")
def merge(request: Request, pair_id: int, keep: int = Form(...), paper_id: str = Form(""), db: Session = Depends(get_db)):
    row = _pair_or_404(db, pair_id)
    back = _back(int(paper_id) if paper_id.strip().isdigit() else None)
    try:
        a, b = db.get(models.Question, row.other_id), db.get(models.Question, row.question_id)
        differ = duplicates.answers_differ(a, b)
        was_live = (a if keep == b.id else b).status == "live"          # the copy about to be dropped
        kept, dropped = duplicates.merge(db, request.state.user, row, keep)
    except ValueError as e:
        flash(request, str(e))
        return back
    duplicates.scan(db, only=[kept.id])                            # the kept question may match others too
    audit.log(db, request.state.user, "duplicate.merge", "question", kept.id, paper_id=kept.paper_id,
              detail={"kept": duplicates.label_of(kept), "dropped": duplicates.label_of(dropped), "dropped_id": dropped.id,
                      "kind": row.kind, "score": row.score, "answers_differed": differ, "dropped_was_live": was_live})
    db.commit()
    flash(request, f"Merged: kept {duplicates.label_of(kept)}; {duplicates.label_of(dropped)} was taken out of circulation "
                   "(it's in Quarantine and can be restored).", "notice")
    return back
