"""Admin: how each student is doing. Strictly read-only.

Two pages — a list of approved students and one student's numbers. There are no forms and no POST routes here, so
nothing in this module can change, delete or re-grade an attempt, and no page shows a password or hash.

Both pages are refused (redirect to the dashboard) while the admin's "User performance" switch is off, and students are
told about this view in their profile only while it is on. The numbers come from the same functions the student's own
Progress page uses, so the admin sees exactly what the student sees."""
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from app import audit, charts, models, settings
from app.database import get_db
from app.practice import analytics
from app.web import flash, format_duration, require_admin, templates

router = APIRouter(dependencies=[Depends(require_admin)])

MAX_LISTED_ATTEMPTS = 50


def _switched_off(request: Request):
    flash(request, "The user performance view is switched off. Turn it on from the dashboard to use it.", "notice")
    return RedirectResponse(url="/admin", status_code=303)


@router.get("/admin/performance")
def performance_list(request: Request, db: Session = Depends(get_db)):
    if not settings.get_bool(db, "user_performance_enabled"):
        return _switched_off(request)
    students = (
        db.query(models.User)
        .filter(models.User.is_admin.is_(False), models.User.status == models.UserStatus.approved).all()
    )
    rows = []
    for student in students:
        overview = analytics.overview(db, student.id)
        weak = analytics.weak_areas(db, student.id)
        rows.append({
            "user": student, "overview": overview,
            "weak_kind": weak["kind"], "weak_names": [w["name"] for w in weak["items"]],
        })
    # Most recently active first; people who have never been active go last.
    rows.sort(key=lambda r: (r["user"].last_active_at is None, -(r["user"].last_active_at.timestamp()
                                                                    if r["user"].last_active_at else 0),
                             r["user"].username))
    return templates.TemplateResponse(
        "admin_performance.html",
        {"request": request, "rows": rows, "duration": format_duration, "flash": request.session.pop("flash", None)},
    )


@router.get("/admin/performance/{user_id}")
def performance_detail(request: Request, user_id: int, range: str = "all", db: Session = Depends(get_db)):
    if not settings.get_bool(db, "user_performance_enabled"):
        return _switched_off(request)
    student = db.get(models.User, user_id)
    if not student or student.is_admin or student.status != models.UserStatus.approved:
        raise HTTPException(status_code=404, detail="Student not found")

    range_key = range if range in analytics.RANGES else "all"
    since = analytics.since_for(range_key)
    overview = analytics.overview(db, student.id, since)
    points = analytics.trend(db, student.id, since)
    subjects = analytics.by_subject(db, student.id, since)
    topics = analytics.by_topic(db, student.id, since)
    weak = analytics.weak_areas(db, student.id, since)

    subject_rows, topic_rows = analytics.accuracy_chart_rows(subjects), analytics.accuracy_chart_rows(topics)
    name = student.display_name or student.username
    trend_svg = None
    if len(points) >= 2:
        trend_svg = charts.line_chart(
            analytics.trend_chart_points(points, link_to_results=False),
            chart_id="trend", title=f"Score in each timed test — {name}",
            desc=f"{name}'s score as a percentage of the maximum, for their last {len(points)} timed tests.")

    papers = {p.id: p.title for p in db.query(models.Paper).all()}
    attempts = (
        db.query(models.Attempt).filter(models.Attempt.user_id == student.id)
        .order_by(models.Attempt.started_at.desc(), models.Attempt.id.desc()).limit(MAX_LISTED_ATTEMPTS).all()
    )
    answered = dict(
        db.query(models.Response.attempt_id, func.count()).filter(
            models.Response.attempt_id.in_([a.id for a in attempts]), models.Response.selected_answer.isnot(None))
        .group_by(models.Response.attempt_id).all()
    ) if attempts else {}
    attempt_total = db.query(models.Attempt).filter(models.Attempt.user_id == student.id).count()

    audit.log(db, request.state.user, "performance.view", "user", student.id, detail={"username": student.username})
    db.commit()
    return templates.TemplateResponse(
        "admin_performance_detail.html",
        {
            "request": request, "student": student, "name": name, "range_key": range_key,
            "ranges": {k: v[0] for k, v in analytics.RANGES.items()},
            "overview": overview, "points": points, "weak": weak, "subjects": subjects, "topics": topics,
            "trend_svg": trend_svg,
            "subject_svg": charts.hbar_chart(subject_rows, chart_id="subjects", title=f"Accuracy by subject — {name}",
                                             desc="Percentage of answers right in each subject.") if subject_rows else None,
            "topic_svg": charts.hbar_chart(topic_rows, chart_id="topics", title=f"Accuracy by topic — {name}",
                                           desc="Percentage of answers right in each topic.") if topic_rows else None,
            "attempts": attempts, "answered": answered, "attempt_total": attempt_total, "papers": papers,
            "kinds": models.AttemptKind.LABELS,
            "min_sample": analytics.MIN_SAMPLE, "weak_below": analytics.WEAK_BELOW_PERCENT, "duration": format_duration,
        },
    )
