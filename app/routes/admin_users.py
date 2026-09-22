"""Admin: approve, reject, deactivate and remove accounts; reset a user's password."""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app import audit, auth, models, throttle
from app.database import get_db
from app.web import flash, require_admin, templates

router = APIRouter(dependencies=[Depends(require_admin)])

Status = models.UserStatus


@router.get("/admin/users")
def admin_users(request: Request, db: Session = Depends(get_db)):
    users = db.query(models.User).order_by(models.User.created_at).all()
    by_status = {status: [u for u in users if u.status == status and not u.is_admin] for status in Status}
    return templates.TemplateResponse(
        "admin_users.html",
        {
            "request": request,
            "admins": [u for u in users if u.is_admin],
            "pending": by_status[Status.pending],
            "approved": by_status[Status.approved],
            "deactivated": by_status[Status.deactivated],
            "rejected": by_status[Status.rejected],
            "flash": request.session.pop("flash", None),
        },
    )


def _managed_user(user_id: int, db: Session) -> models.User:
    target = db.get(models.User, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    if target.is_admin:
        raise HTTPException(status_code=400, detail="Admin accounts can't be changed here")
    return target


def _decide(request: Request, user_id: int, status: Status, db: Session, action: str | None = None):
    target = _managed_user(user_id, db)
    target.status = status
    target.decided_at = datetime.utcnow()
    target.decided_by = request.state.user.id
    audit.log(db, request.state.user, action or f"user.{status.value}", "user", target.id,
              detail={"username": target.username})
    db.commit()
    return RedirectResponse(url="/admin/users", status_code=303)


@router.post("/admin/users/{user_id}/approve")
def approve_user(request: Request, user_id: int, db: Session = Depends(get_db)):
    return _decide(request, user_id, Status.approved, db)


@router.post("/admin/users/{user_id}/reject")
def reject_user(request: Request, user_id: int, db: Session = Depends(get_db)):
    return _decide(request, user_id, Status.rejected, db)


@router.post("/admin/users/{user_id}/deactivate")
def deactivate_user(request: Request, user_id: int, db: Session = Depends(get_db)):
    """Switches an approved account off. Their history is kept and they are signed out on their next click."""
    target = _managed_user(user_id, db)
    if target.status != Status.approved:
        raise HTTPException(status_code=400, detail="Only approved accounts can be deactivated")
    return _decide(request, user_id, Status.deactivated, db, action="user.deactivate")


@router.post("/admin/users/{user_id}/reactivate")
def reactivate_user(request: Request, user_id: int, db: Session = Depends(get_db)):
    target = _managed_user(user_id, db)
    if target.status != Status.deactivated:
        raise HTTPException(status_code=400, detail="Only deactivated accounts can be reactivated")
    return _decide(request, user_id, Status.approved, db, action="user.reactivate")


@router.post("/admin/users/{user_id}/remove")
def remove_user(request: Request, user_id: int, db: Session = Depends(get_db)):
    target = _managed_user(user_id, db)
    audit.log(db, request.state.user, "user.remove", "user", target.id, detail={"username": target.username})
    db.delete(target)
    db.commit()
    return RedirectResponse(url="/admin/users", status_code=303)


@router.post("/admin/users/{user_id}/reset-password")
def reset_password(request: Request, user_id: int, db: Session = Depends(get_db)):
    """Gives the user a temporary password, shown to the admin ONCE (in this response only).

    The user must choose their own password at next login, every session they have is ended,
    and any login lockout on the account is lifted. The temporary password is never stored in
    plain text, in the session, or in the audit log — and no existing password is ever shown.
    """
    target = _managed_user(user_id, db)
    if target.status not in (Status.approved, Status.deactivated):
        raise HTTPException(status_code=400, detail="Only approved or deactivated accounts have a password to reset")

    temp = auth.generate_temp_password()
    target.password_hash = auth.hash_password(temp)
    target.must_change_password = True
    target.password_changed_at = datetime.utcnow()
    target.session_version += 1                       # every existing session of theirs is now invalid
    throttle.clear(db, throttle.key_for_username(target.username))
    audit.log(db, request.state.user, "user.password_reset", "user", target.id,
              detail={"username": target.username})   # deliberately no password here
    db.commit()

    response = templates.TemplateResponse(
        "password_reset_done.html", {"request": request, "target": target, "temp_password": temp}
    )
    response.headers["Cache-Control"] = "no-store"
    return response
