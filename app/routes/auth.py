"""Login, logout and account requests."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import audit, auth, models, throttle
from app.database import get_db
from app.web import templates

router = APIRouter()

MAX_PENDING_REQUESTS = 50
NO_ADMIN_MESSAGE = "No admin account is set up. Set APP_PASSWORD in your .env file and restart."


def landing_page(user: models.User, next_url: str | None) -> str:
    """Where a user goes after logging in: the page they were trying to reach, otherwise their own home.
    The admin's home is the dashboard; everyone else's is the student home at "/"."""
    target = auth.safe_next(next_url)
    return "/admin" if target == "/" and user.is_admin else target


def _has_admin(db: Session) -> bool:
    return db.query(models.User).filter(models.User.is_admin.is_(True)).first() is not None


@router.get("/login")
def login_form(request: Request, next: str = "/", db: Session = Depends(get_db)):
    if request.state.user:
        return RedirectResponse(url=landing_page(request.state.user, next), status_code=303)
    error = None if _has_admin(db) else NO_ADMIN_MESSAGE
    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "error": error,
            "notice": request.session.pop("notice", None),
            "next": auth.safe_next(next),
        },
    )


@router.post("/login")
def login(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    next: str = Form("/"),
    db: Session = Depends(get_db),
):
    ip = request.client.host if request.client else None
    key = throttle.key_for_username(username)

    # Locked out? Refuse without even checking the password, and say the same thing for any username.
    wait = throttle.seconds_locked(db, key, ip)
    if wait:
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "notice": None, "next": auth.safe_next(next),
             "error": f"Too many failed attempts. Please wait {throttle.describe_wait(wait)} and try again."},
            status_code=429,
        )

    user = db.query(models.User).filter(models.User.username == auth.normalize_username(username)).first()
    # Always verify against some hash so a missing user costs the same as a wrong password.
    password_ok = auth.verify_password(password, user.password_hash if user else auth.DUMMY_HASH)

    status_code = 401
    if not user or not password_ok:
        error = "Incorrect username or password."
        throttle.record_failure(db, key, ip)
        db.commit()
    elif user.status == models.UserStatus.pending:
        error, status_code = "Your account request is still awaiting admin approval.", 403
    elif user.status == models.UserStatus.rejected:
        error, status_code = "Your account request was declined by the admin.", 403
    elif user.status == models.UserStatus.deactivated:
        error, status_code = "This account has been deactivated. Please contact the admin.", 403
    else:
        request.session.clear()  # fresh session on login
        request.session["user_id"] = user.id
        request.session["sv"] = user.session_version
        throttle.clear(db, key)
        audit.log(db, user, "user.login", "user", user.id)   # failed attempts are not logged: the "username" may be a mistyped password
        db.commit()
        return RedirectResponse(url=landing_page(user, next), status_code=303)
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "error": error, "notice": None, "next": auth.safe_next(next)},
        status_code=status_code,
    )


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)


def _signup_error(request: Request, message: str, username: str = ""):
    return templates.TemplateResponse(
        "signup.html", {"request": request, "error": message, "username": username}, status_code=400
    )


@router.get("/signup")
def signup_form(request: Request):
    if request.state.user:
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse("signup.html", {"request": request, "error": None, "username": ""})


@router.post("/signup")
def signup(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    confirm: str = Form(""),
    db: Session = Depends(get_db),
):
    if request.state.user:
        return RedirectResponse(url="/", status_code=303)
    username = auth.normalize_username(username)
    problem = auth.validate_signup(username, password, confirm)
    if problem:
        return _signup_error(request, problem, username)

    taken = "That username is taken or already requested."
    if db.query(models.User).filter(models.User.username == username).first():
        return _signup_error(request, taken, username)
    pending = db.query(models.User).filter(models.User.status == models.UserStatus.pending).count()
    if pending >= MAX_PENDING_REQUESTS:
        return _signup_error(request, "Too many pending requests right now — please try again later.", username)

    new_user = models.User(
        username=username,
        password_hash=auth.hash_password(password),
        is_admin=False,
        status=models.UserStatus.pending,
    )
    db.add(new_user)
    try:
        db.flush()
        audit.log(db, None, "user.signup_request", "user", new_user.id, detail={"username": username})
        db.commit()
    except IntegrityError:  # someone claimed the name between the check and the insert
        db.rollback()
        return _signup_error(request, taken, username)

    request.session["notice"] = "Request sent — you can log in once the admin approves it."
    return RedirectResponse(url="/login", status_code=303)
