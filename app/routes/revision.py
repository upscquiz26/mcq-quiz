"""Revision for students: the mistake notebook, mistake practice, a question's own page, bookmarks and notes.

Open to any signed-in user. Everything is that user's own: notebook entries, bookmarks and notes are keyed by
user id, and a question is only ever shown if it is live and the student has already met it."""
import random

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app import auth, models
from app.database import get_db
from app.models import AttemptKind
from app.practice import attempts as engine
from app.practice import grading, pool, reports, revision
from app.web import flash, templates

router = APIRouter()

NOTEBOOK_ROWS = 100
COUNT_CHOICES = (5, 10, 20, 30, 50)


def _opt_int(value: str) -> int | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        raise ValueError("That filter isn't valid.")


def _reason(value: str) -> str | None:
    value = (value or "").strip()
    if not value:
        return None
    if value not in {r.name for r in grading.EDITABLE_REASONS}:
        raise ValueError("That reason isn't valid.")
    return value


@router.get("/revision")
def revision_page(request: Request, subject_id: str = "", topic_id: str = "", reason: str = "", state: str = "all",
                  db: Session = Depends(get_db)):
    user = request.state.user
    try:
        subject, topic, reason_name = _opt_int(subject_id), _opt_int(topic_id), _reason(reason)
    except ValueError:
        subject = topic = reason_name = None
    if state not in revision.STATE_FILTERS:
        state = "all"

    everything = revision.notebook(db, user.id)                           # unfiltered: for the choices and counts
    shown = revision.filter_entries(everything, subject_id=subject, topic_id=topic, reason=reason_name, state=state)

    subjects = {s.id: s.name for s in db.query(models.Subject).all()}
    topics = {t.id: t.name for t in db.query(models.Topic).all()}
    subject_choices = sorted({(e["question"].subject_id, subjects.get(e["question"].subject_id, "")) for e in everything
                              if e["question"].subject_id}, key=lambda x: x[0])
    topic_choices = sorted({(e["question"].topic_id, topics.get(e["question"].topic_id, "")) for e in everything
                            if e["question"].topic_id}, key=lambda x: x[1])
    reason_choices = [(r.name, grading.REASON_LABELS[r]) for r in grading.EDITABLE_REASONS
                      if any(e["reason_name"] == r.name for e in everything)]
    state_counts = {key: sum(1 for e in everything if key == "all" or e["state"] == key or
                             (key == "revise" and e["state"] in ("due", "scheduled")))
                    for key in revision.STATE_FILTERS}

    return templates.TemplateResponse(
        "revision.html",
        {
            "request": request, "entries": shown[:NOTEBOOK_ROWS], "total_shown": len(shown),
            "row_limit": NOTEBOOK_ROWS, "due_now": revision.due_count(db, user.id),
            "subject_choices": subject_choices, "topic_choices": topic_choices, "reason_choices": reason_choices,
            "state_filters": revision.STATE_FILTERS, "state_labels": revision.STATE_LABELS, "state_counts": state_counts,
            "selected": {"subject_id": subject, "topic_id": topic, "reason": reason_name, "state": state},
            "reason_labels": grading.REASON_LABELS, "count_choices": COUNT_CHOICES,
            "has_any": bool(everything), "ladder": revision.LADDER_DAYS,
            "flash": request.session.pop("flash", None),
        },
    )


@router.post("/revision/start")
def start_mistake_practice(
    request: Request, mode: str = Form("all"), subject_id: str = Form(""), topic_id: str = Form(""),
    reason: str = Form(""), include_mastered: str = Form(""), count: str = Form("10"),
    db: Session = Depends(get_db),
):
    user = request.state.user
    try:
        subject, topic, reason_name = _opt_int(subject_id), _opt_int(topic_id), _reason(reason)
        wanted = int((count or "").strip() or 10)
    except ValueError:
        flash(request, "Please choose valid options.")
        return RedirectResponse(url="/revision", status_code=303)
    if wanted < 1:
        flash(request, "Choose how many questions you want (at least 1).")
        return RedirectResponse(url="/revision", status_code=303)
    mode = "due" if mode == "due" else "all"

    ids = revision.mistake_practice_ids(
        db, user.id, mode=mode, subject_id=subject, topic_id=topic, reason=reason_name,
        include_mastered=include_mastered.lower() in ("1", "true", "on", "yes"))
    if not ids:
        flash(request, "Nothing is due for revision right now — well done." if mode == "due"
              else "No mistakes match those choices.")
        return RedirectResponse(url="/revision", status_code=303)

    wanted = min(wanted, engine.MAX_SESSION_QUESTIONS, len(ids))
    chosen = ids[:wanted] if mode == "due" else random.sample(ids, wanted)      # due: most overdue first; else random
    attempt = engine.create_mistake_attempt(
        db, user, chosen, {"mode": mode, "subject_id": subject, "topic_id": topic, "reason": reason_name})
    db.commit()
    return RedirectResponse(url=f"/attempts/{attempt.id}", status_code=303)


# --------------------------------------------------------------------------- a question's own page

def _viewable_question(db: Session, user, question_id: int) -> models.Question:
    """The question, only if it is live and this student has already met it. Anything else is a plain 404."""
    if not pool.can_view_question(db, question_id) or not revision.has_seen(db, user.id, question_id):
        raise HTTPException(status_code=404, detail="Not found")
    return db.get(models.Question, question_id)


@router.get("/questions/{question_id}")
def question_page(request: Request, question_id: int, db: Session = Depends(get_db)):
    user = request.state.user
    question = _viewable_question(db, user, question_id)
    locked = revision.locked_by_running_test(db, user.id, question_id)
    reveal = revision.can_reveal(db, user.id, question_id)

    history, item = [], None
    if reveal:
        history = (
            db.query(models.Response, models.Attempt)
            .join(models.Attempt, models.Attempt.id == models.Response.attempt_id)
            .filter(models.Attempt.user_id == user.id, models.Response.question_id == question_id,
                    models.Response.selected_answer.isnot(None), models.Response.is_correct.isnot(None))
            .order_by(models.Response.id.desc()).all()
        )
        item = db.query(models.RevisionItem).filter_by(user_id=user.id, question_id=question_id).first()
    return templates.TemplateResponse(
        "question_view.html",
        {
            "request": request, "question": question, "reveal": reveal, "locked": locked,
            "options": [("A", question.option_a), ("B", question.option_b), ("C", question.option_c), ("D", question.option_d)],
            "explanation_label": engine.explanation_label(question),
            "history": history, "item": item, "kind_labels": AttemptKind.LABELS,
            "reason_labels": grading.REASON_LABELS, "confidence_labels": engine.CONFIDENCE_LABELS,
            "bookmarked": revision.is_bookmarked(db, user.id, question_id),
            "note_text": revision.get_note(db, user.id, question_id),
            "max_note": revision.MAX_NOTE_LENGTH, "today": revision.today(),
            "report_kinds": reports.KINDS, "report_max_note": reports.MAX_NOTE,
            "my_report": reports.latest_report(db, user.id, question_id) if reveal else None,
            "flash": request.session.pop("flash", None),
        },
    )


@router.post("/questions/{question_id}/bookmark")
def toggle_bookmark(request: Request, question_id: int, on: str = Form("1"), next: str = Form(""),
                    db: Session = Depends(get_db)):
    user = request.state.user
    _viewable_question(db, user, question_id)
    revision.set_bookmark(db, user.id, question_id, on.lower() in ("1", "true", "on", "yes"))
    db.commit()
    return RedirectResponse(url=auth.safe_next(next) if next else f"/questions/{question_id}", status_code=303)


@router.post("/questions/{question_id}/note")
def save_note(request: Request, question_id: int, text: str = Form(""), next: str = Form(""),
              db: Session = Depends(get_db)):
    user = request.state.user
    _viewable_question(db, user, question_id)
    target = auth.safe_next(next) if next else f"/questions/{question_id}"
    try:
        revision.save_note(db, user.id, question_id, text)
        db.commit()
        flash(request, "Note saved." if text.strip() else "Note removed.", "notice")
    except revision.NoteRejected as e:
        db.rollback()
        flash(request, str(e))
    return RedirectResponse(url=target, status_code=303)


@router.get("/bookmarks")
def bookmarks_page(request: Request, db: Session = Depends(get_db)):
    user = request.state.user
    rows = (
        pool.live_questions(db)
        .join(models.QuestionBookmark, models.QuestionBookmark.question_id == models.Question.id)
        .filter(models.QuestionBookmark.user_id == user.id)
        .with_entities(models.Question, models.QuestionBookmark.created_at)
        .order_by(models.QuestionBookmark.created_at.desc())
        .all()
    )
    notes = {n.question_id: n.text for n in db.query(models.QuestionNote)
             .filter(models.QuestionNote.user_id == user.id,
                     models.QuestionNote.question_id.in_([q.id for q, _ in rows] or [0])).all()}
    subjects = {s.id: s.name for s in db.query(models.Subject).all()}
    return templates.TemplateResponse(
        "bookmarks.html",
        {"request": request, "rows": rows, "notes": notes, "subjects": subjects,
         "flash": request.session.pop("flash", None)},
    )
