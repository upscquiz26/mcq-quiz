"""Choosing what to practise, the question and results screens, and finishing. Open to any signed-in user;
every attempt is checked to belong to the person asking (see attempts.own_attempt).

Timed tests are started and saved in routes/tests.py; they share the screens and routes below."""
from datetime import datetime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app import language, models
from app.database import get_db
from app.models import AttemptKind, AttemptStatus
from app.practice import attempts as engine
from app.practice import grading, pool, reports, revision
from app.web import flash, templates

router = APIRouter()

COUNT_CHOICES = (5, 10, 20, 30, 50)
DEFAULT_COUNT = 10


def _filters_from(db: Session, params) -> pool.Filters:
    return pool.parse_filters(
        db,
        source_type=params.get("source_type", ""), year=params.get("year", ""),
        subject_id=params.get("subject_id", ""), topic_id=params.get("topic_id", ""),
        difficulty=params.get("difficulty", ""), unattempted=params.get("unattempted", ""),
    )


@router.get("/practice")
def practice_page(request: Request, db: Session = Depends(get_db)):
    user = request.state.user
    engine.expire_overdue(db, user.id)                    # finish any test whose time ran out while they were away
    mine = db.query(models.Attempt).filter(models.Attempt.user_id == user.id)
    return templates.TemplateResponse(
        "practice.html",
        {
            "request": request,
            "options": pool.filter_options(db),
            "total_available": pool.live_questions(db).count(),
            "count_choices": COUNT_CHOICES, "default_count": DEFAULT_COUNT,
            "unfinished": mine.filter(models.Attempt.status == AttemptStatus.IN_PROGRESS)
                              .order_by(models.Attempt.started_at.desc()).all(),
            "recent": mine.filter(models.Attempt.status != AttemptStatus.IN_PROGRESS)
                          .order_by(models.Attempt.started_at.desc()).limit(8).all(),
            "kind_labels": AttemptKind.LABELS, "answered_count": engine.answered_count,
            "time_left": engine.time_left_label,
            "flash": request.session.pop("flash", None),
        },
    )


@router.get("/practice/count")
def practice_count(request: Request, db: Session = Depends(get_db)):
    """How many questions match the chosen filters (the page calls this as the filters change)."""
    try:
        filters = _filters_from(db, request.query_params)
    except ValueError as e:
        return JSONResponse({"count": 0, "error": str(e)}, status_code=400)
    return {"count": pool.filtered_questions(db, request.state.user.id, filters).count(), "error": None}


@router.post("/practice/start")
def start_practice(
    request: Request,
    source_type: str = Form(""), year: str = Form(""), subject_id: str = Form(""), topic_id: str = Form(""),
    difficulty: str = Form(""), unattempted: str = Form(""), count: str = Form(str(DEFAULT_COUNT)),
    db: Session = Depends(get_db),
):
    try:
        filters = pool.parse_filters(db, source_type, year, subject_id, topic_id, difficulty, unattempted)
    except ValueError as e:
        flash(request, str(e))
        return RedirectResponse(url="/practice", status_code=303)
    try:
        wanted = int(count.strip() or DEFAULT_COUNT)
    except ValueError:
        wanted = 0
    if wanted < 1:
        flash(request, "Choose how many questions you want (at least 1).")
        return RedirectResponse(url="/practice", status_code=303)

    attempt = engine.create_practice_attempt(db, request.state.user, filters, wanted)
    if attempt is None:
        flash(request, "No questions match those choices. Try loosening a filter.")
        return RedirectResponse(url="/practice", status_code=303)
    db.commit()
    return RedirectResponse(url=f"/attempts/{attempt.id}", status_code=303)


@router.get("/attempts/{attempt_id}")
def resume_attempt(request: Request, attempt_id: int, db: Session = Depends(get_db)):
    attempt = engine.own_attempt(db, request.state.user, attempt_id)
    if attempt.status != AttemptStatus.IN_PROGRESS:
        return RedirectResponse(url=f"/attempts/{attempt.id}/result", status_code=303)
    return RedirectResponse(url=f"/attempts/{attempt.id}/q/{engine.next_unanswered_position(attempt)}", status_code=303)


@router.get("/attempts/{attempt_id}/q/{position}")
def show_question(request: Request, attempt_id: int, position: int, db: Session = Depends(get_db)):
    attempt = engine.own_attempt(db, request.state.user, attempt_id)
    response = engine.response_at(attempt, position)
    engine.record_view(db, attempt, response)
    db.commit()

    question = db.get(models.Question, response.question_id)
    available = pool.can_view_question(db, question.id)          # unpublished mid-session? then it disappears
    finished = attempt.status != AttemptStatus.IN_PROGRESS
    total = len(attempt.responses)
    pref = language.pref_of(request.state.user)
    has_hindi = db.query(models.Question.id).join(models.Response, models.Response.question_id == models.Question.id).filter(
        models.Response.attempt_id == attempt.id,
        or_(models.Question.question_hi.isnot(None), models.Question.option_a_hi.isnot(None))).first() is not None
    context = {
        "show_toggle": has_hindi or pref != "en",            # the quick language switch, when anything in this session has Hindi
        "request": request, "attempt": attempt, "response": response, "question": question,
        "available": available, "finished": finished, "position": position, "total": total,
        "options": [(letter, text) for letter, text, _ in language.option_rows(question)],
        "palette": engine.palette(attempt),
        "prev_pos": position - 1 if position > 1 else None,
        "next_pos": position + 1 if position < total else None,
        "answered": engine.answered_count(attempt),
        "confidence_labels": engine.CONFIDENCE_LABELS,
        "kind_label": AttemptKind.LABELS.get(attempt.kind, attempt.kind),
        "flash": request.session.pop("flash", None),
    }

    if engine.is_timed(attempt) and not finished:
        # A running test: no feedback of any kind, and a live countdown from the SERVER's clock.
        context.update({
            "summary": engine.summary_counts(attempt),
            "deadline_iso": attempt.deadline_at.isoformat() + "Z",
            "server_now_iso": datetime.utcnow().isoformat() + "Z",
            "remaining": engine.remaining_seconds(attempt),
        })
        return templates.TemplateResponse("test_question.html", context)

    reveal = available and (engine.is_answered(response) or finished)
    user_id = request.state.user.id
    context.update({
        "reveal": reveal,
        "explanation_label": engine.explanation_label(question),
        # Once the answer is showing, the student can bookmark the question and keep a private note on it.
        "bookmarked": revision.is_bookmarked(db, user_id, question.id) if reveal else False,
        "note_text": revision.get_note(db, user_id, question.id) if reveal else "",
        "page_url": f"/attempts/{attempt.id}/q/{position}",
        "max_note": revision.MAX_NOTE_LENGTH,
        "report_kinds": reports.KINDS, "report_max_note": reports.MAX_NOTE,
        "my_report": reports.latest_report(db, user_id, question.id) if reveal else None,
        # Once a session is finished, a wrong answer shows its suggested mistake reason and lets the student change it.
        "is_wrong": grading.is_wrong(response),
        "reason_label": grading.REASON_LABELS.get(response.mistake_reason, ""),
        "reason_help": grading.REASON_HELP.get(response.mistake_reason, ""),
        "reason_choices": [(r.name, grading.REASON_LABELS[r]) for r in grading.EDITABLE_REASONS],
        "show_marks": attempt.kind not in (AttemptKind.TOPIC, AttemptKind.PAPER_PRACTICE),
    })
    return templates.TemplateResponse("attempt_question.html", context)


@router.post("/attempts/{attempt_id}/q/{position}/answer")
def answer_question(
    request: Request, attempt_id: int, position: int,
    answer: str = Form(""), confidence: str = Form(""), db: Session = Depends(get_db),
):
    attempt = engine.own_attempt(db, request.state.user, attempt_id)
    response = engine.response_at(attempt, position)
    try:
        engine.submit_answer(db, attempt, response, answer, confidence)
        db.commit()
    except engine.AnswerRejected as e:
        db.rollback()
        flash(request, str(e))
    return RedirectResponse(url=f"/attempts/{attempt.id}/q/{position}", status_code=303)


@router.post("/attempts/{attempt_id}/finish")
def finish(request: Request, attempt_id: int, db: Session = Depends(get_db)):
    attempt = engine.own_attempt(db, request.state.user, attempt_id)
    engine.finish_attempt(db, attempt)
    db.commit()
    return RedirectResponse(url=f"/attempts/{attempt.id}/result", status_code=303)
