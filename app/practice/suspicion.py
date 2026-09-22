"""
Suspicious-answer detection: a wrong key shows itself in how the strongest students answer.

For every LIVE question the detector looks at the answers students gave and asks: among the high scorers, did most of them choose a different
option from the one in the key? If so the question goes into the admin's queue (/admin/suspicious) as a hint that the key — or the question —
may be wrong. It changes nothing by itself: no answer is edited, no question hidden, no result re-graded.

The rules (all constants below, all covered by tests on synthetic responses)

  * Only finished attempts count (submitted or expired), only students (never the admin), and only a student's FIRST answer to a question, so
    practising the same question again can't stack the numbers. Skipped questions are ignored.
  * A student is *qualified* once they have answered at least MIN_ANSWERS questions in all. Their ability is then measured as their accuracy on
    every OTHER question (leave-one-out, so the question being judged can't influence who counts as strong), against the keys as they stand now.
  * A question needs at least MIN_RESPONDERS qualified students. The high scorers are the top HIGH_SHARE of them by that accuracy (ties at the
    boundary are all included), and there must be at least MIN_HIGH of them.
  * Suspicious means: the most popular option among the high scorers is NOT the key, more than half of them chose it, and it beats the key's
    count by at least MARGIN students.

Queue lifecycle. A question that stops being suspicious leaves the queue. "The key is right" (dismiss) is remembered against the key it was
judged on; if the key later changes the question can come back. Sending a question back to review is remembered the same way: if it is
confirmed again with the same key it counts as dismissed.
"""
import json
from collections import Counter, defaultdict
from datetime import datetime

from app import models
from app.models import AttemptStatus, QStatus

MIN_ANSWERS = 20
MIN_RESPONDERS = 12
HIGH_SHARE = 0.25
MIN_HIGH = 5
MARGIN = 2
LETTERS = ("A", "B", "C", "D")


def first_answers(db) -> dict[int, dict[int, str]]:
    """{user id: {question id: the option they chose first}} from finished attempts of students."""
    rows = (db.query(models.Attempt.user_id, models.Response.question_id, models.Response.selected_answer)
            .join(models.Response, models.Response.attempt_id == models.Attempt.id)
            .join(models.User, models.User.id == models.Attempt.user_id)
            .filter(models.Attempt.status.in_((AttemptStatus.SUBMITTED, AttemptStatus.EXPIRED)),
                    models.Response.selected_answer.in_(LETTERS), models.User.is_admin.is_(False))
            .order_by(models.Attempt.id, models.Response.position))
    answers: dict[int, dict[int, str]] = defaultdict(dict)
    for user_id, question_id, letter in rows:
        answers[user_id].setdefault(question_id, letter)               # the first answer wins
    return answers


def analyse_question(key: str, chosen: dict[int, str], accuracy_without: dict[int, float]) -> dict | None:
    """The verdict for one question, from synthetic or real data.

    key: the answer in the key; chosen: {user: option they chose}; accuracy_without: {user: accuracy on their other questions}
    (qualified users only). Returns the evidence when the question is suspicious, else None."""
    users = [u for u in chosen if u in accuracy_without]
    if len(users) < MIN_RESPONDERS:
        return None
    ranked = sorted(users, key=lambda u: (-accuracy_without[u], u))
    k = max(MIN_HIGH, _ceil(len(ranked) * HIGH_SHARE))
    if len(ranked) < k:
        return None
    cutoff = accuracy_without[ranked[k - 1]]
    high = [u for u in ranked if accuracy_without[u] >= cutoff]           # ties at the boundary are all in
    if len(high) < MIN_HIGH:
        return None
    high_counts = Counter(chosen[u] for u in high)
    popular, popular_n = max(high_counts.items(), key=lambda item: (item[1], item[0] == key))
    if popular == key or popular_n * 2 <= len(high) or popular_n - high_counts.get(key, 0) < MARGIN:
        return None
    all_counts = Counter(chosen[u] for u in users)
    return {"popular": popular, "high_n": len(high), "high_counts": dict(high_counts), "all_n": len(users), "all_counts": dict(all_counts)}


def _ceil(x: float) -> int:
    n = int(x)
    return n if n == x else n + 1


def analyse(db) -> dict:
    """Work the whole queue out from the responses so far and store it. Returns {"open", "new", "cleared", "checked"} counts."""
    answers = first_answers(db)
    qualified = {u: qs for u, qs in answers.items() if len(qs) >= MIN_ANSWERS}
    live = {q.id: q for q in db.query(models.Question).filter(models.Question.status == QStatus.LIVE)
            if q.correct_answer in LETTERS}
    totals: dict[int, tuple[int, int]] = {}                               # user -> (right, answered) against today's keys, live questions only
    for user_id, qs in qualified.items():
        totals[user_id] = (sum(1 for qid, letter in qs.items() if qid in live and letter == live[qid].correct_answer),
                           sum(1 for qid in qs if qid in live))
    by_question: dict[int, dict[int, str]] = defaultdict(dict)
    for user_id in qualified:
        for qid, letter in answers[user_id].items():
            if qid in live:
                by_question[qid][user_id] = letter

    now = datetime.utcnow()
    existing = {s.question_id: s for s in db.query(models.AnswerSuspicion)}
    result = {"open": 0, "new": 0, "cleared": 0, "checked": 0}
    for qid, chosen in by_question.items():
        result["checked"] += 1
        q = live[qid]
        accuracy: dict[int, float] = {}
        for user_id, letter in chosen.items():
            right, counted = totals[user_id]
            if counted >= 2:
                accuracy[user_id] = (right - (1 if letter == q.correct_answer else 0)) / (counted - 1)
        verdict = analyse_question(q.correct_answer, chosen, accuracy)
        row = existing.pop(qid, None)
        if verdict is None:
            if row is not None and row.status != "sent_back":
                db.delete(row)
                result["cleared"] += 1
            continue
        fields = dict(key_answer=q.correct_answer, popular_answer=verdict["popular"], high_n=verdict["high_n"],
                      high_counts_json=json.dumps(verdict["high_counts"]), all_n=verdict["all_n"],
                      all_counts_json=json.dumps(verdict["all_counts"]), computed_at=now)
        if row is None:
            db.add(models.AnswerSuspicion(question_id=qid, status="open", **fields))
            result["new"] += 1
            result["open"] += 1
            continue
        for name, value in fields.items():
            setattr(row, name, value)
        if row.status in ("dismissed", "sent_back") and row.handled_key == q.correct_answer:
            row.status = "dismissed"                                    # judged on this very key already
        else:
            if row.status != "open":
                result["new"] += 1
            row.status = "open"                                         # the key has changed since: look again
            result["open"] += 1
    for row in existing.values():                                       # no responses counted any more, or it left circulation
        if row.status == "open":
            db.delete(row)
            result["cleared"] += 1
    db.flush()
    return result


def open_count(db) -> int:
    return db.query(models.AnswerSuspicion).filter_by(status="open").count()


def dismiss(db, user, row: models.AnswerSuspicion) -> None:
    q = db.get(models.Question, row.question_id)
    row.status, row.handled_key, row.handled_by, row.handled_at = "dismissed", q.correct_answer, user.id if user else None, datetime.utcnow()


def mark_sent_back(db, user, row: models.AnswerSuspicion) -> None:
    q = db.get(models.Question, row.question_id)
    row.status, row.handled_key, row.handled_by, row.handled_at = "sent_back", q.correct_answer, user.id if user else None, datetime.utcnow()
