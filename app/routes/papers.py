"""Home page, paper upload, page/question images, archiving."""
import hashlib
import os
import re
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from app import audit, backup, ingest, models, ocr_extract, subject_templates, text_extract
from app.database import get_db
from app.practice import attempts as engine
from app.practice import activity, analytics, pool, revision
from app.web import flash, require_admin, templates, to_int

router = APIRouter()

# Marking presets. The form fills the fields from these; every field stays editable.
PRESETS = {
    "upsc_gs1": {"label": "UPSC GS Paper I (100 questions, 2 marks, −⅓, 2 hours)", "expected_total": 100, "marks": "2",
                 "negative": "1/3", "duration": 120},
    "upsc_csat": {"label": "UPSC CSAT (80 questions, 2.5 marks, −⅓, 2 hours)", "expected_total": 80, "marks": "2.5",
                  "negative": "1/3", "duration": 120},
    "custom": {"label": "Custom", "expected_total": "", "marks": "", "negative": "", "duration": ""},
}

FORM_FIELDS = (
    "title", "source_type", "source_name", "test_name", "test_number", "series", "year", "exam_type",
    "expected_total", "marks_per_question", "negative_fraction", "duration_minutes", "key_source", "key_version",
    "subject_ranges", "subject_template", "layout",
)


def _parse_scheme(expected_total: str, marks_per_question: str, negative_fraction: str, duration_minutes: str) -> dict:
    """Validates the marking-scheme and time fields shared by the upload form and the paper-settings form.
    Blank means 'not set'. Raises ValueError with a message meant for the admin."""
    try:
        expected = to_int(expected_total)
        if expected is not None and expected < 1:
            raise ValueError
    except ValueError:
        raise ValueError("Expected number of questions must be a whole number of at least 1.")
    try:
        marks = float(marks_per_question) if marks_per_question.strip() else None
        if marks is not None and marks <= 0:
            raise ValueError
    except ValueError:
        raise ValueError("Marks per question must be a positive number.")
    try:
        negative = _parse_fraction(negative_fraction)
    except (ValueError, ZeroDivisionError):
        raise ValueError("Negative marking must be a fraction like 1/3 or a number between 0 and 1.")
    try:
        minutes = to_int(duration_minutes)
        if minutes is not None and not 1 <= minutes <= 600:
            raise ValueError
    except ValueError:
        raise ValueError("Duration must be a whole number of minutes between 1 and 600.")
    return {"expected_total": expected, "marks_per_question": marks, "negative_fraction": negative,
            "duration_minutes": minutes}


@router.get("/")
def home(request: Request, db: Session = Depends(get_db)):
    if not request.state.user.is_admin:
        return _student_home(request, db)
    papers = db.query(models.Paper).order_by(models.Paper.created_at.desc()).all()
    return templates.TemplateResponse(
        "home.html",
        {
            "request": request,
            "papers": [p for p in papers if p.archived_at is None],
            "archived": [p for p in papers if p.archived_at is not None],
            "source_labels": models.SourceType.LABELS,
            "flash": request.session.pop("flash", None),
        },
    )


def _student_home(request: Request, db: Session):
    """A student's landing page. Only papers with questions a student can actually practise are listed
    (everything comes from live_questions(), the single source of truth for what students may see)."""
    live_counts = dict(
        pool.live_questions(db)
        .with_entities(models.Question.paper_id, func.count(models.Question.id))
        .group_by(models.Question.paper_id)
        .all()
    )
    papers = []
    if live_counts:
        papers = (
            db.query(models.Paper)
            .filter(models.Paper.id.in_(live_counts))
            .order_by(models.Paper.created_at.desc())
            .all()
        )
    engine.expire_overdue(db, request.state.user.id)     # tests whose time ran out while they were away
    unfinished = (
        db.query(models.Attempt)
        .filter(models.Attempt.user_id == request.state.user.id,
                models.Attempt.status == models.AttemptStatus.IN_PROGRESS)
        .order_by(models.Attempt.started_at.desc())
        .limit(3).all()
    )
    return templates.TemplateResponse(
        "student_home.html",
        {
            "request": request,
            "unfinished": unfinished,
            "due_today": revision.due_count(db, request.state.user.id),
            "daily": activity.summary(db, db.get(models.User, request.state.user.id)),
            "recent_tests": analytics.recent_tests(db, request.state.user.id, 5),
            "weak": analytics.weak_areas(db, request.state.user.id),
            "kind_labels": models.AttemptKind.LABELS,
            "answered_count": engine.answered_count,
            "time_left": engine.time_left_label,
            "papers": papers,
            "live_counts": live_counts,
            "source_labels": models.SourceType.LABELS,
            "total_questions": sum(live_counts.values()),
            "flash": request.session.pop("flash", None),
        },
    )


def _subject_names(db: Session) -> list[str]:
    return [s.name for s in db.query(models.Subject).order_by(models.Subject.id).all()]


def _upload_page(request: Request, db: Session, error: str | None = None, form: dict | None = None, status_code: int = 200):
    return templates.TemplateResponse(
        "upload.html",
        {
            "request": request, "error": error, "form": form or {},
            "subjects": _subject_names(db), "presets": PRESETS,
            "subject_templates": [(t.id, subject_templates.label(t), t.ranges_text) for t in subject_templates.all_templates(db)],
            "source_types": models.SourceType.LABELS, "layouts": text_extract.LAYOUTS,
        },
        status_code=status_code,
    )


@router.get("/upload", dependencies=[Depends(require_admin)])
def upload_form(request: Request, db: Session = Depends(get_db)):
    return _upload_page(request, db)


def _parse_fraction(value: str) -> float | None:
    """'1/3' or '0.33' -> a number between 0 and 1. Blank -> None."""
    value = (value or "").strip()
    if not value:
        return None
    number = float(value.split("/")[0]) / float(value.split("/")[1]) if "/" in value else float(value)
    if not 0 <= number <= 1:
        raise ValueError("out of range")
    return round(number, 4)


def _save_pdf(upload: UploadFile, kind: str) -> tuple[str, str]:
    """Stores an uploaded PDF under data/pdfs. Returns (path, sha256). Raises ValueError if it isn't a PDF."""
    head = upload.file.read(5)
    upload.file.seek(0)
    if head != b"%PDF-":
        raise ValueError(f"“{upload.filename}” is not a PDF file.")
    os.makedirs(ingest.PDF_DIR, exist_ok=True)
    original = os.path.basename((upload.filename or "paper.pdf").replace("\\", "/"))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    path = os.path.join(ingest.PDF_DIR, f"{stamp}_{kind}_{original}")
    digest = hashlib.sha256()
    with open(path, "wb") as f:
        while chunk := upload.file.read(1024 * 1024):
            digest.update(chunk)
            f.write(chunk)
    return path, digest.hexdigest()


def _existing_same_test(db: Session, source_name, test_name, test_number, series) -> models.Paper | None:
    """An earlier paper that looks like the same test (same source + test name/number + series)."""
    if not source_name or not (test_name or test_number):
        return None
    query = db.query(models.Paper).filter(func.lower(models.Paper.source_name) == source_name.lower())
    if test_name:
        query = query.filter(func.lower(models.Paper.test_name) == test_name.lower())
    if test_number:
        query = query.filter(func.lower(models.Paper.test_number) == test_number.lower())
    if series:
        query = query.filter(func.lower(models.Paper.series) == series.lower())
    return query.first()


@router.post("/upload", dependencies=[Depends(require_admin)])
def upload_pdf(
    request: Request,
    background_tasks: BackgroundTasks,
    title: str = Form(...),
    source_type: str = Form(""),
    source_name: str = Form(""),
    test_name: str = Form(""),
    test_number: str = Form(""),
    series: str = Form(""),
    year: str = Form(""),
    exam_type: str = Form(...),
    expected_total: str = Form(""),
    marks_per_question: str = Form(""),
    negative_fraction: str = Form(""),
    duration_minutes: str = Form(""),
    key_source: str = Form(""),
    key_version: str = Form(""),
    is_current_affairs: bool = Form(False),
    subject_ranges: str = Form(""),
    subject_template: str = Form(""),
    layout: str = Form("auto"),
    allow_duplicate: bool = Form(False),
    pdf_file: UploadFile = File(...),
    answer_file: UploadFile | None = File(None),
    db: Session = Depends(get_db),
):
    values = dict(locals())
    form = {name: values[name] for name in FORM_FIELDS}   # echoed back if validation fails

    def fail(message: str):
        return _upload_page(request, db, message, form, status_code=400)

    try:
        year_value = to_int(year)
    except ValueError:
        return fail("Year must be a number.")
    try:
        exam_type_value = models.ExamType(exam_type)
    except ValueError:
        return fail("Invalid paper type.")
    if source_type and source_type not in models.SourceType.ALL:
        return fail("Choose Official PYQ or Coaching test as the source.")
    try:
        scheme = _parse_scheme(expected_total, marks_per_question, negative_fraction, duration_minutes)
    except ValueError as e:
        return fail(str(e))
    try:
        range_text, used_template = subject_templates.ranges_to_use(db, subject_ranges, subject_template, series)
        ranges = ingest.parse_subject_ranges(range_text, _subject_names(db))
    except ValueError as e:
        return fail(str(e))
    if layout not in text_extract.LAYOUTS:
        return fail("Choose a page layout from the list.")

    saved = []

    def discard_saved():
        for path in saved:
            if os.path.exists(path):
                os.remove(path)

    try:
        question_path, question_hash = _save_pdf(pdf_file, "questions")
        saved.append(question_path)
        answer_path = answer_hash = None
        if answer_file is not None and answer_file.filename:
            answer_path, answer_hash = _save_pdf(answer_file, "answers")
            saved.append(answer_path)
    except ValueError as e:
        discard_saved()
        return fail(str(e))

    # A PDF with real text is read directly. Only a scan needs OCR, so only then must Tesseract be installed — and
    # this fails now, not after the paper has been created.
    try:
        needs_ocr = not text_extract.has_text_layer(question_path)
    except Exception:
        needs_ocr = True                                  # unreadable as text: the OCR step will say what is wrong
    if needs_ocr:
        try:
            ocr_extract.tesseract_cmd()
        except ocr_extract.OcrUnavailable as e:
            discard_saved()
            return fail(str(e))

    if not allow_duplicate:
        same_file = db.query(models.Paper).filter(models.Paper.file_hash == question_hash).first()
        if same_file:
            discard_saved()
            note = " (archived)" if same_file.archived_at else ""
            return fail(f"This exact file was already uploaded as “{same_file.title}”{note}. "
                        "Tick “Upload anyway” if you really want to import it again.")
        same_test = _existing_same_test(db, source_name.strip(), test_name.strip(), test_number.strip(), series.strip())
        if same_test:
            discard_saved()
            return fail(f"A paper for this test already exists: “{same_test.title}”. "
                        "Tick “Upload anyway” if this is a different file for the same test.")

    # Safety net: a copy of the database before any bulk import touches it.
    backup_name = backup.create_backup("auto")

    paper = models.Paper(
        title=title.strip(),
        year=year_value,
        exam_type=exam_type_value,
        source_pdf_path=question_path,
        answer_pdf_path=answer_path,
        is_current_affairs=is_current_affairs,
        status="processing",
        source_type=source_type or None,
        source_name=source_name.strip() or None,
        test_name=test_name.strip() or None,
        test_number=test_number.strip() or None,
        series=series.strip().upper() or None,
        **scheme,
        key_source=key_source.strip() or None,
        key_version=key_version.strip() or None,
        file_hash=question_hash,
        answer_file_hash=answer_hash,
        layout=layout,
    )
    db.add(paper)
    db.flush()
    audit.log(db, request.state.user, "paper.upload", "paper", paper.id, paper_id=paper.id, detail={
        "title": paper.title, "question_file": pdf_file.filename, "answer_file": answer_file.filename if answer_path else None,
        "sha256": question_hash[:16], "source_type": paper.source_type, "backup": backup_name,
    })
    db.commit()
    db.refresh(paper)

    background_tasks.add_task(ingest.process_paper, paper.id, ranges, layout)
    return RedirectResponse(url=f"/review/{paper.id}", status_code=303)


@router.post("/papers/{paper_id}/settings", dependencies=[Depends(require_admin)])
def update_paper_settings(
    request: Request, paper_id: int,
    expected_total: str = Form(""), marks_per_question: str = Form(""),
    negative_fraction: str = Form(""), duration_minutes: str = Form(""),
    db: Session = Depends(get_db),
):
    """Marking scheme and time allowed. Tests already started keep the marks they started with; only
    tests started from now on use the new values."""
    paper = db.get(models.Paper, paper_id)
    if not paper:
        raise HTTPException(status_code=404, detail="Paper not found")
    try:
        scheme = _parse_scheme(expected_total, marks_per_question, negative_fraction, duration_minutes)
    except ValueError as e:
        flash(request, str(e))
        return RedirectResponse(url=f"/review/{paper_id}", status_code=303)

    changes = {}
    for field, new in scheme.items():
        old = getattr(paper, field)
        if old != new:
            changes[field] = {"from": old, "to": new}
            setattr(paper, field, new)
    if changes:
        audit.log(db, request.state.user, "paper.settings", "paper", paper.id, paper_id=paper.id,
                  detail={"title": paper.title, "changes": changes})
        db.commit()
    flash(request, "Saved. Tests already started keep the marking they began with." if changes else "Nothing changed.",
          "notice")
    return RedirectResponse(url=f"/review/{paper_id}", status_code=303)


@router.get("/media/{paper_id}/{filename}")
def question_image(request: Request, paper_id: int, filename: str, db: Session = Depends(get_db)):
    """A question's printed-page snapshot.

    The admin can see any of them (they are the review aid). A student may only see the snapshot of
    a LIVE question that depends on an image — never one from an unpublished, quarantined or
    archived paper, and never the snapshot of an ordinary text question.
    """
    if re.fullmatch(r"page\d{1,4}\.jpg", filename):
        # A whole original page (from a PDF attached to a JSON import): a review aid for the admin, never for students,
        # because it shows other questions and possibly the answers.
        if not request.state.user.is_admin:
            raise HTTPException(status_code=404)
    elif not re.fullmatch(r"q\d{1,4}\.jpg", filename):
        raise HTTPException(status_code=404)
    if not request.state.user.is_admin:
        visible = (
            pool.live_questions(db)
            .filter(
                models.Question.paper_id == paper_id,
                models.Question.source_image_path == filename,
                models.Question.has_image.is_(True),
            )
            .first()
        )
        if visible is None:
            raise HTTPException(status_code=404)
    path = os.path.join(ingest.images_dir_for(paper_id), filename)
    if not os.path.exists(path):
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type="image/jpeg")


@router.post("/papers/{paper_id}/publish", dependencies=[Depends(require_admin)])
def publish_paper(request: Request, paper_id: int, db: Session = Depends(get_db)):
    """Makes a paper's verified questions live, so students can practise them.

    Refused unless every non-quarantined question is verified and has an answer. Questions that
    depend on an image but have no page snapshot stay verified (not live) and are reported.
    """
    paper = db.get(models.Paper, paper_id)
    if not paper:
        raise HTTPException(status_code=404, detail="Paper not found")
    problems = pool.publish_blockers(db, paper)
    if problems:
        flash(request, "This paper can't be published yet: " + " ".join(problems.values()))
        return RedirectResponse(url=f"/review/{paper_id}", status_code=303)

    made_live = held_back = 0
    for q in paper.questions:
        if q.status == models.QStatus.VERIFIED:
            if pool.needs_snapshot(q):
                held_back += 1
                continue
            q.status = models.QStatus.LIVE
            made_live += 1
    paper.publish_status = "published"
    paper.published_at = datetime.utcnow()
    paper.published_by = request.state.user.id
    audit.log(db, request.state.user, "paper.publish", "paper", paper.id, paper_id=paper.id,
              detail={"title": paper.title, "made_live": made_live, "held_back_missing_image": held_back})
    db.commit()

    message = f"“{paper.title}” is published: {made_live} question{'s' if made_live != 1 else ''} went live."
    if held_back:
        message += f" {held_back} depend on an image without a page snapshot and were left out."
    flash(request, message, "notice")
    return RedirectResponse(url=f"/review/{paper_id}", status_code=303)


@router.post("/papers/{paper_id}/unpublish", dependencies=[Depends(require_admin)])
def unpublish_paper(request: Request, paper_id: int, db: Session = Depends(get_db)):
    """Takes the paper away from students. Its questions go back to verified; nothing is deleted."""
    paper = db.get(models.Paper, paper_id)
    if not paper:
        raise HTTPException(status_code=404, detail="Paper not found")
    reverted = 0
    for q in paper.questions:
        if q.status == models.QStatus.LIVE:
            q.status = models.QStatus.VERIFIED
            reverted += 1
    paper.publish_status = "draft"
    paper.published_at = None
    paper.published_by = None
    audit.log(db, request.state.user, "paper.unpublish", "paper", paper.id, paper_id=paper.id,
              detail={"title": paper.title, "questions_reverted": reverted})
    db.commit()
    flash(request, f"“{paper.title}” is no longer visible to students.", "notice")
    return RedirectResponse(url=f"/review/{paper_id}", status_code=303)


@router.post("/papers/{paper_id}/archive", dependencies=[Depends(require_admin)])
def archive_paper(request: Request, paper_id: int, db: Session = Depends(get_db)):
    """Papers are archived, never deleted: the questions, files and history all stay."""
    paper = db.get(models.Paper, paper_id)
    if not paper:
        raise HTTPException(status_code=404, detail="Paper not found")
    if paper.status == "processing":
        flash(request, "This paper is still being read — wait for it to finish, then archive it.")
        return RedirectResponse(url=f"/review/{paper_id}", status_code=303)
    if paper.archived_at is None:
        paper.archived_at = datetime.utcnow()
        audit.log(db, request.state.user, "paper.archive", "paper", paper.id, paper_id=paper.id,
                  detail={"title": paper.title})
        db.commit()
    flash(request, f"“{paper.title}” was archived. Nothing was deleted.", "notice")
    return RedirectResponse(url="/", status_code=303)


@router.post("/papers/{paper_id}/unarchive", dependencies=[Depends(require_admin)])
def unarchive_paper(request: Request, paper_id: int, db: Session = Depends(get_db)):
    paper = db.get(models.Paper, paper_id)
    if not paper:
        raise HTTPException(status_code=404, detail="Paper not found")
    if paper.archived_at is not None:
        paper.archived_at = None
        audit.log(db, request.state.user, "paper.unarchive", "paper", paper.id, paper_id=paper.id,
                  detail={"title": paper.title})
        db.commit()
    flash(request, f"“{paper.title}” was restored.", "notice")
    return RedirectResponse(url="/", status_code=303)
