"""Timed tests: choosing one, starting it, saving answers as the student goes, and the submit screen.

The question screen and results are in routes/practice.py (shared with topic practice). Everything is
open to any signed-in user, and every attempt is checked to belong to the person asking."""
from datetime import datetime
from fractions import Fraction
import math

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from app import models
from app.database import get_db
from app.models import AttemptStatus
from app.practice import attempts as engine
from app.practice import catalogue, pool
from app.web import flash, templates

router = APIRouter()

CUSTOM_COUNT_CHOICES = (10, 20, 30, 50)


@router.get("/tests")
def tests_page(request: Request, db: Session = Depends(get_db)):
    user = request.state.user
    engine.expire_overdue(db, user.id)
    sections_by_paper: dict = {}
    for entry in catalogue.sections(db, user.id):
        sections_by_paper.setdefault((entry["paper_id"], entry["paper"]), []).append(entry)
    custom_options = pool.filter_options(db)
    custom_options["sources"] = [source for source in custom_options["sources"]
                                 if source["value"] != models.SourceType.BOOK]
    return templates.TemplateResponse(
        "tests.html",
        {
            "request": request,
            "full_tests": catalogue.full_tests(db, user.id),
            "sections_by_paper": sections_by_paper,
            "options": custom_options,
            "count_choices": CUSTOM_COUNT_CHOICES,
            "seconds_per_question": engine.SECONDS_PER_QUESTION,
            "flash": request.session.pop("flash", None),
        },
    )


def _int(value: str, label: str) -> int:
    try:
        return int((value or "").strip())
    except ValueError:
        raise ValueError(f"Please choose a valid {label}.")


def _optional_int(value: str, label: str) -> int | None:
    return _int(value, label) if (value or "").strip() else None


def _optional_marks(value: str) -> float | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        marks = float(value)
    except ValueError:
        raise ValueError("Marks per question must be a positive number.")
    if not math.isfinite(marks) or marks <= 0:
        raise ValueError("Marks per question must be a positive number.")
    return marks


def _optional_negative_fraction(value: str) -> float | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        fraction = float(Fraction(value))
    except (ValueError, ZeroDivisionError, OverflowError):
        raise ValueError("Negative marking must be 0 or a fraction from 0 to 1, such as 1/3.")
    if not math.isfinite(fraction) or not 0 <= fraction <= 1:
        raise ValueError("Negative marking must be 0 or a fraction from 0 to 1, such as 1/3.")
    return fraction


@router.post("/tests/start")
def start_test(
    request: Request,
    mode: str = Form(""), paper_id: str = Form(""), subject_id: str = Form(""),
    duration_minutes: str = Form(""), marks_per_question: str = Form(""), negative_fraction: str = Form(""),
    source_type: str = Form(""), year: str = Form(""), topic_id: str = Form(""), difficulty: str = Form(""),
    unattempted: str = Form(""), count: str = Form("20"),
    db: Session = Depends(get_db),
):
    user = request.state.user
    try:
        if mode == "full":
            attempt, resumed = engine.start_full_test(
                db, user, _int(paper_id, "test"), duration_minutes=_optional_int(duration_minutes, "time limit"),
                marks_per_question=_optional_marks(marks_per_question),
                negative_fraction=_optional_negative_fraction(negative_fraction))
        elif mode == "full_practice":
            attempt, resumed = engine.start_full_paper_practice(db, user, _int(paper_id, "paper"))
        elif mode == "section":
            attempt, resumed = engine.start_section_test(db, user, _int(paper_id, "test"), _int(subject_id, "subject"))
        elif mode == "custom":
            filters = pool.parse_filters(db, source_type, year, subject_id, topic_id, difficulty, unattempted)
            attempt, resumed = engine.start_custom_test(db, user, filters, _int(count, "number of questions"))
        else:
            raise ValueError("Choose a test to start.")
    except (engine.StartRefused, ValueError) as e:
        db.rollback()
        flash(request, str(e))
        return RedirectResponse(url="/tests", status_code=303)

    db.commit()
    if resumed:
        message = ("You already have this test open, so you're carrying on where you left off. The clock kept running."
                   if engine.is_timed(attempt) else "You already have this practice open, so you're carrying on where you left off.")
        flash(request, message, "notice")
    return RedirectResponse(url=f"/attempts/{attempt.id}", status_code=303)


def _as_flag(value: str) -> bool | None:
    value = (value or "").strip().lower()
    if value in ("1", "true", "on", "yes"):
        return True
    if value in ("0", "false", "off", "no"):
        return False
    return None


def _entry(attempt: models.Attempt, response: models.Response) -> dict:
    return {"pos": response.position,
            "state": "answered" if engine.is_answered(response) else "unanswered",
            "marked": bool(response.marked_for_review)}


@router.post("/attempts/{attempt_id}/q/{position}/save")
def save_answer(
    request: Request, attempt_id: int, position: int,
    answer: str = Form(""), confidence: str = Form(""), marked: str = Form(""),
    clear: str = Form(""), go: str = Form(""),
    db: Session = Depends(get_db),
):
    """Autosave for a timed test. Any of: pick an option, rate confidence, mark for review, clear.
    The page calls this with fetch() after every action; without JavaScript it is an ordinary form post."""
    wants_json = "application/json" in request.headers.get("accept", "")
    attempt = engine.own_attempt(db, request.state.user, attempt_id)      # also ends the test if time ran out
    response = engine.response_at(attempt, position)
    result_url = f"/attempts/{attempt.id}/result"

    if attempt.status != AttemptStatus.IN_PROGRESS:
        if wants_json:
            return JSONResponse({"ok": False, "expired": True, "redirect": result_url}, status_code=409)
        flash(request, "This test has finished, so that change wasn't saved.")
        return RedirectResponse(url=result_url, status_code=303)

    try:
        engine.save_test_answer(db, attempt, response, answer=answer, confidence=confidence,
                                marked=_as_flag(marked), clear=_as_flag(clear) is True)
        db.commit()
    except engine.AnswerRejected as e:
        db.rollback()
        if wants_json:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        flash(request, str(e))
        return RedirectResponse(url=f"/attempts/{attempt.id}/q/{position}", status_code=303)

    if wants_json:
        summary = engine.summary_counts(attempt)
        return {"ok": True, "entry": _entry(attempt, response), "answered": summary["answered"],
                "marked": summary["marked"], "remaining": engine.remaining_seconds(attempt),
                "saved_at": datetime.utcnow().isoformat() + "Z"}
    target = {"next": position + 1, "prev": position - 1}.get(go, position)
    target = target if 1 <= target <= len(attempt.responses) else position
    return RedirectResponse(url=f"/attempts/{attempt.id}/q/{target}", status_code=303)


@router.get("/attempts/{attempt_id}/submit")
def submit_page(request: Request, attempt_id: int, db: Session = Depends(get_db)):
    """The 'are you sure?' screen: counts of what is answered, unanswered and marked for review."""
    attempt = engine.own_attempt(db, request.state.user, attempt_id)
    if attempt.status != AttemptStatus.IN_PROGRESS:
        return RedirectResponse(url=f"/attempts/{attempt.id}/result", status_code=303)
    if not engine.is_timed(attempt):
        return RedirectResponse(url=f"/attempts/{attempt.id}", status_code=303)
    first_marked = next((r.position for r in attempt.responses if r.marked_for_review), None)
    first_unanswered = next((r.position for r in attempt.responses if not engine.is_answered(r)), None)
    return templates.TemplateResponse(
        "test_submit.html",
        {
            "request": request, "attempt": attempt, "summary": engine.summary_counts(attempt),
            "first_marked": first_marked, "first_unanswered": first_unanswered,
            "back_pos": engine.next_unanswered_position(attempt),
            "deadline_iso": attempt.deadline_at.isoformat() + "Z",
            "server_now_iso": datetime.utcnow().isoformat() + "Z",
            "remaining": engine.remaining_seconds(attempt),
            "kind_label": models.AttemptKind.LABELS.get(attempt.kind, attempt.kind),
        },
    )
