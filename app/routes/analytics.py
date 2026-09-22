"""A student's own progress: accuracy, score trend, weak areas, and (for everyone) what official PYQs ask about most.

Open to any signed-in user. Everything about "you" is computed from the signed-in user's own answers; there are no
ids in the URL to change, so there is nothing to point at anyone else's data."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app import charts
from app.database import get_db
from app.practice import analytics
from app.practice import attempts as engine
from app.web import flash, format_duration, templates

router = APIRouter()

PRACTISE_COUNT = 10


@router.get("/analytics")
def analytics_page(request: Request, range: str = "all", db: Session = Depends(get_db)):
    user = request.state.user
    range_key = range if range in analytics.RANGES else "all"
    since = analytics.since_for(range_key)

    overview = analytics.overview(db, user.id, since)
    points = analytics.trend(db, user.id, since)
    subjects = analytics.by_subject(db, user.id, since)
    topics = analytics.by_topic(db, user.id, since)
    weak = analytics.weak_areas(db, user.id, since)
    frequency = analytics.topic_frequency(db)

    subject_rows, topic_rows = analytics.accuracy_chart_rows(subjects), analytics.accuracy_chart_rows(topics)
    trend_svg = None
    if len(points) >= 2:
        trend_svg = charts.line_chart(
            analytics.trend_chart_points(points),
            chart_id="trend", title="Score in each timed test",
            desc=f"Your score as a percentage of the maximum, for your last {len(points)} timed tests.")

    frequency_svg = None
    if frequency["rows"]:
        top = max(r["count"] for r in frequency["rows"])
        frequency_svg = charts.hbar_chart(
            [{"label": r["name"], "value": r["count"], "value_text": str(r["count"]), "highlight": False,
              "tip": analytics.tip(f"{r['count']} question{'s' if r['count'] != 1 else ''}",
                          f"{r['subject'] + ' · ' if r['subject'] else ''}{r['name']}")} for r in frequency["rows"]],
            chart_id="frequency", title="Questions per " + ("topic" if frequency["kind"] == "topics" else "subject"),
            desc="How many official PYQ questions each has.", max_value=top, accent_all=True)

    return templates.TemplateResponse(
        "analytics.html",
        {
            "request": request, "range_key": range_key, "ranges": {k: v[0] for k, v in analytics.RANGES.items()},
            "overview": overview, "points": points, "subjects": subjects, "topics": topics, "weak": weak,
            "trend_svg": trend_svg,
            "subject_svg": charts.hbar_chart(subject_rows, chart_id="subjects", title="Accuracy by subject",
                                             desc="Percentage of answers right in each subject.") if subject_rows else None,
            "topic_svg": charts.hbar_chart(topic_rows, chart_id="topics", title="Accuracy by topic",
                                           desc="Percentage of answers right in each topic.") if topic_rows else None,
            "frequency": frequency, "frequency_svg": frequency_svg,
            "min_sample": analytics.MIN_SAMPLE, "weak_below": analytics.WEAK_BELOW_PERCENT,
            "duration": format_duration, "practise_count": PRACTISE_COUNT,
            "flash": request.session.pop("flash", None),
        },
    )


@router.post("/analytics/practise-weak")
def practise_weak_areas(request: Request, range: str = Form("all"), db: Session = Depends(get_db)):
    """'Practice these': an untimed session drawn from the student's weakest topics (or subjects)."""
    user = request.state.user
    since = analytics.since_for(range if range in analytics.RANGES else "all")
    weak = analytics.weak_areas(db, user.id, since)
    if not weak["ids"]:
        flash(request, "Nothing is flagged as weak right now — there's no need to practise a particular area.", "notice")
        return RedirectResponse(url="/analytics", status_code=303)
    attempt = engine.create_weak_areas_attempt(db, user, weak["kind"], weak["ids"], PRACTISE_COUNT)
    if attempt is None:
        flash(request, "Those areas have no questions available to practise right now.")
        return RedirectResponse(url="/analytics", status_code=303)
    db.commit()
    return RedirectResponse(url=f"/attempts/{attempt.id}", status_code=303)
