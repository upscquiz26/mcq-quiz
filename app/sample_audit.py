"""
The sample audit: a check on the reviewer, not on the paper.

"Confirm" is a claim that the admin compared a question with the printed original. Questions confirmed WITHOUT any edit are the ones
that could have been waved through, so a random sample of them is checked against the original again:

  * sample size: 10% of the unedited confirmed questions, and at least 5 (or all of them if there are fewer);
  * the admin marks each picked question "correct" or "wrong" against the printed page;
  * a question marked wrong goes straight back to review;
  * 3 or more wrong: the audit FAILED — every question that was confirmed without an edit goes back to review for a second pass;
  * fewer than 3 wrong: the audit PASSED.

Publishing is blocked while an audit is pending or failed, and also when a paper has AUDIT_MIN or more unedited confirmed questions
and has not passed an audit. Anything that changes what has been confirmed (a new confirmation, sending a question back to review,
a key that changes answers) invalidates a passed audit, so it can't vouch for questions it never saw.

Paper.audit_state: none | pending | passed | failed.  Paper.audit_round counts audits started.
"""
import math
import random

from sqlalchemy import func

from app import models
from app.models import QStatus

AUDIT_FRACTION = 0.10
AUDIT_MIN = 5
AUDIT_FAIL_AT = 3
CONFIRMED = (QStatus.VERIFIED, QStatus.LIVE)
CONTENT_FIELDS = ("text", "option_a", "option_b", "option_c", "option_d", "correct_answer", "has_image",
                  "question_hi", "option_a_hi", "option_b_hi", "option_c_hi", "option_d_hi")
FILING_REASONS = ("subject/topic/difficulty", "bulk subject change", "subject suggestion accepted")   # version reasons that aren't content edits


def _active(paper: models.Paper) -> list[models.Question]:
    return [q for q in paper.questions if q.status != QStatus.QUARANTINED]


def edited_ids(db, paper: models.Paper) -> set[int]:
    ids = [q.id for q in paper.questions]
    if not ids:
        return set()
    # Only a change to what the question SAYS (text, options, answer, image) exempts it from the audit. Re-reading pages and
    # filing a question under a subject/topic/difficulty are not edits of the content, so they don't.
    reason = func.coalesce(models.QuestionVersion.reason, "")
    rows = (db.query(models.QuestionVersion.question_id)
            .filter(models.QuestionVersion.question_id.in_(ids), ~reason.like("pages % read again"), reason.notin_(FILING_REASONS))
            .distinct())
    return {row[0] for row in rows}


def unedited_confirmed(db, paper: models.Paper) -> list[models.Question]:
    """Confirmed questions that have never been edited (no version history) — what an audit samples from."""
    edited = edited_ids(db, paper)
    return [q for q in _active(paper) if q.status in CONFIRMED and q.id not in edited]


def sample_size(pool_size: int) -> int:
    return min(pool_size, max(AUDIT_MIN, math.ceil(AUDIT_FRACTION * pool_size)))


def blocker(db, paper: models.Paper) -> str | None:
    """The reason the audit stops this paper being published, or None."""
    state = paper.audit_state or "none"
    if state == "pending":
        return "The sample audit isn't finished."
    if state == "failed":
        return "The last sample audit failed: review the questions sent back, then run another audit."
    if state == "none":
        pool = len(unedited_confirmed(db, paper))
        if pool >= AUDIT_MIN:
            return f"Run the sample audit first ({pool} questions were confirmed without any edit)."
    return None


def invalidate(paper: models.Paper) -> None:
    """Something confirmed changed: a passed (or failed) audit no longer describes the paper. A running audit is left alone."""
    if (paper.audit_state or "none") in ("passed", "failed"):
        paper.audit_state = "none"


def start(db, paper: models.Paper, rng: random.Random | None = None) -> list[models.Question]:
    """Pick the sample and put the paper into 'pending'. Returns the picked questions."""
    pool = unedited_confirmed(db, paper)
    if not pool:
        raise ValueError("There are no confirmed questions to audit yet.")
    for q in paper.questions:
        q.audit_pick, q.audit_result = False, None
    picked = sorted((rng or random.SystemRandom()).sample(pool, sample_size(len(pool))), key=lambda q: q.question_number or 0)
    for q in picked:
        q.audit_pick = True
    paper.audit_state = "pending"
    paper.audit_round = (paper.audit_round or 0) + 1
    return picked


def picked(paper: models.Paper) -> list[models.Question]:
    return sorted((q for q in paper.questions if q.audit_pick), key=lambda q: q.question_number or 0)


def check(db, paper: models.Paper, q: models.Question, verdict: str) -> dict | None:
    """Record 'ok' or 'wrong' for a picked question. A wrong one goes back to review at once. When the last pick has a verdict the
    audit is finished and its outcome returned (else None)."""
    if verdict not in ("ok", "wrong"):
        raise ValueError("Verdict must be ok or wrong")
    if (paper.audit_state or "none") != "pending" or not q.audit_pick:
        raise ValueError("That question isn't part of a running audit.")
    q.audit_result = verdict
    if verdict == "wrong" and q.status in CONFIRMED:
        _send_back(q)
    if any(p.audit_result is None for p in picked(paper)):
        return None
    return finish(db, paper)


def _send_back(q: models.Question) -> None:
    q.status, q.reviewed_by, q.reviewed_at, q.flags_acknowledged = QStatus.NEEDS_REVIEW, None, None, False


def finish(db, paper: models.Paper) -> dict:
    sample = picked(paper)
    wrong = sum(1 for q in sample if q.audit_result == "wrong")
    outcome = {"round": paper.audit_round, "checked": len(sample), "wrong": wrong, "passed": wrong < AUDIT_FAIL_AT, "sent_back": 0}
    if outcome["passed"]:
        paper.audit_state = "passed"
    else:
        paper.audit_state = "failed"
        for q in unedited_confirmed(db, paper):                # everyone who was waved through gets a second look
            _send_back(q)
            outcome["sent_back"] += 1
    for q in paper.questions:
        q.audit_pick = False                                   # the results stay on the questions; the pick is over
    return outcome
