"""Leaderboards for signed-in users: this week's, and one per test (a paper's full-length test or one of its sections).

Rules and privacy are documented in app/practice/leaderboard.py. While the admin's Leaderboard switch is off, every page here
redirects home and shows nothing."""
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app import models, settings
from app.database import get_db
from app.practice import attempts as engine
from app.practice import catalogue
from app.practice import leaderboard as boards
from app.web import flash, format_duration, templates

router = APIRouter()


def rank_key_for(db, paper_id: int, subject_id: int | None) -> str:
    """The key of the test as it stands now (built from the live questions, like the test itself). 404 if nothing is live."""
    ids = engine.paper_live_ids(db, paper_id, subject_id)
    if not ids or db.get(models.Paper, paper_id) is None:
        raise HTTPException(status_code=404, detail="That leaderboard doesn't exist")
    if subject_id is None:
        return f"paper:{paper_id}:full:{engine.question_set_fingerprint(ids)}"
    return f"paper:{paper_id}:subject:{subject_id}:{engine.question_set_fingerprint(ids)}"


def _off(request: Request, db: Session):
    if settings.get_bool(db, "leaderboard_enabled"):
        return None
    flash(request, "Leaderboards are switched off right now.", "notice")
    return RedirectResponse(url="/", status_code=303)


@router.get("/leaderboard")
def leaderboard_home(request: Request, week: str = "this", db: Session = Depends(get_db)):
    if (blocked := _off(request, db)) is not None:
        return blocked
    user = request.state.user
    week = week if week in ("this", "last") else "this"
    weekly = boards.weekly_board(db, user.id, week)

    engine.expire_overdue(db)
    entries = []
    for e in catalogue.full_tests(db, user.id):
        entries.append({"title": e["paper"].title, "detail": "Full-length test", "url": f"/leaderboard/paper/{e['paper'].id}",
                        "rank_key": e["rank_key"]})
    for e in catalogue.sections(db, user.id):
        entries.append({"title": e["paper"], "detail": f"{e['subject']} section",
                        "url": f"/leaderboard/paper/{e['paper_id']}/subject/{e['subject_id']}", "rank_key": e["rank_key"]})
    listed = []
    for entry in entries:
        board = boards.test_board(db, entry["rank_key"], user.id, settle=False)
        if board["participants"]:
            listed.append({**entry, "participants": board["participants"], "you": board["you"]})
    return templates.TemplateResponse(
        "leaderboard.html",
        {"request": request, "weekly": weekly, "week": week, "listed": listed, "me": user, "duration": format_duration,
         "top_n": boards.TOP_N, "flash": request.session.pop("flash", None)},
    )


def _test_page(request: Request, db: Session, paper_id: int, subject_id: int | None):
    if (blocked := _off(request, db)) is not None:
        return blocked
    user = request.state.user
    key = rank_key_for(db, paper_id, subject_id)
    paper = db.get(models.Paper, paper_id)
    subject = db.get(models.Subject, subject_id) if subject_id is not None else None
    mine = (
        db.query(models.Attempt).filter(models.Attempt.user_id == user.id, models.Attempt.rank_key == key)
        .order_by(models.Attempt.id).first()
    )
    return templates.TemplateResponse(
        "leaderboard_test.html",
        {"request": request, "board": boards.test_board(db, key, user.id), "paper": paper, "subject": subject, "me": user,
         "my_first": mine, "duration": format_duration, "top_n": boards.TOP_N},
    )


@router.get("/leaderboard/paper/{paper_id}")
def full_test_board(request: Request, paper_id: int, db: Session = Depends(get_db)):
    return _test_page(request, db, paper_id, None)


@router.get("/leaderboard/paper/{paper_id}/subject/{subject_id}")
def section_board(request: Request, paper_id: int, subject_id: int, db: Session = Depends(get_db)):
    return _test_page(request, db, paper_id, subject_id)
