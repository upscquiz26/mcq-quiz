"""The admin dashboard: headline numbers, what needs attention, recent activity, and the switches that control student-facing features."""
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from app import audit, models, settings
from app.database import get_db
from app.models import AttemptKind, AttemptStatus, QStatus
from app.practice import reports, suspicion
from app.web import flash, require_admin, templates

router = APIRouter(dependencies=[Depends(require_admin)])

# URL name -> (setting key, what it is called in messages)
SWITCHES = {
    "user-performance": ("user_performance_enabled", "User performance view"),
    "leaderboard": ("leaderboard_enabled", "Leaderboard"),
}

ATTENTION_PAPERS = 5
RECENT_ACTIVITY = 8
WEEK = timedelta(days=7)
QUIET_ACTIONS = ("user.login", "user.logout", "performance.view")      # routine noise: not worth a place in "recent activity"

# Readable names for the audit-log actions an admin sees on the dashboard; anything else is tidied automatically.
ACTION_LABELS = {
    "paper.upload": "Uploaded a paper", "paper.import_done": "Finished reading a paper", "paper.import_failed": "A paper failed to import",
    "paper.json_import": "Imported questions from JSON", "paper.publish": "Published a paper", "paper.unpublish": "Unpublished a paper",
    "paper.archive": "Archived a paper", "paper.unarchive": "Restored a paper", "paper.delete": "Deleted a paper", "paper.settings": "Changed a paper's marking scheme",
    "question.edit": "Edited a question", "question.confirm": "Confirmed a question", "question.confirm_bulk": "Confirmed questions in bulk",
    "question.add": "Added a question", "question.delete": "Deleted a question",
    "question.quarantine": "Quarantined a question", "question.restore": "Restored a question", "question.reopen": "Sent a question back to review",
    "question.reopen_bulk": "Sent a paper back to review", "question.demote": "A live question went back to review",
    "user.approved": "Approved an account", "user.rejected": "Rejected an account", "user.deactivated": "Deactivated an account",
    "user.reactivated": "Reactivated an account", "user.password_reset": "Reset a password", "user.password_change": "Changed a password",
    "report.resolve": "Resolved a question report", "report.dismiss": "Dismissed a question report",
    "settings.change": "Changed a setting", "backup.create": "Made a backup", "performance.view": "Viewed a student's performance",
    "subjects.bulk_set": "Set subjects on a paper", "subjects.bulk_assign": "Assigned a subject to selected questions",
    "suspicion.analyse": "Analysed answers for suspicious keys", "suspicion.dismiss": "Kept an answer key students questioned",
    "duplicate.merge": "Merged a duplicate question", "duplicate.keep_both": "Kept two similar questions", "duplicates.scan": "Looked for duplicate questions",
}


def action_label(action: str) -> str:
    return ACTION_LABELS.get(action) or action.replace(".", " ").replace("_", " ").capitalize()


def paper_review_state(db: Session) -> list[dict]:
    """Every live (not archived, finished-reading) paper with how far its review has got."""
    rows = []
    for paper in db.query(models.Paper).filter(models.Paper.archived_at.is_(None)).order_by(models.Paper.created_at.desc()).all():
        active = [q for q in paper.questions if q.status != QStatus.QUARANTINED]
        to_confirm = sum(1 for q in active if q.status in (QStatus.DRAFT, QStatus.NEEDS_REVIEW))
        rows.append({"paper": paper, "total": len(active), "to_confirm": to_confirm, "confirmed": len(active) - to_confirm})
    return rows


@router.get("/admin")
def dashboard(request: Request, db: Session = Depends(get_db)):
    now = datetime.utcnow()
    user_counts = dict(
        db.query(models.User.status, func.count()).filter(models.User.is_admin.is_(False))
        .group_by(models.User.status).all()
    )
    papers = paper_review_state(db)
    ready = [p for p in papers if p["paper"].status == "ready"]
    failed = [p for p in papers if p["paper"].status == "failed"]
    awaiting = [p for p in ready if p["to_confirm"]]
    to_publish = [p for p in ready if p["total"] and not p["to_confirm"] and p["paper"].publish_status != "published"]
    open_reports = reports.open_count(db)
    pending = user_counts.get(models.UserStatus.pending, 0)

    active_students = db.query(models.User).filter(
        models.User.is_admin.is_(False), models.User.status == models.UserStatus.approved,
        models.User.last_active_at >= now - WEEK).count()
    tests_this_week = db.query(models.Attempt).filter(
        models.Attempt.kind.in_(AttemptKind.TIMED), models.Attempt.status != AttemptStatus.IN_PROGRESS,
        models.Attempt.started_at >= now - WEEK).count()
    live_questions = db.query(models.Question).filter(models.Question.status == QStatus.LIVE).count()

    attention = []
    if pending:
        attention.append({"tone": "warm", "icon": "users", "text": f"{pending} account{'s' if pending != 1 else ''} waiting for approval",
                          "href": "/admin/users", "action": "Review requests"})
    if open_reports:
        attention.append({"tone": "red", "icon": "flag", "text": f"{open_reports} reported question{'s' if open_reports != 1 else ''} to look at",
                          "href": "/admin/reports", "action": "Open the queue"})
    open_suspicious = suspicion.open_count(db)
    if open_suspicious:
        attention.append({"tone": "red", "icon": "alert", "text": f"{open_suspicious} live question{'s' if open_suspicious != 1 else ''} where strong students disagree with the key",
                          "href": "/admin/suspicious", "action": "Check the keys"})
    for p in failed:
        attention.append({"tone": "red", "icon": "alert", "text": f"“{p['paper'].title}” failed to import",
                          "href": f"/review/{p['paper'].id}", "action": "See why"})
    for p in awaiting[:ATTENTION_PAPERS]:
        attention.append({"tone": "blue", "icon": "file", "text": f"“{p['paper'].title}”: {p['to_confirm']} of {p['total']} questions to confirm",
                          "href": f"/review/{p['paper'].id}?show=to_confirm", "action": "Review"})
    for p in to_publish[:ATTENTION_PAPERS]:
        attention.append({"tone": "green", "icon": "check", "text": f"“{p['paper'].title}” is fully confirmed but not published",
                          "href": f"/review/{p['paper'].id}", "action": "Publish"})

    recent = [
        {"who": e.username or "system", "what": action_label(e.action), "at": e.at, "paper_id": e.paper_id}
        for e in db.query(models.AuditLog).filter(models.AuditLog.action.notin_(QUIET_ACTIONS))
        .order_by(models.AuditLog.id.desc()).limit(RECENT_ACTIVITY).all()
    ]
    paper_query = db.query(models.Paper).filter(models.Paper.archived_at.is_(None))
    return templates.TemplateResponse(
        "admin_dashboard.html",
        {
            "request": request,
            "pending": pending,
            "approved": user_counts.get(models.UserStatus.approved, 0),
            "deactivated": user_counts.get(models.UserStatus.deactivated, 0),
            "papers_published": paper_query.filter(models.Paper.publish_status == "published").count(),
            "papers_draft": paper_query.filter(models.Paper.publish_status != "published").count(),
            "open_reports": open_reports,
            "to_confirm_total": sum(p["to_confirm"] for p in ready),
            "papers_awaiting": len(awaiting),
            "active_students": active_students, "tests_this_week": tests_this_week, "live_questions": live_questions,
            "attention": attention, "recent": recent,
            "performance_on": settings.get_bool(db, "user_performance_enabled"),
            "leaderboard_on": settings.get_bool(db, "leaderboard_enabled"),
            "flash": request.session.pop("flash", None),
        },
    )


@router.post("/admin/settings/{name}")
def change_setting(request: Request, name: str, enabled: str = Form(""), db: Session = Depends(get_db)):
    if name not in SWITCHES:
        raise HTTPException(status_code=404, detail="Unknown setting")
    key, label = SWITCHES[name]
    value = enabled == "1"
    settings.set_bool(db, key, value, request.state.user)
    audit.log(db, request.state.user, "settings.change", "setting", None, detail={"setting": key, "enabled": value})
    db.commit()
    flash(request, f"{label} is now {'on' if value else 'off'}.", "notice")
    return RedirectResponse(url="/admin", status_code=303)
