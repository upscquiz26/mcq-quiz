"""What timed tests a student can start, built from live_questions() (so only live questions are ever offered)."""
from sqlalchemy import func

from app import models
from app.models import AttemptStatus
from app.practice import attempts as engine
from app.practice import pool


def _history(db, user_id: int, rank_keys: list[str]) -> dict:
    """For each rank key: how many times this user has sat it, and whether one is open right now."""
    rows = (
        db.query(models.Attempt.rank_key, models.Attempt.id, models.Attempt.status)
        .filter(models.Attempt.user_id == user_id, models.Attempt.rank_key.in_(rank_keys))
        .order_by(models.Attempt.id)
        .all()
    ) if rank_keys else []
    info: dict = {}
    for key, attempt_id, status in rows:
        entry = info.setdefault(key, {"taken": 0, "open_id": None})
        entry["taken"] += 1
        if status == AttemptStatus.IN_PROGRESS:
            entry["open_id"] = attempt_id
    return info


def full_tests(db, user_id: int) -> list[dict]:
    """One entry per paper that has live questions."""
    counts = dict(
        pool.live_questions(db).with_entities(models.Question.paper_id, func.count(models.Question.id))
        .group_by(models.Question.paper_id).all()
    )
    if not counts:
        return []
    papers = db.query(models.Paper).filter(models.Paper.id.in_(counts)).order_by(models.Paper.created_at.desc()).all()
    entries = []
    for paper in papers:
        ids = engine.paper_live_ids(db, paper.id)
        entries.append({
            "paper": paper, "count": counts[paper.id],
            "rank_key": f"paper:{paper.id}:full:{engine.question_set_fingerprint(ids)}",
            "minutes": engine.full_test_duration_seconds(paper, len(ids)) // 60,
            "scheme_ready": bool(paper.marks_per_question) and paper.negative_fraction is not None,
        })
    history = _history(db, user_id, [e["rank_key"] for e in entries])
    for e in entries:
        e.update(history.get(e["rank_key"], {"taken": 0, "open_id": None}))
    return entries


def sections(db, user_id: int) -> list[dict]:
    """One entry per (paper, subject) that has live questions, grouped by paper for the page."""
    rows = (
        pool.live_questions(db)
        .join(models.Subject, models.Subject.id == models.Question.subject_id)
        .with_entities(models.Paper.id, models.Paper.title, models.Subject.id, models.Subject.name,
                       func.count(models.Question.id))
        .group_by(models.Paper.id, models.Paper.title, models.Subject.id, models.Subject.name)
        .order_by(models.Paper.id.desc(), models.Subject.id)
        .all()
    )
    entries = []
    for paper_id, title, subject_id, subject, count in rows:
        ids = engine.paper_live_ids(db, paper_id, subject_id)
        entries.append({
            "paper_id": paper_id, "paper": title, "subject_id": subject_id, "subject": subject, "count": count,
            "minutes": max(1, -(-len(ids) * engine.SECONDS_PER_QUESTION // 60)),
            "rank_key": f"paper:{paper_id}:subject:{subject_id}:{engine.question_set_fingerprint(ids)}",
        })
    history = _history(db, user_id, [e["rank_key"] for e in entries])
    for e in entries:
        e.update(history.get(e["rank_key"], {"taken": 0, "open_id": None}))
    return entries
