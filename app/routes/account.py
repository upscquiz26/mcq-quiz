"""A signed-in user's own account: profile settings and changing their password."""
import re
from datetime import datetime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from app import audit, auth, language as language_module, models, settings, throttle
from app.database import get_db
from app.practice import activity
from app.web import flash, templates

router = APIRouter()


def _me(request: Request, db: Session) -> models.User:
    """The signed-in user, loaded in THIS request's session so it can be modified."""
    return db.get(models.User, request.state.user.id)


def _profile_page(request: Request, db: Session, me: models.User, error: str | None = None, status_code: int = 200):
    return templates.TemplateResponse(
        "account.html",
        {
            "request": request, "me": me, "error": error,
            "performance_visible": settings.get_bool(db, "user_performance_enabled"),
            "leaderboard_enabled": settings.get_bool(db, "leaderboard_enabled"),
            "flash": request.session.pop("flash", None),
        },
        status_code=status_code,
    )


@router.get("/account")
def account(request: Request, db: Session = Depends(get_db)):
    return _profile_page(request, db, _me(request, db))


@router.post("/account/language")
def choose_language(request: Request, language: str = Form(""), next: str = Form(""), db: Session = Depends(get_db)):
    """The quick language switch without JavaScript: set the preference and go back to where the student was."""
    me = _me(request, db)
    if language in language_module.PREFS:
        me.language = language
        db.commit()
    return RedirectResponse(url=auth.safe_next(next) if next else "/", status_code=303)


@router.post("/account/language/save")
def save_language(request: Request, language: str = Form(""), db: Session = Depends(get_db)):
    """The same, for the page's own switch (JavaScript): remember the choice and answer with JSON. Nothing else is touched — no attempt,
    answer, timer or position — because the page only changes what it shows."""
    if language not in language_module.PREFS:
        return JSONResponse({"error": "Choose English, Hindi or both."}, status_code=400)
    me = _me(request, db)
    me.language = language
    db.commit()
    return JSONResponse({"language": language})


@router.post("/account/profile")
def update_profile(
    request: Request,
    language: str = Form(""),
    display_name: str = Form(""),
    show_on_leaderboard: bool = Form(False),
    daily_target: str = Form(""),
    db: Session = Depends(get_db),
):
    me = _me(request, db)
    try:
        target = activity.parse_target(daily_target)
    except ValueError as e:
        return _profile_page(request, db, me, str(e), 400)
    name = re.sub(r"\s+", " ", display_name).strip()
    if name:
        problem = auth.validate_display_name(name)
        if problem:
            return _profile_page(request, db, me, problem, 400)
        # Nobody may borrow another account's name (e.g. "admin") as their display name.
        taken = (
            db.query(models.User.id)
            .filter(models.User.id != me.id,
                    (func.lower(models.User.username) == name.lower())
                    | (func.lower(models.User.display_name) == name.lower()))
            .first()
        )
        if taken:
            return _profile_page(request, db, me, "That name is already in use. Please choose another.", 400)
    me.display_name = name or None
    me.show_on_leaderboard = show_on_leaderboard
    me.daily_target = target
    if language in language_module.PREFS:
        me.language = language
    db.commit()
    flash(request, "Your settings were saved.", "notice")
    return RedirectResponse(url="/account", status_code=303)


def _password_page(request: Request, me: models.User, error: str | None = None, status_code: int = 200):
    return templates.TemplateResponse(
        "password.html",
        {"request": request, "error": error, "forced": me.must_change_password},
        status_code=status_code,
    )


@router.get("/account/password")
def password_form(request: Request, db: Session = Depends(get_db)):
    return _password_page(request, _me(request, db))


@router.post("/account/password")
def change_password(
    request: Request,
    current_password: str = Form(""),
    new_password: str = Form(""),
    confirm_password: str = Form(""),
    db: Session = Depends(get_db),
):
    me = _me(request, db)
    key = throttle.key_for_password_check(me.id)

    wait = throttle.seconds_locked(db, key)
    if wait:
        return _password_page(
            request, me, f"Too many wrong attempts. Please wait {throttle.describe_wait(wait)} and try again.", 429
        )
    if not auth.verify_password(current_password, me.password_hash):
        throttle.record_failure(db, key)
        db.commit()
        return _password_page(request, me, "Your current password is incorrect.", 400)
    problem = auth.validate_new_password(new_password, confirm_password, current_password)
    if problem:
        return _password_page(request, me, problem, 400)

    me.password_hash = auth.hash_password(new_password)
    me.must_change_password = False
    me.password_changed_at = datetime.utcnow()
    me.session_version += 1                          # signs out every OTHER session of this account
    request.session["sv"] = me.session_version       # ...but keeps this one
    throttle.clear(db, key)
    audit.log(db, me, "user.password_change", "user", me.id)
    db.commit()
    flash(request, "Your password was changed. Any other devices were signed out.", "notice")
    return RedirectResponse(url="/", status_code=303)
