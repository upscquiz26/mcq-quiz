"""App wiring only: environment, middleware, routers. The pages themselves live in app/routes/."""
import os
from datetime import datetime, timedelta
from urllib.parse import urlencode, urlparse

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

load_dotenv()

from app import auth, errors, models, settings, startup
from app.database import BASE_DIR, SessionLocal
from app.routes import (account, admin_dashboard, admin_performance, admin_reports, admin_tools, admin_users, analytics, book_import,
                        duplicates, file_import, json_import, keys, leaderboard, papers, practice, question_reports, quarantine, results, review, sample_audit, suspicious, tests)
from app.routes import revision as revision_routes
from app.routes import auth as auth_routes

startup.init()

# The interactive API docs would list every route to any logged-in user; nobody needs them here.
app = FastAPI(title="UPSC PYQ Practice", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "app", "static")), name="static")

PUBLIC_PATHS = {"/login", "/signup"}
# What a user who must choose a new password is still allowed to reach.
PASSWORD_CHANGE_PATHS = {"/account/password", "/logout"}
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
LAST_ACTIVE_EVERY = timedelta(minutes=5)
SESSION_MAX_AGE = 14 * 24 * 3600


def _same_origin(request: Request) -> bool:
    """Browsers always send Origin (or at least Referer) on a cross-site POST. If either names another
    site, the request is refused. Non-browser clients send neither and are allowed: this exists to stop
    other websites driving a logged-in user's browser, not to authenticate anything."""
    host = request.headers.get("host", "")
    origin = request.headers.get("origin")
    if origin is not None:
        return urlparse(origin).netloc == host
    referer = request.headers.get("referer")
    if referer:
        return urlparse(referer).netloc == host
    return True


@app.middleware("http")
async def require_login(request: Request, call_next):
    """Resolve the session to an approved user on every request.

    Looking the user up each time means rejecting, deactivating or removing someone logs
    them out immediately, and bumping their session_version (password reset/change)
    signs them out everywhere.
    """
    if request.method in UNSAFE_METHODS and not _same_origin(request):
        return PlainTextResponse("Cross-site request blocked.", status_code=403)

    request.state.user = None
    request.state.pending_count = 0
    request.state.report_count = 0
    request.state.suspicion_count = 0
    request.state.leaderboard_on = False            # decides whether the nav shows the Leaderboard link
    user_id = request.session.get("user_id")
    if user_id:
        db = SessionLocal()
        try:
            user = db.get(models.User, user_id)
            if (
                user
                and user.status == models.UserStatus.approved
                and request.session.get("sv", 0) == user.session_version
            ):
                now = datetime.utcnow()
                if user.last_active_at is None or now - user.last_active_at > LAST_ACTIVE_EVERY:
                    user.last_active_at = now
                    db.commit()
                    db.refresh(user)    # commit expires the object; reload it before the session closes
                request.state.user = user
                request.state.leaderboard_on = settings.get_bool(db, "leaderboard_enabled")
                if user.is_admin:
                    request.state.pending_count = (
                        db.query(models.User)
                        .filter(models.User.status == models.UserStatus.pending)
                        .count()
                    )
                    request.state.report_count = db.query(models.QuestionReport).filter_by(status="open").count()
                    request.state.suspicion_count = db.query(models.AnswerSuspicion).filter_by(status="open").count()
        finally:
            db.close()
        if request.state.user is None:
            request.session.clear()

    path = request.url.path
    user = request.state.user
    if user and user.must_change_password and path not in PASSWORD_CHANGE_PATHS and not path.startswith("/static/"):
        return RedirectResponse(url="/account/password", status_code=303)
    if user or path in PUBLIC_PATHS or path.startswith("/static/"):
        return await call_next(request)
    target = "/login"
    if request.method == "GET":
        target += "?" + urlencode({"next": request.url.path + (f"?{request.url.query}" if request.url.query else "")})
    return RedirectResponse(url=target, status_code=303)


# Added after require_login so it wraps it: the session must be loaded before the check runs.
app.add_middleware(
    SessionMiddleware,
    secret_key=auth.get_secret_key(),
    same_site="lax",
    max_age=SESSION_MAX_AGE,
    https_only=os.environ.get("SESSION_HTTPS_ONLY", "").lower() in ("1", "true", "yes"),
)

errors.install(app)
app.include_router(auth_routes.router)
app.include_router(account.router)
app.include_router(practice.router)
app.include_router(tests.router)
app.include_router(results.router)
app.include_router(revision_routes.router)
app.include_router(analytics.router)
app.include_router(admin_dashboard.router)
app.include_router(admin_users.router)
app.include_router(admin_performance.router)
app.include_router(leaderboard.router)
app.include_router(question_reports.router)
app.include_router(admin_reports.router)
app.include_router(json_import.router)
app.include_router(book_import.router)
app.include_router(file_import.router)
app.include_router(keys.router)
app.include_router(duplicates.router)
app.include_router(suspicious.router)
app.include_router(sample_audit.router)
app.include_router(papers.router)
app.include_router(review.router)
app.include_router(quarantine.router)
app.include_router(admin_tools.router)
