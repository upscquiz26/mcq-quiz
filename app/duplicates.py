"""
Duplicate questions: the same question turning up in more than one paper (or twice in one).

Two ways of matching, both on a *normalised* form so case, punctuation, spacing and the order of the options don't matter:

  * exact — the normalised text and the set of options are identical (the same hash);
  * near  — the wording is at least NEAR_SCORE% similar (rapidfuzz) and the options are alike too, for the reworded or slightly mis-read copy.

Nothing is decided automatically. Each pair is stored as a QuestionDuplicate for the admin, who chooses:

  * keep both — they are different questions after all (the pair is remembered and never raised again);
  * merge     — keep one, take the other out of circulation (quarantined, with a reason) and record where it appeared on the kept one
                (QuestionSource), so "also asked in …" survives and nothing is deleted. Attempts made on the dropped copy keep their history.

Papers that are archived and questions that are quarantined are not compared.
"""
import re
from datetime import datetime

from rapidfuzz import fuzz, process

from app import language, models
from app.models import QStatus

NEAR_SCORE = 90            # similarity (0-100) of the wording at which two different-looking questions are raised as "near" duplicates
NEAR_OPTIONS_SCORE = 70    # ...provided their options are at least this alike (the same stem with other options is a different question)
MIN_NEAR_LENGTH = 25       # shorter text is only compared exactly: short questions look alike by accident
NEAR_PER_QUESTION = 5      # at most this many near matches are stored per question


def _words(s: str | None) -> str:
    """Lower-cased words: Latin letters, digits and Devanagari letters are kept, everything else is a separator."""
    return " ".join(re.sub(r"[^a-z0-9\u0900-\u0963\u0970-\u097f]+", " ", (s or "").lower()).split())


def _options(q) -> list[str]:
    return sorted(_words(o) for o in language.primary(q)[2])


def _text(q) -> str:
    return language.primary(q)[1]


def normalised(q) -> str:   # kept for callers/tests that want the whole question as one comparable string
    """The text a comparison is made on: normalised question, then its normalised options in sorted order."""
    return _words(_text(q)) + " || " + " | ".join(_options(q))


def norm_hash(q) -> str:
    """Same hash the JSON importer uses (text and options with everything but letters and digits removed, options sorted)."""
    from app import json_import
    _, text, options = language.primary(q)
    return json_import.norm_hash(text, options)


def answer_text(q) -> str | None:
    """The normalised text of the option marked correct — what the answer *means*, whatever letter it has in this copy."""
    letter = (q.correct_answer or "").upper()
    if letter not in ("A", "B", "C", "D"):
        return None
    return _words(language.primary(q)[2]["ABCD".index(letter)]) or None


def answers_differ(a, b) -> bool:
    """Two copies of a question disagree about the answer. Compared by the answer's *text*, not its letter, because the same question is often
    printed with its options in a different order (A in one paper can be C in another)."""
    ta, tb = answer_text(a), answer_text(b)
    return bool(ta and tb and ta != tb)


CONFLICT_FLAG = "source_conflict"


def conflicts_of(db, q: models.Question) -> list[models.Question]:
    """Other copies of this question in circulation (identical or very similar, and not merged away) whose answer disagrees with this one's.
    Merging is the resolution — the admin chose the copy that is right — so a merged copy no longer counts."""
    if q.status == QStatus.QUARANTINED or not q.correct_answer:
        return []
    partners: dict[int, models.Question] = {}
    rows = db.query(models.QuestionDuplicate).filter(
        models.QuestionDuplicate.status.in_(("open", "kept_both")),
        (models.QuestionDuplicate.question_id == q.id) | (models.QuestionDuplicate.other_id == q.id))
    for row in rows:
        other = db.get(models.Question, row.other_id if row.question_id == q.id else row.question_id)
        if other is not None and other.status != QStatus.QUARANTINED:
            partners[other.id] = other
    return [p for p in partners.values() if answers_differ(q, p)]


def refresh_conflicts(db, question_ids) -> int:
    """Keep the `source_conflict` warning true for the given questions: set while another copy of the question has a different answer, cleared
    when it doesn't. A confirmed question that newly gets the warning must be looked at again (its warning counts as unacknowledged).
    Returns the number of questions that newly carry the warning."""
    newly = 0
    for qid in set(question_ids):
        q = db.get(models.Question, qid)
        if q is None:
            continue
        flags = [f for f in (q.ocr_flags or "").split(",") if f]
        has = CONFLICT_FLAG in flags
        conflict = bool(conflicts_of(db, q))
        if conflict and not has:
            flags.append(CONFLICT_FLAG)
            q.flags_acknowledged = False
            newly += 1
        elif has and not conflict:
            flags.remove(CONFLICT_FLAG)
        q.ocr_flags = ",".join(flags) or None
    return newly


def refresh_around(db, question_ids) -> int:
    """refresh_conflicts for these questions and for every other copy tied to them (an answer changed on one side changes the other's warning too)."""
    ids = set(question_ids)
    if not ids:
        return 0
    everyone = set(ids)
    for row in db.query(models.QuestionDuplicate).filter(models.QuestionDuplicate.status.in_(("open", "kept_both", "merged")),
                                                         models.QuestionDuplicate.question_id.in_(ids) | models.QuestionDuplicate.other_id.in_(ids)):
        everyone |= {row.question_id, row.other_id}
    for s in db.query(models.QuestionSource).filter(models.QuestionSource.question_id.in_(ids) | models.QuestionSource.from_question_id.in_(ids)):
        everyone |= {s.question_id, s.from_question_id} - {None}
    return refresh_conflicts(db, everyone)


def label_of(q: models.Question, paper: models.Paper | None = None) -> str:
    paper = paper or q.paper
    year = f" · {paper.year}" if paper is not None and paper.year else ""
    return f"{paper.title if paper is not None else 'a paper'}{year} · Q{q.question_number}"


def _pool(db) -> list[models.Question]:
    return (db.query(models.Question)
            .join(models.Paper, models.Paper.id == models.Question.paper_id)
            .filter(models.Paper.archived_at.is_(None), models.Question.status != QStatus.QUARANTINED)
            .all())


def _pair(a: models.Question, b: models.Question) -> tuple[int, int]:
    """(newer id, older id) — the stored order of a pair."""
    return (a.id, b.id) if a.id > b.id else (b.id, a.id)


def scan(db, paper_id: int | None = None, only: list[int] | None = None) -> dict:
    """Find duplicates of the questions in one paper (or the given question ids, or — with neither — of every question) among all live
    papers' questions, and store new pairs as open. Pairs already decided (kept both / merged) are left as they are. Open pairs that no
    longer match (edited, quarantined, archived) are removed. Returns {"exact": n, "near": n, "cleared": n} for the newly found / removed ones."""
    pool = _pool(db)
    hashes: dict[int, str] = {}
    for q in pool:
        h = norm_hash(q)
        hashes[q.id] = h
        if q.norm_hash != h:
            q.norm_hash = h                                    # keep the stored hash in step with the text
    targets = [q for q in pool if (only is None or q.id in only) and (paper_id is None or q.paper_id == paper_id)]
    target_ids = {q.id for q in targets}

    by_hash: dict[str, list[models.Question]] = {}
    for q in pool:
        by_hash.setdefault(hashes[q.id], []).append(q)
    stems = [_words(_text(q)) for q in pool]
    option_text = [" | ".join(_options(q)) for q in pool]

    wanted: dict[tuple[int, int], tuple[str, int]] = {}
    for q in targets:
        for other in by_hash[hashes[q.id]]:
            if other.id != q.id:
                wanted[_pair(q, other)] = ("exact", 100)
        mine, mine_options = _words(_text(q)), " | ".join(_options(q))
        if len(mine) >= MIN_NEAR_LENGTH:
            found = 0
            # The wording must be very similar AND the options must be alike too: the same stem with other options is another question.
            for _, score, index in process.extract(mine, stems, scorer=fuzz.ratio, score_cutoff=NEAR_SCORE, limit=NEAR_PER_QUESTION + 20):
                other = pool[index]
                if other.id == q.id or hashes[other.id] == hashes[q.id]:
                    continue
                if fuzz.ratio(mine_options, option_text[index]) < NEAR_OPTIONS_SCORE:
                    continue
                wanted.setdefault(_pair(q, other), ("near", int(score)))
                found += 1
                if found >= NEAR_PER_QUESTION:
                    break

    existing = {(d.question_id, d.other_id): d
                for d in db.query(models.QuestionDuplicate).filter(
                    (models.QuestionDuplicate.question_id.in_(target_ids or [0])) | (models.QuestionDuplicate.other_id.in_(target_ids or [0])))}
    counts = {"exact": 0, "near": 0, "cleared": 0}
    for key, (kind, score) in wanted.items():
        row = existing.get(key)
        if row is None:
            db.add(models.QuestionDuplicate(question_id=key[0], other_id=key[1], kind=kind, score=score))
            counts[kind] += 1
        elif row.status == "open":
            row.kind, row.score = kind, score
    for key, row in existing.items():
        if row.status == "open" and key not in wanted:
            db.delete(row)
            counts["cleared"] += 1
    db.flush()
    touched = set(target_ids) | {i for pair in wanted for i in pair} | {i for key, row in existing.items() for i in key}
    conflicts = refresh_conflicts(db, touched)
    if conflicts:
        counts["conflicts"] = conflicts                        # only present when a question newly disagrees with another copy
    return counts


def open_pairs(db, paper_id: int | None = None) -> list[models.QuestionDuplicate]:
    query = db.query(models.QuestionDuplicate).filter(models.QuestionDuplicate.status == "open")
    rows = query.order_by(models.QuestionDuplicate.kind, models.QuestionDuplicate.score.desc(), models.QuestionDuplicate.id).all()
    if paper_id is None:
        return rows
    ids = {q.id for q in db.query(models.Question.id).filter(models.Question.paper_id == paper_id)}
    return [r for r in rows if r.question_id in ids or r.other_id in ids]


def open_counts(db, paper_id: int) -> dict[int, int]:
    """{question id: number of open pairs it is in} for one paper's questions."""
    ids = {q.id for q in db.query(models.Question.id).filter(models.Question.paper_id == paper_id)}
    counts: dict[int, int] = {}
    for row in db.query(models.QuestionDuplicate).filter(models.QuestionDuplicate.status == "open"):
        for qid in (row.question_id, row.other_id):
            if qid in ids:
                counts[qid] = counts.get(qid, 0) + 1
    return counts


def keep_both(db, user, row: models.QuestionDuplicate) -> None:
    """They are different questions after all. (If they are identical yet disagree on the answer, the conflict warning stays: that is a real problem.)"""
    row.status, row.decided_by, row.decided_at = "kept_both", user.id if user else None, datetime.utcnow()
    db.flush()
    refresh_conflicts(db, [row.question_id, row.other_id])


def merge(db, user, row: models.QuestionDuplicate, keep_id: int) -> tuple[models.Question, models.Question]:
    """Keep one question of the pair and take the other out of circulation. Returns (kept, dropped).
    The dropped copy is quarantined (never deleted), and recorded as a source of the kept one, along with any sources it had itself."""
    if row.status != "open":
        raise ValueError("This pair has already been decided.")
    if keep_id not in (row.question_id, row.other_id):
        raise ValueError("Choose one of the two questions to keep.")
    kept = db.get(models.Question, keep_id)
    dropped = db.get(models.Question, row.other_id if keep_id == row.question_id else row.question_id)
    if kept.status == QStatus.QUARANTINED or dropped.status == QStatus.QUARANTINED:
        raise ValueError("One of these questions is already in quarantine.")
    now = datetime.utcnow()
    label = label_of(dropped)
    db.add(models.QuestionSource(question_id=kept.id, from_question_id=dropped.id, paper_id=dropped.paper_id,
                                 question_number=dropped.question_number, label=label, added_by=user.id if user else None))
    for source in db.query(models.QuestionSource).filter(models.QuestionSource.question_id == dropped.id):
        source.question_id = kept.id                          # everything the dropped copy stood for now stands on the kept one
    dropped.status = QStatus.QUARANTINED
    dropped.quarantine_reason = f"Merged into a duplicate: {label_of(kept)}"
    dropped.quarantined_at = now
    dropped.reviewed_by = dropped.reviewed_at = None
    row.status, row.merged_into, row.decided_by, row.decided_at = "merged", kept.id, user.id if user else None, now
    for other in db.query(models.QuestionDuplicate).filter(
            models.QuestionDuplicate.status == "open", models.QuestionDuplicate.id != row.id,
            (models.QuestionDuplicate.question_id == dropped.id) | (models.QuestionDuplicate.other_id == dropped.id)):
        db.delete(other)                                      # the dropped copy is out of circulation: its other pairs go with it
    db.flush()
    refresh_conflicts(db, [kept.id, dropped.id])
    return kept, dropped


def undo_merge(db, question: models.Question) -> int:
    """A merged question was restored from quarantine: it is its own question again. Its source record is removed from the kept question
    and the pair is opened again. Returns the number of pairs reopened."""
    db.query(models.QuestionSource).filter(models.QuestionSource.from_question_id == question.id).delete()
    reopened = 0
    for row in db.query(models.QuestionDuplicate).filter(models.QuestionDuplicate.status == "merged",
                                                         (models.QuestionDuplicate.question_id == question.id)
                                                         | (models.QuestionDuplicate.other_id == question.id)):
        row.status, row.merged_into, row.decided_by, row.decided_at = "open", None, None, None
        reopened += 1
    db.flush()
    partners = {i for row in db.query(models.QuestionDuplicate).filter(
        (models.QuestionDuplicate.question_id == question.id) | (models.QuestionDuplicate.other_id == question.id))
        for i in (row.question_id, row.other_id)}
    refresh_conflicts(db, partners | {question.id})
    return reopened


def sources_of(db, question_ids: list[int]) -> dict[int, list[models.QuestionSource]]:
    out: dict[int, list[models.QuestionSource]] = {}
    if not question_ids:
        return out
    for s in db.query(models.QuestionSource).filter(models.QuestionSource.question_id.in_(question_ids)).order_by(models.QuestionSource.id):
        out.setdefault(s.question_id, []).append(s)
    return out
