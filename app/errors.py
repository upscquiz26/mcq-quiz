"""Friendly error pages. A browser (which asks for text/html) gets a proper page inside the app's layout; anything else — the
autosave calls, scripts, tests — still gets the plain JSON error it always did."""
from fastapi.exception_handlers import http_exception_handler
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.web import templates

COPY = {
    400: ("That request couldn't be processed", "Something in it wasn't valid. Go back, check what you entered and try again."),
    403: ("You don't have access to this page", "This page is only for the administrator. If you think you should be able to see it, ask the admin."),
    404: ("We couldn't find that page", "The link may be out of date, or the item may have been archived, unpublished or removed."),
    405: ("That isn't allowed here", "This address can't be used in that way."),
    429: ("Too many attempts", "Please wait a little while and try again."),
}
DEFAULT = ("Something went wrong", "The page couldn't be shown. Try again, or go back to the start.")
GENERIC_DETAILS = {"Not Found", "Forbidden", "Method Not Allowed", "Admin only", "Not found"}


def install(app) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def friendly(request, exc):
        if "text/html" not in request.headers.get("accept", ""):
            return await http_exception_handler(request, exc)
        title, message = COPY.get(exc.status_code, DEFAULT)
        detail = exc.detail if isinstance(exc.detail, str) and exc.detail not in GENERIC_DETAILS else None
        return templates.TemplateResponse(
            "error.html",
            {"request": request, "status": exc.status_code, "title": title, "message": message, "detail": detail},
            status_code=exc.status_code, headers=getattr(exc, "headers", None),
        )
