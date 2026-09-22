"""Admin: import questions from JSON (usually produced by another AI). Two steps — validate (nothing is saved), then import.

Everything from JSON is saved as `needs_review` with source `ai_json` and goes through the normal review and publish gates.
See app/json_import.py for the rules."""
import hashlib
import json
import os
import re
import shutil
import time
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from app import audit, backup, duplicates, ingest, json_import, models
from app.database import DATA_DIR, get_db
from app.routes.papers import PRESETS, _existing_same_test, _parse_scheme
from app.web import flash, require_admin, templates

router = APIRouter(dependencies=[Depends(require_admin)])

IMPORT_DIR = os.path.join(DATA_DIR, "json_imports")
KEEP_SECONDS = 24 * 3600
TOKEN = re.compile(r"[0-9a-f]{32}")


# --------------------------------------------------------------------------- staging of the uploaded parts

def _dir(token: str) -> str:
    if not TOKEN.fullmatch(token or ""):
        raise HTTPException(status_code=404, detail="That import session doesn't exist")
    path = os.path.join(IMPORT_DIR, token)
    if not os.path.isdir(path):
        raise HTTPException(status_code=404, detail="That import session has expired — start again")
    return path


def _sweep_old() -> None:
    if not os.path.isdir(IMPORT_DIR):
        return
    for name in os.listdir(IMPORT_DIR):
        path = os.path.join(IMPORT_DIR, name)
        if os.path.isdir(path) and time.time() - os.path.getmtime(path) > KEEP_SECONDS:
            shutil.rmtree(path, ignore_errors=True)


def _read_parts(token: str) -> list[tuple[str, bytes]]:
    path = _dir(token)
    meta = json.load(open(os.path.join(path, "meta.json"), encoding="utf-8"))
    return [(name, open(os.path.join(path, f"part_{i:02d}.json"), "rb").read()) for i, name in enumerate(meta["names"], start=1)]


def _meta(token: str) -> dict:
    return json.load(open(os.path.join(_dir(token), "meta.json"), encoding="utf-8"))


# --------------------------------------------------------------------------- what the validator needs from the database

def _subject_names(db: Session) -> list[str]:
    return [s.name for s in db.query(models.Subject).order_by(models.Subject.id).all()]


def _topics(db: Session) -> dict[str, set[str]]:
    names = {s.id: s.name for s in db.query(models.Subject).all()}
    out: dict[str, set[str]] = {}
    for t in db.query(models.Topic).all():
        out.setdefault(names.get(t.subject_id, ""), set()).add(t.name.lower())
    return out


def _known_hashes(db: Session) -> dict[str, str]:
    """{norm_hash: 'Paper title Q7'} for every question in a paper that isn't archived (quarantined ones excluded)."""
    rows = (
        db.query(models.Question, models.Paper.title)
        .join(models.Paper, models.Paper.id == models.Question.paper_id)
        .filter(models.Paper.archived_at.is_(None), models.Question.status != models.QStatus.QUARANTINED)
        .all()
    )
    known: dict[str, str] = {}
    for q, title in rows:
        h = q.norm_hash or duplicates.norm_hash(q)
        known.setdefault(h, f"“{title}” Q{q.question_number}")
    return known


def _target_paper(db: Session, target: str) -> models.Paper | None:
    if target in ("", "new"):
        return None
    try:
        paper = db.get(models.Paper, int(target))
    except ValueError:
        paper = None
    if paper is None or paper.archived_at is not None or paper.status != "ready":
        raise HTTPException(status_code=404, detail="That paper can't be added to (it doesn't exist, is archived or isn't ready)")
    return paper


def _report_for(db: Session, token: str, paper: models.Paper | None, later_wins: bool, expected_total: int | None = None):
    existing = {q.question_number: q.status for q in paper.questions if q.question_number is not None} if paper else None
    return json_import.build_report(
        _read_parts(token), _subject_names(db),
        expected_total=expected_total or (paper.expected_total if paper else None),
        later_wins=later_wins, topics=_topics(db), existing_numbers=existing, known_hashes=_known_hashes(db),
    )


# --------------------------------------------------------------------------- pages

def _form_page(request: Request, db: Session, error: str | None = None, status_code: int = 200):
    papers = db.query(models.Paper).filter(models.Paper.archived_at.is_(None), models.Paper.status == "ready") \
        .order_by(models.Paper.created_at.desc()).all()
    return templates.TemplateResponse(
        "json_import.html",
        {"request": request, "error": error, "papers": papers,
         "prompt": json_import.prompt_text(_subject_names(db)), "template": json_import.TEMPLATE_TEXT,
         "flash": request.session.pop("flash", None)},
        status_code=status_code,
    )


@router.get("/admin/import/json")
def json_import_form(request: Request, db: Session = Depends(get_db)):
    return _form_page(request, db)


@router.get("/admin/import/json/template")
def download_template():
    return Response(json_import.TEMPLATE_TEXT + "\n", media_type="application/json",
                    headers={"Content-Disposition": 'attachment; filename="question_import_template.json"'})


@router.get("/admin/import/json/prompt")
def download_prompt(db: Session = Depends(get_db)):
    return Response(json_import.prompt_text(_subject_names(db)), media_type="text/plain; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="prompt_for_another_ai.txt"'})


def _report_page(request: Request, db: Session, token: str, paper: models.Paper | None, report: json_import.Report,
                 later_wins: bool, form: dict | None = None, error: str | None = None, status_code: int = 200):
    meta = _meta(token)
    defaults = {
        "title": report.paper.get("title") or "", "source_type": report.paper.get("source_type") or "",
        "source_name": report.paper.get("source_name") or "", "year": report.paper.get("year") or "",
        "series": str(report.paper.get("series") or "").upper()[:1], "expected_total": report.paper.get("expected_total") or "",
        "exam_type": "full_length", "test_name": "", "test_number": "", "key_source": "", "key_version": "",
    }
    defaults.update(form or {})
    rows = list(report.questions.values())
    return templates.TemplateResponse(
        "json_report.html",
        {"request": request, "token": token, "paper": paper, "report": report, "later_wins": later_wins, "form": defaults,
         "meta": meta, "rows": rows, "hidden_rows": 0, "error": error,
         "presets": PRESETS, "source_types": models.SourceType.LABELS, "subjects": _subject_names(db),
         "flags": ingest.FLAG_LABELS, "flags_for": json_import.flags_for,
         "counts": _counts(report), "flash": request.session.pop("flash", None)},
        status_code=status_code,
    )


def _counts(report: json_import.Report) -> dict:
    qs = report.questions.values()
    return {
        "questions": len(report.questions),
        "with_answer": sum(1 for q in qs if q["answer"]), "without_answer": sum(1 for q in qs if not q["answer"]),
        "with_explanation": sum(1 for q in qs if q["explanation"]), "uncertain": sum(1 for q in qs if q["uncertain"]),
        "images": sum(1 for q in qs if q["has_image"]), "no_subject": sum(1 for q in qs if not q["subject"]),
        "both": sum(1 for q in qs if q["text"] and json_import._has_hi(q)),
        "english_only": sum(1 for q in qs if q["text"] and not json_import._has_hi(q)),
        "hindi_only": sum(1 for q in qs if not q["text"] and json_import._has_hi(q)),
        "language_flagged": sum(1 for q in qs if q.get("lang_flags")),
        "existing": len(report.existing),
    }


@router.post("/admin/import/json/validate")
async def validate(
    request: Request,
    target: str = Form("new"),
    pasted: str = Form(""),
    later_wins: bool = Form(False),
    files: list[UploadFile] = File(default=[]),
    pdf_file: UploadFile | None = File(None),
    db: Session = Depends(get_db),
):
    """The dry run: reads and checks every part, saves nothing to the database."""
    paper = _target_paper(db, target)
    parts: list[tuple[str, bytes]] = []
    for upload in files:
        if upload.filename:
            parts.append((os.path.basename(upload.filename.replace("\\", "/")), upload.file.read()))
    if pasted.strip():
        parts.append(("Pasted text", pasted.encode("utf-8")))
    if not parts:
        return _form_page(request, db, "Upload at least one JSON file or paste some JSON.", 400)
    pdf_bytes, pdf_name = None, None
    if pdf_file is not None and pdf_file.filename:
        pdf_bytes = pdf_file.file.read()
        if pdf_bytes[:5] != b"%PDF-":
            return _form_page(request, db, f"“{pdf_file.filename}” is not a PDF file.", 400)
        pdf_name = os.path.basename(pdf_file.filename.replace("\\", "/"))

    _sweep_old()
    token = uuid.uuid4().hex
    path = os.path.join(IMPORT_DIR, token)
    os.makedirs(path)
    for i, (name, data) in enumerate(parts, start=1):
        with open(os.path.join(path, f"part_{i:02d}.json"), "wb") as f:
            f.write(data)
    if pdf_bytes:
        with open(os.path.join(path, "paper.pdf"), "wb") as f:
            f.write(pdf_bytes)
    with open(os.path.join(path, "meta.json"), "w", encoding="utf-8") as f:
        json.dump({"names": [n for n, _ in parts], "paper_id": paper.id if paper else None, "pdf_name": pdf_name,
                   "sha256": [hashlib.sha256(d).hexdigest() for _, d in parts]}, f)

    report = _report_for(db, token, paper, later_wins)
    return _report_page(request, db, token, paper, report, later_wins)


@router.post("/admin/import/json/apply")
def apply_import(
    request: Request,
    token: str = Form(...),
    later_wins: bool = Form(False),
    overwrite_needs_review: bool = Form(False),
    allow_duplicate: bool = Form(False),
    skip_duplicates: bool = Form(False),
    title: str = Form(""), source_type: str = Form(""), source_name: str = Form(""), test_name: str = Form(""),
    test_number: str = Form(""), series: str = Form(""), year: str = Form(""), exam_type: str = Form("full_length"),
    expected_total: str = Form(""), marks_per_question: str = Form(""), negative_fraction: str = Form(""),
    duration_minutes: str = Form(""), key_source: str = Form(""), key_version: str = Form(""),
    db: Session = Depends(get_db),
):
    meta = _meta(token)
    paper = _target_paper(db, str(meta["paper_id"]) if meta["paper_id"] else "new")
    form = {k: v for k, v in locals().items() if k in (
        "title", "source_type", "source_name", "test_name", "test_number", "series", "year", "exam_type", "expected_total",
        "marks_per_question", "negative_fraction", "duration_minutes", "key_source", "key_version")}

    def refuse(message: str, report=None):
        report = report or _report_for(db, token, paper, later_wins)
        return _report_page(request, db, token, paper, report, later_wins, form, message, 400)

    try:
        scheme = _parse_scheme(expected_total, marks_per_question, negative_fraction, duration_minutes) if paper is None else None
    except ValueError as e:
        return refuse(str(e))
    total = scheme["expected_total"] if scheme else None
    report = _report_for(db, token, paper, later_wins, expected_total=total)
    if not report.ok:
        return refuse("The import can't go ahead until the errors below are fixed (edit the JSON and validate again).", report)

    digest = "json:" + hashlib.sha256("".join(meta["sha256"]).encode()).hexdigest()
    exam_kind = None
    if paper is None:
        if not title.strip():
            return refuse("Give the new paper a title.", report)
        try:
            exam_kind = models.ExamType(exam_type)
            year_value = int(year) if year.strip() else None
        except ValueError:
            return refuse("Check the paper type and the year.", report)
        if source_type and source_type not in models.SourceType.ALL:
            return refuse("Choose Official PYQ or Coaching test as the source.", report)
        if not allow_duplicate:
            same_file = db.query(models.Paper).filter(models.Paper.file_hash == digest).first()
            if same_file:
                return refuse(f"This exact JSON was already imported as “{same_file.title}”. "
                              "Tick “Import anyway” to import it again.", report)
            same_test = _existing_same_test(db, source_name.strip(), test_name.strip(), test_number.strip(), series.strip())
            if same_test:
                return refuse(f"A paper for this test already exists: “{same_test.title}”. Tick “Import anyway” if this is a "
                              "different set of questions for the same test.", report)

    skipped_duplicates = 0
    if skip_duplicates:                                       # only the exact duplicates the report listed; nothing else is dropped
        for number in {d["number"] for d in report.duplicates}:
            if report.questions.pop(number, None) is not None:
                skipped_duplicates += 1
        if not report.questions:
            return refuse("Every question in this JSON is a duplicate of one already imported, so there is nothing left to import.", report)

    backup_name = backup.create_backup("auto")
    pdf_path = pdf_hash = None
    if meta.get("pdf_name"):
        src = os.path.join(_dir(token), "paper.pdf")
        os.makedirs(ingest.PDF_DIR, exist_ok=True)
        stamp = time.strftime("%Y%m%d%H%M%S")
        pdf_path = os.path.join(ingest.PDF_DIR, f"{stamp}_questions_{meta['pdf_name']}")
        shutil.copyfile(src, pdf_path)
        pdf_hash = hashlib.sha256(open(pdf_path, "rb").read()).hexdigest()

    if paper is None:
        answered = any(q["answer"] for q in report.questions.values())
        paper = models.Paper(
            title=title.strip(), year=year_value, exam_type=exam_kind, status="ready", source_type=source_type or None,
            source_name=source_name.strip() or None, test_name=test_name.strip() or None, test_number=test_number.strip() or None,
            series=series.strip().upper() or None, **scheme,
            key_source=key_source.strip() or ("AI-supplied (JSON) — unverified" if answered else None),
            key_version=key_version.strip() or None, file_hash=digest, source_pdf_path=pdf_path,
        )
        db.add(paper)
        db.flush()
        created_paper = True
    else:
        created_paper = False
        if pdf_path and not paper.source_pdf_path:
            paper.source_pdf_path = pdf_path

    counts = json_import.save_questions(db, request.state.user, report, paper, overwrite_needs_review=overwrite_needs_review)
    pictures, picture_problem = 0, None
    if pdf_path:
        pictures, picture_problem = json_import.render_pages(
            pdf_path, ingest.images_dir_for(paper.id), [q["page"] for q in report.questions.values()])
    audit.log(db, request.state.user, "paper.json_import", "paper", paper.id, paper_id=paper.id, detail={
        "title": paper.title, "new_paper": created_paper, "parts": [{"name": n, "sha256": h[:16]} for n, h in zip(meta["names"], meta["sha256"])],
        "pdf": {"name": meta.get("pdf_name"), "sha256": (pdf_hash or "")[:16], "page_pictures": pictures},
        **counts, "warnings": len(report.warnings), "backup": backup_name, "later_wins": later_wins,
        "overwrite_needs_review": overwrite_needs_review, "skipped_duplicates": skipped_duplicates,
    })
    db.commit()
    shutil.rmtree(_dir(token), ignore_errors=True)

    message = (f"Imported {counts['created']} question{'s' if counts['created'] != 1 else ''} as “AI-supplied, unverified” — "
               "each one needs your review before students can see it.")
    if counts["overwritten"]:
        message += f" {counts['overwritten']} waiting question{'s were' if counts['overwritten'] != 1 else ' was'} replaced."
    if counts["skipped_existing"]:
        message += f" {counts['skipped_existing']} number{'s were' if counts['skipped_existing'] != 1 else ' was'} already in the paper and skipped."
    if skipped_duplicates:
        message += f" {skipped_duplicates} duplicate{'s were' if skipped_duplicates != 1 else ' was'} skipped."
    found_dups = counts.get("duplicates", {"exact": 0, "near": 0})
    if found_dups["exact"] + found_dups["near"]:
        message += (f" {found_dups['exact'] + found_dups['near']} possible duplicate"
                    f"{'s' if found_dups['exact'] + found_dups['near'] != 1 else ''} found — see Duplicates.")
    if picture_problem:
        message += f" (Original page pictures couldn't be made: {picture_problem}.)"
    flash(request, message, "notice")
    return RedirectResponse(url=f"/review/{paper.id}", status_code=303)
