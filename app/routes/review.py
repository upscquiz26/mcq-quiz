"""The review screen: check, correct, confirm, quarantine and undo edits on a paper's questions."""
import json
import os
import re
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app import audit, duplicates, ingest, key_parse, language, models, sample_audit, subject_hints, subject_templates, versions
from app.database import get_db
from app.models import QStatus
from app.practice import pool
from app.routes.keys import answer_summary
from app.web import flash, require_admin, templates, to_int

router = APIRouter(dependencies=[Depends(require_admin)])

TO_CONFIRM = (QStatus.DRAFT, QStatus.NEEDS_REVIEW)


def _paper_or_404(db: Session, paper_id: int) -> models.Paper:
    paper = db.get(models.Paper, paper_id)
    if not paper:
        raise HTTPException(status_code=404, detail="Paper not found")
    return paper


def _question_or_404(db: Session, paper_id: int, question_id: int) -> models.Question:
    q = db.get(models.Question, question_id)
    if not q or q.paper_id != paper_id:
        raise HTTPException(status_code=404, detail="Question not found")
    return q


def extraction_summary(paper: models.Paper, questions: list[models.Question]) -> dict:
    """'Extracted X of Y': which question numbers exist, and which are missing.
    Y is the admin's expected total when set; otherwise the highest number seen, so gaps in the middle still show."""
    present = {q.question_number for q in questions if q.question_number}
    top = paper.expected_total or (max(present) if present else 0)
    missing = [n for n in range(1, top + 1) if n not in present]
    beyond = sorted(n for n in present if paper.expected_total and n > paper.expected_total)
    return {"found": len(present), "expected": paper.expected_total, "of": top, "missing": missing, "beyond": beyond}


@router.get("/review/{paper_id}")
def review_paper(request: Request, paper_id: int, show: str = "all", sort: str = "", db: Session = Depends(get_db)):
    paper = _paper_or_404(db, paper_id)
    every = (
        db.query(models.Question)
        .filter(models.Question.paper_id == paper_id)
        .order_by(models.Question.question_number)
        .all()
    )
    active = [q for q in every if q.status != QStatus.QUARANTINED]
    counts = {
        "total": len(active),
        "to_confirm": sum(1 for q in active if q.status in TO_CONFIRM),
        "verified": sum(1 for q in active if q.status in (QStatus.VERIFIED, QStatus.LIVE)),
        "flagged": sum(1 for q in active if q.ocr_flags),
        "clean": sum(1 for q in active if q.status in TO_CONFIRM and not q.ocr_flags and q.correct_answer
                     and q.answer_source != "json"),
        "bulk_confirmable": sum(1 for q in active if q.status in TO_CONFIRM and q.correct_answer in pool.ANSWER_LETTERS),
        "bulk_ai_answers": sum(1 for q in active if q.status in TO_CONFIRM and q.correct_answer in pool.ANSWER_LETTERS
                    and q.answer_source == "json"),
        "bulk_flagged": sum(1 for q in active if q.status in TO_CONFIRM and q.correct_answer in pool.ANSWER_LETTERS and q.ocr_flags),
        "bulk_no_answer": sum(1 for q in active if q.status in TO_CONFIRM and q.correct_answer not in pool.ANSWER_LETTERS),
        "ai": sum(1 for q in active if q.source == "ai_json"),
        "ai_answers": sum(1 for q in active if q.answer_source == "json"),
        "no_subject": sum(1 for q in active if not q.subject_id),
        "suggested": sum(1 for q in active if not q.subject_id and q.suggested_subject_id),
        "quarantined": len(every) - len(active),
        "both": sum(1 for q in active if language.which(q) == "both"),
        "hindi_only": sum(1 for q in active if language.which(q) == "hi"),
    }
    if show == "to_confirm":
        shown = [q for q in active if q.status in TO_CONFIRM]
        if sort != "number":                                    # warnings first, so the risky ones get the freshest attention
            shown.sort(key=lambda q: (0 if q.ocr_flags else 1, q.question_number or 0))
            sort = "flagged"
    elif show == "confirmed":
        shown = [q for q in active if q.status in (QStatus.VERIFIED, QStatus.LIVE)]
    elif show == "flagged":
        shown = [q for q in active if q.ocr_flags]
    elif show == "no_subject":
        shown = [q for q in active if not q.subject_id]
    elif show == "ai_answers":
        shown = [q for q in active if q.answer_source == "json"]
    else:
        show, shown = "all", active

    version_counts = {}
    if shown:
        version_counts = dict(
            db.query(models.QuestionVersion.question_id, func.count())
            .filter(models.QuestionVersion.question_id.in_([q.id for q in shown]))
            .group_by(models.QuestionVersion.question_id)
            .all()
        )
    subjects = db.query(models.Subject).order_by(models.Subject.id).all()
    folder = ingest.images_dir_for(paper_id)                    # original-page pictures made from a PDF attached to a JSON import
    page_pictures = {int(m.group(1)) for name in (os.listdir(folder) if os.path.isdir(folder) else [])
                     if (m := re.fullmatch(r"page(\d{1,4})\.jpg", name))}
    return templates.TemplateResponse(
        "review.html",
        {
            "request": request, "paper": paper, "questions": shown, "subjects": subjects,
            "counts": counts, "show": show, "flag_labels": ingest.FLAG_LABELS, "page_pictures": page_pictures,
            "key_summary": answer_summary(paper), "extraction": extraction_summary(paper, every), "sort": sort,
            "audit": {"state": paper.audit_state or "none", "round": paper.audit_round or 0,
                      "pool": len(sample_audit.unedited_confirmed(db, paper)), "min": sample_audit.AUDIT_MIN},
            "subject_names": {s.id: s.name for s in subjects},
            "dup_counts": duplicates.open_counts(db, paper_id), "sources": duplicates.sources_of(db, [q.id for q in shown]),
            "saved_templates": [t for t in subject_templates.all_templates(db) if subject_templates.fits(t, paper.series)],
            "version_counts": version_counts, "to_confirm_statuses": TO_CONFIRM,
            "blockers": pool.publish_blockers(db, paper),
            "live_count": sum(1 for q in active if q.status == QStatus.LIVE),
            "held_back": sum(1 for q in active if q.status == QStatus.VERIFIED and pool.needs_snapshot(q)),
            "next_number": (max((q.question_number or 0) for q in every) + 1) if every else 1,
            "flash": request.session.pop("flash", None),
        },
    )


@router.post("/review/{paper_id}/question/{question_id}")
def save_question(
    request: Request,
    paper_id: int,
    question_id: int,
    text: str = Form(""),
    option_a: str = Form(""),
    option_b: str = Form(""),
    option_c: str = Form(""),
    option_d: str = Form(""),
    option_e: str = Form(""),
    correct_answer: str = Form(""),
    subject_id: str = Form(""),
    topic_name: str = Form(""),
    difficulty: str = Form(""),
    has_image: bool = Form(False),
    explanation_verified: bool = Form(False),
    explanation_hi_verified: bool = Form(False),
    question_hi: str | None = Form(None),          # the Hindi boxes are only in the form for a question that has Hindi;
    option_a_hi: str | None = Form(None),          # None means "not in the form": leave that field as it is
    option_b_hi: str | None = Form(None),
    option_c_hi: str | None = Form(None),
    option_d_hi: str | None = Form(None),
    db: Session = Depends(get_db),
):
    user = request.state.user
    q = _question_or_404(db, paper_id, question_id)
    if q.status == QStatus.QUARANTINED:
        raise HTTPException(status_code=400, detail="This question is in quarantine — restore it first")

    answer = correct_answer.strip().upper()
    if answer not in pool.ANSWER_LETTERS or (answer == "E" and not option_e.strip()):
        raise HTTPException(status_code=400, detail="Correct answer must match an available option A–E")
    try:
        subject_id_value = to_int(subject_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid subject")

    # Work out the topic first, so every changed field can be compared before anything is written.
    topic_name = (topic_name or "").strip()
    topic_id_value = None
    new_topic = None
    if topic_name and subject_id_value:
        topic = (
            db.query(models.Topic)
            .filter(func.lower(models.Topic.name) == topic_name.lower(), models.Topic.subject_id == subject_id_value)
            .first()
        )
        if topic:
            topic_id_value = topic.id
        else:
            new_topic = models.Topic(name=topic_name, subject_id=subject_id_value)
            db.add(new_topic)
            db.flush()
            topic_id_value = new_topic.id

    new_values = {
        "text": text, "option_a": option_a, "option_b": option_b, "option_c": option_c, "option_d": option_d,
        "option_e": option_e.strip() or None,
        "correct_answer": answer, "subject_id": subject_id_value, "topic_id": topic_id_value,
        "difficulty": difficulty or None, "has_image": has_image,
    }
    for name, value in (("question_hi", question_hi), ("option_a_hi", option_a_hi), ("option_b_hi", option_b_hi),
                        ("option_c_hi", option_c_hi), ("option_d_hi", option_d_hi)):
        if value is not None:
            new_values[name] = value.strip() or None          # a Hindi field that is emptied becomes null, like an absent one
    if not (text or "").strip() and not (new_values.get("question_hi", q.question_hi) or "").strip():
        raise HTTPException(status_code=400, detail="A question needs text in at least one language")
    # Only meaningful when there is an explanation; "verified" is something an admin says, never a default.
    if q.explanation:
        if explanation_verified:
            new_values["explanation_status"] = "verified"
        elif q.explanation_status == "verified":
            new_values["explanation_status"] = "unverified"     # the admin un-ticked it
        # otherwise leave it exactly as it was, so an untouched question isn't counted as edited
    if q.explanation_hi:                                     # the Hindi explanation has its own tick, separate from the English one
        if explanation_hi_verified:
            new_values["explanation_hi_status"] = "verified"
        elif q.explanation_hi_status == "verified":
            new_values["explanation_hi_status"] = "unverified"
    changed = versions.changed_fields(q, new_values)
    if changed:
        # the state before this edit, so it can be undone. Filing a question (subject, topic, difficulty) is recorded under its own
        # reason so it doesn't make a question look hand-edited to the sample audit.
        edits_content = any(f in sample_audit.CONTENT_FIELDS for f in changed)
        versions.snapshot(db, q, user, "edit" if edits_content else "subject/topic/difficulty")
        for field, value in new_values.items():
            setattr(q, field, value)
        if "correct_answer" in changed:
            q.answer_source = "manual"
            key_parse.refresh_mismatch_flag(q)
        audit.log(db, user, "question.edit", "question", q.id, paper_id=paper_id, detail={"fields": changed})
        language.refresh_flags(q)                            # incomplete / swapped / mismatch follow what is there now
        if edits_content:
            duplicates.scan(db, only=[q.id])                 # the wording changed: it may now match (or stop matching) another question

    was_live = q.status == QStatus.LIVE
    if q.status in TO_CONFIRM:
        q.status = QStatus.VERIFIED
        sample_audit.invalidate(q.paper)                     # a new confirmation the last audit never saw
        q.reviewed_by, q.reviewed_at = user.id, datetime.utcnow()
        q.flags_acknowledged = bool(q.ocr_flags)
        audit.log(db, user, "question.confirm", "question", q.id, paper_id=paper_id,
                  detail={"had_warnings": bool(q.ocr_flags)})
    elif was_live and changed:
        q.status = QStatus.NEEDS_REVIEW            # a live question that changed must be confirmed again
        sample_audit.invalidate(q.paper)
        q.reviewed_by = q.reviewed_at = None
        q.flags_acknowledged = False
        audit.log(db, user, "question.demote", "question", q.id, paper_id=paper_id,
                  detail={"reason": "edited while live"})
    db.commit()

    # Land on the next question so a long paper can be confirmed top to bottom.
    anchor = f"#q{q.question_number + 1}" if q.question_number else ""
    return RedirectResponse(url=f"/review/{paper_id}{anchor}", status_code=303)


@router.post("/review/{paper_id}/question/{question_id}/reopen")
def reopen_question(request: Request, paper_id: int, question_id: int, db: Session = Depends(get_db)):
    """Sends a confirmed (verified or live) question back to review, so it has to be checked and confirmed again.
    A live question stops being shown to students straight away."""
    q = _question_or_404(db, paper_id, question_id)
    if q.status not in (QStatus.VERIFIED, QStatus.LIVE):
        flash(request, f"Q{q.question_number} isn't confirmed, so there is nothing to send back.", "notice")
        return RedirectResponse(url=f"/review/{paper_id}#q{q.question_number}", status_code=303)
    was = q.status
    q.status = QStatus.NEEDS_REVIEW
    q.reviewed_by = q.reviewed_at = None
    q.flags_acknowledged = False
    sample_audit.invalidate(q.paper)
    audit.log(db, request.state.user, "question.reopen", "question", q.id, paper_id=paper_id,
              detail={"number": q.question_number, "was": was})
    db.commit()
    flash(request, f"Q{q.question_number} is back in review." + (" Students can't see it until you confirm it again." if was == QStatus.LIVE else ""), "notice")
    return RedirectResponse(url=f"/review/{paper_id}#q{q.question_number}", status_code=303)


@router.post("/review/{paper_id}/reopen-all")
def reopen_all(request: Request, paper_id: int, db: Session = Depends(get_db)):
    """Sends every confirmed question of the paper back to review — for a second full pass."""
    paper = _paper_or_404(db, paper_id)
    questions = (
        db.query(models.Question)
        .filter(models.Question.paper_id == paper_id, models.Question.status.in_((QStatus.VERIFIED, QStatus.LIVE)))
        .order_by(models.Question.question_number).all()
    )
    live = sum(1 for q in questions if q.status == QStatus.LIVE)
    if questions:
        sample_audit.invalidate(paper)
    for q in questions:
        q.status = QStatus.NEEDS_REVIEW
        q.reviewed_by = q.reviewed_at = None
        q.flags_acknowledged = False
    if questions:
        audit.log(db, request.state.user, "question.reopen_bulk", "paper", paper_id, paper_id=paper_id,
                  detail={"count": len(questions), "live": live, "numbers": [q.question_number for q in questions]})
    db.commit()
    message = (f"{len(questions)} confirmed question{'s' if len(questions) != 1 else ''} sent back to review."
               if questions else "No confirmed questions to send back.")
    flash(request, message, "notice")
    return RedirectResponse(url=f"/review/{paper_id}?show=to_confirm", status_code=303)


@router.post("/review/{paper_id}/rerun")
def rerun_pages(request: Request, background_tasks: BackgroundTasks, paper_id: int, first_page: str = Form(""), last_page: str = Form(""),
                db: Session = Depends(get_db)):
    """Read some pages of the PDF again. Confirmed and hand-edited questions are protected (see ingest.rerun_pages)."""
    paper = _paper_or_404(db, paper_id)
    back = RedirectResponse(url=f"/review/{paper_id}", status_code=303)
    try:
        first, last = int(first_page), int(last_page or first_page)
    except ValueError:
        flash(request, "Enter the first and last page as whole numbers.")
        return back
    if first < 1 or last < first or (paper.pages_total and last > paper.pages_total):
        flash(request, f"Choose a page range within the paper (1–{paper.pages_total or "?"}), first page before last.")
        return back
    if paper.status != "ready":
        flash(request, "This paper isn't ready to be read again.")
        return back
    if not paper.source_pdf_path or not os.path.exists(paper.source_pdf_path):
        flash(request, "The question PDF for this paper isn't on file, so its pages can't be read again.")
        return back
    paper.status, paper.status_message, paper.pages_done = "processing", None, 0
    audit.log(db, request.state.user, "paper.rerun_start", "paper", paper.id, paper_id=paper.id, detail={"first": first, "last": last})
    db.commit()
    background_tasks.add_task(ingest.rerun_pages, paper.id, first, last, paper.layout or "auto")
    return back


@router.post("/review/{paper_id}/confirm-clean")
def confirm_clean(request: Request, paper_id: int, db: Session = Depends(get_db)):
    """Confirms every question that has an answer and no warnings. Flagged ones are left for a manual look."""
    paper = _paper_or_404(db, paper_id)
    questions = (
        db.query(models.Question)
        .filter(models.Question.paper_id == paper_id, models.Question.status.in_(TO_CONFIRM))
        .order_by(models.Question.question_number)
        .all()
    )
    confirmed = []
    for q in questions:
        if not q.ocr_flags and q.correct_answer and q.answer_source != "json":     # AI-supplied answers are never bulk-confirmed
            q.status = QStatus.VERIFIED
            q.reviewed_by, q.reviewed_at = request.state.user.id, datetime.utcnow()
            confirmed.append(q.question_number)
    if confirmed:
        sample_audit.invalidate(paper)
        audit.log(db, request.state.user, "question.confirm_bulk", "paper", paper_id, paper_id=paper_id,
                  detail={"count": len(confirmed), "numbers": confirmed})
    db.commit()
    flash(request, f"Confirmed {len(confirmed)} question{'s' if len(confirmed) != 1 else ''} with no warnings.", "notice")
    return RedirectResponse(url=f"/review/{paper_id}", status_code=303)


@router.post("/review/{paper_id}/confirm-all")
def confirm_all(request: Request, paper_id: int, db: Session = Depends(get_db)):
    """Bulk-confirms answered pending questions, including AI answers and questions with acknowledged warnings."""
    paper = _paper_or_404(db, paper_id)
    questions = (
        db.query(models.Question)
        .filter(models.Question.paper_id == paper_id, models.Question.status.in_(TO_CONFIRM))
        .order_by(models.Question.question_number)
        .all()
    )
    confirmed, ai_answers, flagged = [], 0, 0
    for q in questions:
        if q.correct_answer not in pool.ANSWER_LETTERS:
            continue
        q.status = QStatus.VERIFIED
        q.reviewed_by, q.reviewed_at = request.state.user.id, datetime.utcnow()
        q.flags_acknowledged = bool(q.ocr_flags)
        confirmed.append(q.question_number)
        ai_answers += q.answer_source == "json"
        flagged += bool(q.ocr_flags)
    if confirmed:
        sample_audit.invalidate(paper)
        audit.log(db, request.state.user, "question.confirm_bulk", "paper", paper_id, paper_id=paper_id,
                  detail={"count": len(confirmed), "numbers": confirmed, "ai_answers": ai_answers,
                          "flagged": flagged, "mode": "all_answered"})
    db.commit()
    remaining = len(questions) - len(confirmed)
    message = f"Confirmed {len(confirmed)} answered question{'s' if len(confirmed) != 1 else ''}, including {ai_answers} AI-supplied and {flagged} flagged."
    if remaining:
        message += f" {remaining} question{'s' if remaining != 1 else ''} without a valid answer remain in review."
    flash(request, message, "notice")
    return RedirectResponse(url=f"/review/{paper_id}", status_code=303)


@router.post("/review/{paper_id}/subjects")
def set_subjects_by_range(
    request: Request, paper_id: int, subject_ranges: str = Form(""), template_id: str = Form(""),
    save_name: str = Form(""), save_series: str = Form(""), db: Session = Depends(get_db)
):
    """Subjects by question number: typed ranges, or a saved template. Optionally saves the ranges as a template (per series)."""
    paper = _paper_or_404(db, paper_id)
    back = RedirectResponse(url=f"/review/{paper_id}", status_code=303)
    try:
        names = [s.name for s in db.query(models.Subject).order_by(models.Subject.id).all()]
        text, template = subject_templates.ranges_to_use(db, subject_ranges, template_id, paper.series)
        ranges = ingest.parse_subject_ranges(text, names)
        if not ranges:
            raise ValueError("Enter at least one range, e.g. 1-30 History — or choose a saved template.")
        saved = None
        if save_name.strip():
            saved = subject_templates.save(db, request.state.user, save_name, save_series, text, names)
    except ValueError as e:
        db.rollback()
        flash(request, str(e))
        return back
    changed = ingest.apply_subject_ranges(db, paper_id, ranges, user=request.state.user, keep_history=True)
    audit.log(db, request.state.user, "subjects.bulk_set", "paper", paper_id, paper_id=paper_id,
              detail={"ranges": text.strip(), "changed": changed, "template": template.name if template else None})
    message = f"Subject set on {changed} question{'s' if changed != 1 else ''}."
    if saved:
        audit.log(db, request.state.user, "subject_template.save", "subject_template", saved.id, paper_id=paper_id,
                  detail={"name": saved.name, "series": saved.series, "ranges": saved.ranges_text})
        message += f" Saved as template “{saved.name}”{f' (series {saved.series})' if saved.series else ' (any series)'}."
    db.commit()
    flash(request, message, "notice")
    return back


@router.post("/review/{paper_id}/subject-templates/{template_id}/delete")
def delete_subject_template(request: Request, paper_id: int, template_id: int, db: Session = Depends(get_db)):
    _paper_or_404(db, paper_id)
    t = db.get(models.SubjectTemplate, template_id)
    if t:
        audit.log(db, request.state.user, "subject_template.delete", "subject_template", t.id, paper_id=paper_id,
                  detail={"name": t.name, "series": t.series})
        db.delete(t)
        db.commit()
        flash(request, f"Template “{t.name}” deleted. Questions already given subjects keep them.", "notice")
    return RedirectResponse(url=f"/review/{paper_id}", status_code=303)


def _chosen(db: Session, paper_id: int, ids: list[int]) -> list[models.Question]:
    """The chosen questions of this paper (other papers' ids and quarantined questions are ignored)."""
    if not ids:
        return []
    return (db.query(models.Question)
            .filter(models.Question.paper_id == paper_id, models.Question.id.in_(ids), models.Question.status != QStatus.QUARANTINED)
            .order_by(models.Question.question_number).all())


def _file_under(db: Session, user, q: models.Question, subject_id: int, reason: str) -> bool:
    """Gives a question a subject (its old state goes into its history). Returns whether anything changed."""
    if q.subject_id == subject_id:
        q.suggested_subject_id = None
        return False
    versions.snapshot(db, q, user, reason)
    q.subject_id = subject_id
    q.suggested_subject_id = None
    if q.topic is not None and q.topic.subject_id != subject_id:
        q.topic_id = None                                       # a topic belongs to one subject
    return True


@router.post("/review/{paper_id}/bulk-subject")
def bulk_subject(request: Request, paper_id: int, question_ids: list[int] = Form(default=[]), subject_id: str = Form(""),
                 db: Session = Depends(get_db)):
    """Assign one subject to every ticked question."""
    _paper_or_404(db, paper_id)
    back = RedirectResponse(url=f"/review/{paper_id}", status_code=303)
    chosen = _chosen(db, paper_id, question_ids)
    if not chosen:
        flash(request, "Tick at least one question first.")
        return back
    subject = db.get(models.Subject, to_int(subject_id)) if (subject_id or "").strip().isdigit() else None
    if not subject:
        flash(request, "Choose a subject to assign.")
        return back
    changed = sum(_file_under(db, request.state.user, q, subject.id, "bulk subject change") for q in chosen)
    audit.log(db, request.state.user, "subjects.bulk_assign", "paper", paper_id, paper_id=paper_id,
              detail={"subject": subject.name, "selected": len(chosen), "changed": changed})
    db.commit()
    flash(request, f"{subject.name} set on {changed} of the {len(chosen)} selected question{'s' if len(chosen) != 1 else ''}.", "notice")
    return back


@router.post("/review/{paper_id}/accept-suggestions")
def accept_suggestions(request: Request, paper_id: int, question_ids: list[int] = Form(default=[]), db: Session = Depends(get_db)):
    """Turn keyword suggestions into subjects: for the ticked questions, or for every question that has a suggestion and no subject."""
    _paper_or_404(db, paper_id)
    query = db.query(models.Question.id).filter(models.Question.paper_id == paper_id, models.Question.subject_id.is_(None),
                                                models.Question.suggested_subject_id.isnot(None))
    ids = question_ids or [row[0] for row in query]
    accepted = 0
    for q in _chosen(db, paper_id, ids):
        if q.suggested_subject_id and not q.subject_id:
            accepted += _file_under(db, request.state.user, q, q.suggested_subject_id, "subject suggestion accepted")
    audit.log(db, request.state.user, "subjects.accept_suggestions", "paper", paper_id, paper_id=paper_id,
              detail={"accepted": accepted, "only_selected": bool(question_ids)})
    db.commit()
    flash(request, f"Accepted the suggested subject on {accepted} question{'s' if accepted != 1 else ''}." if accepted
          else "There were no suggestions to accept.", "notice")
    return RedirectResponse(url=f"/review/{paper_id}", status_code=303)


@router.post("/review/{paper_id}/suggest-subjects")
def suggest_subjects(request: Request, paper_id: int, db: Session = Depends(get_db)):
    """Work the keyword suggestions out again for every question that has no subject."""
    _paper_or_404(db, paper_id)
    found = subject_hints.suggest_for_paper(db, paper_id, redo=True)
    audit.log(db, request.state.user, "subjects.suggest", "paper", paper_id, paper_id=paper_id, detail={"suggestions": found})
    db.commit()
    flash(request, f"{found} question{'s' if found != 1 else ''} now carry a suggested subject (nothing is applied until you accept it).", "notice")
    return RedirectResponse(url=f"/review/{paper_id}", status_code=303)


# ---- quarantine ------------------------------------------------------------

@router.post("/review/{paper_id}/question/{question_id}/quarantine")
def quarantine_question(
    request: Request, paper_id: int, question_id: int, reason: str = Form(""), db: Session = Depends(get_db)
):
    """Takes a bad question out of circulation without deleting it. Restore it from the Quarantine page."""
    q = _question_or_404(db, paper_id, question_id)
    reason = reason.strip()
    if not reason:
        flash(request, "Give a reason for quarantining the question.")
        return RedirectResponse(url=f"/review/{paper_id}#q{q.question_number}", status_code=303)
    if q.status != QStatus.QUARANTINED:
        q.status = QStatus.QUARANTINED
        q.quarantine_reason = reason
        q.quarantined_at = datetime.utcnow()
        audit.log(db, request.state.user, "question.quarantine", "question", q.id, paper_id=paper_id,
                  detail={"number": q.question_number, "reason": reason})
        db.commit()
    flash(request, f"Q{q.question_number} was quarantined. It can be restored from the Quarantine page.", "notice")
    return RedirectResponse(url=f"/review/{paper_id}", status_code=303)


def question_has_student_work(db: Session, question_id: int) -> bool:
    return db.query(models.Response.id).filter_by(question_id=question_id).first() is not None


def paper_has_student_work(db: Session, paper_id: int) -> bool:
    if db.query(models.Attempt.id).filter_by(paper_id=paper_id).first():
        return True
    qids = [row[0] for row in db.query(models.Question.id).filter_by(paper_id=paper_id)]
    if not qids:
        return False
    return db.query(models.Response.id).filter(models.Response.question_id.in_(qids)).first() is not None


def purge_question(db: Session, q: models.Question) -> None:
    """Remove a question and the rows that point at it. The audit log is kept."""
    qid = q.id
    db.query(models.QuestionVersion).filter_by(question_id=qid).delete()
    db.query(models.QuestionDuplicate).filter(or_(
        models.QuestionDuplicate.question_id == qid,
        models.QuestionDuplicate.other_id == qid,
    )).delete()
    db.query(models.QuestionDuplicate).filter_by(merged_into=qid).update(
        {models.QuestionDuplicate.merged_into: None}, synchronize_session=False
    )
    db.query(models.QuestionSource).filter(or_(
        models.QuestionSource.question_id == qid,
        models.QuestionSource.from_question_id == qid,
    )).delete()
    db.query(models.RevisionItem).filter_by(question_id=qid).delete()
    db.query(models.QuestionBookmark).filter_by(question_id=qid).delete()
    db.query(models.QuestionNote).filter_by(question_id=qid).delete()
    db.query(models.QuestionReport).filter_by(question_id=qid).delete()
    db.query(models.AnswerSuspicion).filter_by(question_id=qid).delete()
    db.delete(q)


@router.post("/review/{paper_id}/questions/add")
def add_question(
    request: Request,
    paper_id: int,
    question_number: str = Form(""),
    text: str = Form(""),
    option_a: str = Form(""),
    option_b: str = Form(""),
    option_c: str = Form(""),
    option_d: str = Form(""),
    option_e: str = Form(""),
    correct_answer: str = Form(""),
    explanation: str = Form(""),
    db: Session = Depends(get_db),
):
    """Type a question into this paper. It starts as needs-review and is not shown to students until you confirm and publish."""
    paper = _paper_or_404(db, paper_id)
    if paper.status == "processing":
        flash(request, "This paper is still being read — wait for it to finish, then add questions.")
        return RedirectResponse(url=f"/review/{paper_id}", status_code=303)
    text, option_a, option_b, option_c, option_d, option_e = (text.strip(), option_a.strip(), option_b.strip(),
                                                               option_c.strip(), option_d.strip(), option_e.strip())
    if not text or not all((option_a, option_b, option_c, option_d)):
        flash(request, "A new question needs its text and all four options.")
        return RedirectResponse(url=f"/review/{paper_id}#add-question", status_code=303)
    existing = [n for (n,) in db.query(models.Question.question_number).filter_by(paper_id=paper_id)
                if n is not None]
    if question_number.strip():
        try:
            number = int(question_number.strip())
        except ValueError:
            flash(request, "Question number must be a whole number.")
            return RedirectResponse(url=f"/review/{paper_id}#add-question", status_code=303)
        if number < 1:
            flash(request, "Question number must be 1 or more.")
            return RedirectResponse(url=f"/review/{paper_id}#add-question", status_code=303)
        if number in existing:
            flash(request, f"Q{number} is already on this paper. Pick another number, or edit that question.")
            return RedirectResponse(url=f"/review/{paper_id}#add-question", status_code=303)
    else:
        number = (max(existing) + 1) if existing else 1
    answer = correct_answer.strip().upper()
    if answer and (answer not in pool.ANSWER_LETTERS or (answer == "E" and not option_e)):
        flash(request, "Correct answer must match an available option A–E, or be left blank.")
        return RedirectResponse(url=f"/review/{paper_id}#add-question", status_code=303)
    explanation = explanation.strip() or None
    q = models.Question(
        paper_id=paper_id, question_number=number, text=text,
        option_a=option_a, option_b=option_b, option_c=option_c, option_d=option_d, option_e=option_e or None,
        correct_answer=answer or None, explanation=explanation,
        explanation_status="unverified" if explanation else None,
        status=QStatus.NEEDS_REVIEW, source="manual", answer_source="manual" if answer else None,
    )
    language.refresh_flags(q)
    db.add(q)
    db.flush()
    q.norm_hash = duplicates.norm_hash(q)
    sample_audit.invalidate(paper)
    subject_hints.suggest_for_paper(db, paper_id)
    duplicates.scan(db, paper_id)
    audit.log(db, request.state.user, "question.add", "question", q.id, paper_id=paper_id,
              detail={"number": number, "source": "manual"})
    db.commit()
    flash(request, f"Q{number} was added. Confirm it before it can go live.", "notice")
    return RedirectResponse(url=f"/review/{paper_id}#q{number}", status_code=303)


@router.post("/review/{paper_id}/question/{question_id}/delete")
def delete_question(request: Request, paper_id: int, question_id: int, db: Session = Depends(get_db)):
    """Permanently remove a question that nobody has answered. If students have already met it, quarantine it instead."""
    paper = _paper_or_404(db, paper_id)
    q = _question_or_404(db, paper_id, question_id)
    if question_has_student_work(db, q.id):
        flash(request, f"Q{q.question_number} has student answers, so it can't be deleted. Quarantine it to take it out of circulation without erasing history.")
        return RedirectResponse(url=f"/review/{paper_id}#q{q.question_number}", status_code=303)
    number = q.question_number
    image = q.source_image_path
    purge_question(db, q)
    sample_audit.invalidate(paper)
    audit.log(db, request.state.user, "question.delete", "question", question_id, paper_id=paper_id,
              detail={"number": number})
    db.commit()
    if image:
        path = os.path.join(ingest.images_dir_for(paper_id), image)
        if os.path.isfile(path):
            os.remove(path)
    flash(request, f"Q{number} was deleted.", "notice")
    return RedirectResponse(url=f"/review/{paper_id}", status_code=303)


# ---- version history -------------------------------------------------------

@router.get("/review/{paper_id}/question/{question_id}/history")
def question_history(request: Request, paper_id: int, question_id: int, db: Session = Depends(get_db)):
    paper = _paper_or_404(db, paper_id)
    q = _question_or_404(db, paper_id, question_id)
    rows = []
    for v in versions.history(db, q.id):
        snap = json.loads(v.snapshot_json)
        author = db.get(models.User, v.changed_by) if v.changed_by else None
        rows.append({
            "version": v,
            "snapshot": snap,
            "differs": versions.changed_fields(q, snap),
            "author": author.username if author else "system",
        })
    return templates.TemplateResponse(
        "question_history.html",
        {"request": request, "paper": paper, "q": q, "rows": rows, "flash": request.session.pop("flash", None)},
    )


@router.post("/review/{paper_id}/question/{question_id}/restore/{version_id}")
def restore_version(
    request: Request, paper_id: int, question_id: int, version_id: int, db: Session = Depends(get_db)
):
    q = _question_or_404(db, paper_id, question_id)
    version = db.get(models.QuestionVersion, version_id)
    if not version or version.question_id != q.id:
        raise HTTPException(status_code=404, detail="Version not found")
    changed = versions.restore(db, q, version, request.state.user)
    db.commit()
    flash(request, f"Restored version {version.version_no} ({len(changed)} field{'s' if len(changed) != 1 else ''}). "
                   "The question needs to be confirmed again.", "notice")
    return RedirectResponse(url=f"/review/{paper_id}/question/{question_id}/history", status_code=303)
