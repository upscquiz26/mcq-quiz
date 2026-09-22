"""
A student's own numbers: accuracy by subject and topic, score trend, time per answer, weak areas, recent tests —
plus one global view (which topics appear most in official PYQs).

Everything except topic_frequency() is computed from ONE user's graded responses; there is no way to ask for
someone else's. Only graded answers count (topic/mistake practice answers as they are given; tests once finished).

Definitions
  * accuracy        right answers / answered questions
  * weak            accuracy below WEAK_BELOW_PERCENT with at least MIN_SAMPLE answers (fewer is too little to judge)
  * average time    the mean over answers where a time was recorded
  * score trend     each finished timed test as a percentage of its maximum marks (can be negative)
"""
from datetime import datetime, timedelta

from sqlalchemy import func

from app import models
from app.models import AttemptKind, AttemptStatus
from app.practice import grading, pool

WEAK_BELOW_PERCENT = 60
MIN_SAMPLE = grading.MIN_SAMPLE
MAX_TREND_POINTS = 30
MAX_BARS = 15
RANGES = {"all": ("All time", None), "30": ("Last 30 days", 30), "90": ("Last 90 days", 90)}


def since_for(range_key: str, now: datetime | None = None) -> datetime | None:
    days = RANGES.get(range_key, RANGES["all"])[1]
    return (now or datetime.utcnow()) - timedelta(days=days) if days else None


def _percent(part: int, whole: int) -> int | None:
    return round(100 * part / whole) if whole else None


def graded_rows(db, user_id: int, since: datetime | None = None) -> list:
    """(is_correct, seconds, subject_id, topic_id) for each graded answer of this user."""
    query = (
        db.query(models.Response.is_correct, models.Response.time_spent_seconds,
                 models.Question.subject_id, models.Question.topic_id)
        .join(models.Attempt, models.Attempt.id == models.Response.attempt_id)
        .join(models.Question, models.Question.id == models.Response.question_id)
        .filter(models.Attempt.user_id == user_id, models.Response.selected_answer.isnot(None),
                models.Response.is_correct.isnot(None))
    )
    if since is not None:
        query = query.filter(models.Attempt.started_at >= since)
    return query.all()


def _mean_time(times) -> float | None:
    times = [t for t in times if t]
    return sum(times) / len(times) if times else None


def timed_attempts(db, user_id: int, since: datetime | None = None) -> list[models.Attempt]:
    """Finished timed tests that have a maximum to compare against, oldest first."""
    query = (
        db.query(models.Attempt)
        .filter(models.Attempt.user_id == user_id, models.Attempt.kind.in_(AttemptKind.TIMED),
                models.Attempt.status != AttemptStatus.IN_PROGRESS, models.Attempt.max_marks > 0,
                models.Attempt.score.isnot(None))
        .order_by(models.Attempt.completed_at, models.Attempt.id)
    )
    if since is not None:
        query = query.filter(models.Attempt.started_at >= since)
    return query.all()


def overview(db, user_id: int, since: datetime | None = None) -> dict:
    rows = graded_rows(db, user_id, since)
    tests = timed_attempts(db, user_id, since)
    right = sum(1 for correct, *_ in rows if correct)
    scores = [100 * a.score / a.max_marks for a in tests]
    average_time = _mean_time([seconds for _, seconds, *_ in rows])
    return {
        "answered": len(rows), "right": right, "accuracy": _percent(right, len(rows)),
        "tests_taken": len(tests),
        "average_score": round(sum(scores) / len(scores), 1) if scores else None,
        "average_seconds": round(average_time) if average_time is not None else None,
    }


def trend(db, user_id: int, since: datetime | None = None) -> list[dict]:
    """The most recent finished timed tests as percentages, oldest first."""
    papers = {p.id: p.title for p in db.query(models.Paper).all()}
    points = []
    for a in timed_attempts(db, user_id, since)[-MAX_TREND_POINTS:]:
        points.append({
            "attempt_id": a.id, "when": a.completed_at or a.started_at,
            "percent": round(100 * a.score / a.max_marks, 1), "score": a.score, "max": a.max_marks,
            "label": papers.get(a.paper_id) or models.AttemptKind.LABELS.get(a.kind, a.kind),
            "kind": models.AttemptKind.LABELS.get(a.kind, a.kind),
        })
    return points


def _group(rows, key_index: int, names: dict, missing_label: str) -> list[dict]:
    groups: dict = {}
    for row in rows:
        g = groups.setdefault(row[key_index], {"answered": 0, "right": 0, "times": []})
        g["answered"] += 1
        g["right"] += 1 if row[0] else 0
        g["times"].append(row[1])
    result = []
    for key, g in groups.items():
        accuracy = _percent(g["right"], g["answered"])
        result.append({
            "id": key, "name": names.get(key, missing_label), "answered": g["answered"], "right": g["right"],
            "accuracy": accuracy, "avg_seconds": _mean_time(g["times"]),
            "weak": g["answered"] >= MIN_SAMPLE and accuracy < WEAK_BELOW_PERCENT,
            "enough": g["answered"] >= MIN_SAMPLE,
        })
    # Weakest first; groups with too few answers to judge go last.
    result.sort(key=lambda r: (not r["enough"], r["accuracy"], -r["answered"], r["name"]))
    return result


def by_subject(db, user_id: int, since: datetime | None = None) -> list[dict]:
    names = {s.id: s.name for s in db.query(models.Subject).all()}
    return _group(graded_rows(db, user_id, since), 2, names, "No subject set")


def by_topic(db, user_id: int, since: datetime | None = None) -> list[dict]:
    names = {t.id: t.name for t in db.query(models.Topic).all()}
    rows = [r for r in graded_rows(db, user_id, since) if r[3] is not None]      # questions with no topic aren't topics
    subject_of = {t.id: t.subject_id for t in db.query(models.Topic).all()}
    subject_names = {s.id: s.name for s in db.query(models.Subject).all()}
    groups = _group(rows, 3, names, "Unknown topic")
    for g in groups:
        g["subject"] = subject_names.get(subject_of.get(g["id"]), "")
    return groups


def weak_areas(db, user_id: int, since: datetime | None = None, limit: int = 3) -> dict:
    """The areas to work on: weak TOPICS if any topic is weak, otherwise weak SUBJECTS (topics are optional data)."""
    topics = [t for t in by_topic(db, user_id, since) if t["weak"]][:limit]
    if topics:
        return {"kind": "topics", "items": topics, "ids": [t["id"] for t in topics]}
    subjects = [s for s in by_subject(db, user_id, since) if s["weak"] and s["id"] is not None][:limit]
    if subjects:
        return {"kind": "subjects", "items": subjects, "ids": [s["id"] for s in subjects]}
    return {"kind": None, "items": [], "ids": []}


def tip(value: str, detail: str) -> str:
    """Tooltip text for a chart mark: the value leads, the detail follows (charts.js splits on ' | ')."""
    return f"{value} | {detail}"


def accuracy_chart_rows(groups: list[dict]) -> list[dict]:
    """Bar-chart rows for subject or topic accuracy: weak ones highlighted, groups with too few answers left out
    (they stay in the table)."""
    rows = []
    for g in groups:
        if not g["enough"]:
            continue
        where = f"{g['subject']} · " if g.get("subject") else ""
        rows.append({
            "label": g["name"], "value": g["accuracy"],
            "value_text": f"{g['accuracy']}%" + (" · weak" if g["weak"] else ""), "highlight": g["weak"],
            "tip": tip(f"{g['accuracy']}% right", f"{where}{g['name']} · {g['right']} of {g['answered']} answered"
                       + (" · weak" if g["weak"] else "")),
        })
    return rows


def trend_chart_points(points: list[dict], link_to_results: bool = True) -> list[dict]:
    """Line-chart points for the score trend. The student's chart links each point to its results page; the admin's
    read-only view can't (results belong to the student), so it passes link_to_results=False."""
    return [{"x_label": p["when"].strftime("%d %b"), "value": p["percent"],
             "href": f"/attempts/{p['attempt_id']}/result" if link_to_results else None,
             "tip": tip(f"{p['percent']:g}%", f"{p['label']} · {p['when'].strftime('%d %b %Y')}")} for p in points]


def recent_tests(db, user_id: int, limit: int = 5) -> list[dict]:
    """The latest finished timed tests, newest first, for the home page."""
    return list(reversed(trend(db, user_id)))[:limit]


def topic_frequency(db) -> dict:
    """How many LIVE official-PYQ questions each topic has (the topics that come up most). Falls back to subjects
    when the admin hasn't assigned topics yet. This is about the question bank, not about any student."""
    official = pool.live_questions(db).filter(models.Paper.source_type == "official_pyq")
    topic_rows = (
        official.join(models.Topic, models.Topic.id == models.Question.topic_id)
        .join(models.Subject, models.Subject.id == models.Topic.subject_id)
        .with_entities(models.Topic.id, models.Topic.name, models.Subject.name, func.count(models.Question.id))
        .group_by(models.Topic.id, models.Topic.name, models.Subject.name)
        .order_by(func.count(models.Question.id).desc(), models.Topic.name).limit(MAX_BARS).all()
    )
    if topic_rows:
        return {"kind": "topics", "total": official.count(),
                "rows": [{"id": i, "name": n, "subject": s, "count": c} for i, n, s, c in topic_rows]}
    subject_rows = (
        official.join(models.Subject, models.Subject.id == models.Question.subject_id)
        .with_entities(models.Subject.id, models.Subject.name, func.count(models.Question.id))
        .group_by(models.Subject.id, models.Subject.name)
        .order_by(func.count(models.Question.id).desc(), models.Subject.name).limit(MAX_BARS).all()
    )
    return {"kind": "subjects" if subject_rows else None, "total": official.count(),
            "rows": [{"id": i, "name": n, "subject": "", "count": c} for i, n, c in subject_rows]}
