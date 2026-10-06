"""Small pieces every route module shares: templates, the admin check, flash messages."""
import logging
import json
import os

from fastapi import HTTPException, Request
from fastapi.templating import Jinja2Templates

from app import language
from app.database import BASE_DIR

templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "app", "templates"))
logger = logging.getLogger("uvicorn.error")


def format_duration(seconds) -> str:
    """4325 -> '1h 12m 05s', 95 -> '1m 35s'."""
    if seconds is None:
        return "—"
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}h {minutes:02d}m {secs:02d}s" if hours else f"{minutes}m {secs:02d}s"


def format_marks(value) -> str:
    """2.0 -> '2', 1.3333 -> '1.33', -0.6667 -> '-0.67'."""
    if value is None:
        return "—"
    return f"{round(float(value) + 0.0, 2):g}"


templates.env.filters["duration"] = format_duration
templates.env.globals["lang_modes"] = language.modes                # which display modes show which language of a question (see language.py)
templates.env.globals["list_text"] = language.list_text
templates.env.globals["user_language"] = lambda request: language.pref_of(getattr(request.state, "user", None))
templates.env.globals["lang_labels"] = language.PREF_LABELS
templates.env.globals["expl_label_hi"] = language.explanation_label_hi
templates.env.globals["primary"] = language.primary       # (language, question text, four options): English if the question has it, else Hindi
templates.env.globals["option_rows"] = language.option_rows


def book_source_ref(question):
    try:
        return json.loads(question.source_ref) if question.source_ref else None
    except (TypeError, json.JSONDecodeError):
        return None


templates.env.globals["book_source_ref"] = book_source_ref
templates.env.filters["marks"] = format_marks


# --------------------------------------------------------------------------- navigation (the sidebar)

def _item(label, href, icon, *match, exact=False, badge=None):
    """A sidebar link. `match` are extra path prefixes that also mean "you are here"; exact=True matches the href only."""
    return {"label": label, "href": href, "icon": icon, "match": [(href, exact)] + [(m, False) for m in match], "badge": badge}


def build_nav(request: Request) -> list[dict]:
    """The sidebar as data: groups of links for the signed-in user, with exactly one link marked active (the one with the
    longest matching path prefix), and count badges for the admin. Students never get an admin link."""
    user = request.state.user
    if not user:
        return []
    student_links = [
        _item("Practice", "/practice", "practice"),
        _item("Tests", "/tests", "clock"),
        _item("Revision", "/revision", "revision", "/questions", "/bookmarks"),
        _item("Progress", "/analytics", "chart"),
    ]
    if getattr(request.state, "leaderboard_on", False):
        student_links.append(_item("Leaderboard", "/leaderboard", "trophy"))
    if user.is_admin:
        groups = [
            {"title": "Overview", "items": [_item("Dashboard", "/admin", "grid", exact=True)]},
            {"title": "Content", "items": [
                _item("Papers", "/", "file", "/review", "/papers", exact=False),
                _item("Upload paper", "/upload", "upload"),
                _item("Import JSON", "/admin/import/json", "braces"),
                _item("Import book JSON", "/admin/books/import/json", "book"),
                _item("Import a file", "/admin/import/file", "upload"),
                _item("Duplicates", "/admin/duplicates", "list"),
                _item("Quarantine", "/quarantine", "archive"),
            ]},
            {"title": "People", "items": [
                _item("Users", "/admin/users", "users", badge=getattr(request.state, "pending_count", 0) or None),
                _item("User performance", "/admin/performance", "trend"),
            ]},
            {"title": "Quality", "items": [
                _item("Reports", "/admin/reports", "flag", badge=getattr(request.state, "report_count", 0) or None),
                _item("Suspicious answers", "/admin/suspicious", "alert", badge=getattr(request.state, "suspicion_count", 0) or None),
            ]},
            {"title": "Safety", "items": [
                _item("Audit log", "/admin/audit", "audit"),
                _item("Backups", "/admin/backups", "database"),
            ]},
            {"title": "As a student", "items": student_links},
        ]
    else:
        groups = [{"title": None, "items": [_item("Home", "/", "home", exact=True)] + student_links}]

    path = request.url.path
    best, best_len = None, -1
    for group in groups:
        for item in group["items"]:
            for prefix, exact in item["match"]:
                hit = path == prefix if exact or prefix == "/" else (path == prefix or path.startswith(prefix.rstrip("/") + "/"))
                if hit and len(prefix) > best_len:
                    best, best_len = item, len(prefix)
    for group in groups:
        for item in group["items"]:
            item["active"] = item is best
    return groups


templates.env.globals["nav_groups"] = build_nav


def require_admin(request: Request):
    user = request.state.user
    if not user or not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin only")


def flash(request: Request, message: str, kind: str = "error"):
    """One-shot message shown on the next page that renders `flash` (see review.html)."""
    request.session["flash"] = {"kind": kind, "message": message}


def to_int(value: str | None) -> int | None:
    """Blank form inputs arrive as "" — treat them as missing."""
    value = (value or "").strip()
    return int(value) if value else None
