"""Admin: import questions from a spreadsheet (CSV / XLSX), a Word document (DOCX) or page pictures. See app/file_import.py.

Two steps, like the JSON import: the file is checked and shown first (nothing is saved), then imported. Everything is saved as needs-review."""
import hashlib
import json
import os
import re
import shutil
import time
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app import audit, backup, file_import, ingest, models, ocr_extract
from app.database import DATA_DIR, get_db
from app.routes.papers import PRESETS, _existing_same_test, _parse_scheme
from app.web import flash, require_admin, templates

router = APIRouter(dependencies=[Depends(require_admin)])

IMPORT_DIR = os.path.join(DATA_DIR, "file_imports")
KEEP_SECONDS = 24 * 3600
TOKEN = re.compile(r"[0-9a-f]{32}")
PREVIEW_ROWS = 6
SAMPLE_QUESTIONS = 5
METADATA = ("title", "source_type", "source_name", "test_name", "test_number", "series", "year", "exam_type", "expected_total",
            "marks_per_question", "negative_fraction", "duration_minutes", "key_source", "key_version")


# --------------------------------------------------------------------------- staging

def _dir(token: str) -> str:
    if not TOKEN.fullmatch(token or ""):
        raise HTTPException(status_code=404, detail="That import session doesn't exist")
    path = os.path.join(IMPORT_DIR, token)
    if not os.path.isdir(path):
        raise HTTPException(status_code=404, detail="That import session has expired — start again")
    return path


def _sweep_old() -> None:
    if os.path.isdir(IMPORT_DIR):
        for name in os.listdir(IMPORT_DIR):
            path = os.path.join(IMPORT_DIR, name)
            if os.path.isdir(path) and time.time() - os.path.getmtime(path) > KEEP_SECONDS:
                shutil.rmtree(path, ignore_errors=True)


def _meta(token: str) -> dict:
    return json.load(open(os.path.join(_dir(token), "meta.json"), encoding="utf-8"))


def _files(token: str, meta: dict) -> list[bytes]:
    path = _dir(token)
    return [open(os.path.join(path, f"file_{i:02d}{ext}"), "rb").read() for i, ext in enumerate(meta["exts"])]


def _subject_names(db: Session) -> list[str]:
    return [s.name for s in db.query(models.Subject).order_by(models.Subject.id).all()]


def _topics(db: Session) -> dict[str, set[str]]:
    names = {s.id: s.name for s in db.query(models.Subject).all()}
    out: dict[str, set[str]] = {}
    for t in db.query(models.Topic).all():
        out.setdefault(names.get(t.subject_id, ""), set()).add(t.name.lower())
    return out


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


# --------------------------------------------------------------------------- pages

def _upload_page(request: Request, db: Session, error: str | None = None, status_code: int = 200):
    papers = (db.query(models.Paper).filter(models.Paper.archived_at.is_(None), models.Paper.status == "ready")
              .order_by(models.Paper.created_at.desc()).all())
    return templates.TemplateResponse("file_import.html", {"request": request, "error": error, "papers": papers,
                                                           "flash": request.session.pop("flash", None)}, status_code=status_code)


@router.get("/admin/import/file")
def upload_form(request: Request, db: Session = Depends(get_db)):
    return _upload_page(request, db)


def _prepare(db: Session, meta: dict, files: list[bytes], form: dict, switch_sheet: bool = False) -> dict:
    """Everything the report page shows for a staged file: the table and mapping, or the parsed document."""
    ctx: dict = {"kind": meta["kind"], "table": None, "mapping": {}, "remembered": None, "parsed": None, "images": len(files)}
    if meta["kind"] == "table":
        sheet = form.get("sheet") or None
        table = file_import.read_table(files[0], meta["names"][0], sheet)
        ctx["table"] = table
        submitted = {} if switch_sheet else file_import.read_mapping_form(form, len(table.headers))
        if submitted:
            mapping = submitted
        else:
            mapping, ctx["remembered"] = file_import.recall_mapping(db, table.headers)
            if not mapping:
                mapping = file_import.guess_mapping(table.headers)
        ctx["mapping"] = mapping
        ctx["parsed"] = file_import.parse_rows(table, mapping, _subject_names(db), _topics(db),
                                               method="xlsx" if meta["names"][0].lower().endswith((".xlsx", ".xlsm")) else "csv")
    elif meta["kind"] == "docx":
        ctx["parsed"] = file_import.parse_docx(files[0])
    return ctx


def _page(request: Request, db: Session, token: str, meta: dict, ctx: dict, form: dict | None = None, error: str | None = None,
          status_code: int = 200):
    paper = _target_paper(db, str(meta["paper_id"])) if meta.get("paper_id") else None
    stem = os.path.splitext(meta["names"][0])[0].replace("_", " ").strip()
    defaults = {"title": stem, "source_type": "", "source_name": "", "test_name": "", "test_number": "", "series": "", "year": "",
                "exam_type": "full_length", "expected_total": "", "marks_per_question": "", "negative_fraction": "", "duration_minutes": "",
                "key_source": "", "key_version": ""}
    defaults.update({k: v for k, v in (form or {}).items() if k in METADATA})
    return templates.TemplateResponse(
        "file_report.html",
        {"request": request, "token": token, "meta": meta, "paper": paper, "form": defaults, "error": error, "presets": PRESETS,
         "source_types": models.SourceType.LABELS, "fields": file_import.FIELDS, "sample": SAMPLE_QUESTIONS, "preview_rows": PREVIEW_ROWS,
         "shown_problems": file_import.SHOWN_PROBLEMS, "flash": request.session.pop("flash", None), **ctx},
        status_code=status_code,
    )


@router.post("/admin/import/file/read")
async def read_file(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    target = str(form.get("target") or "new")
    uploads = [(f.filename, await f.read()) for f in form.getlist("files") if hasattr(f, "filename") and f.filename]
    if not uploads:
        return _upload_page(request, db, "Choose a file first.", 400)
    kinds = {file_import.kind_of(name) for name, _ in uploads}
    if None in kinds:
        bad = next(n for n, _ in uploads if file_import.kind_of(n) is None)
        return _upload_page(request, db, f"“{bad}” isn't a kind of file this can read. Use .csv, .xlsx, .docx, or pictures (.png, .jpg …).", 400)
    if len(kinds) > 1:
        return _upload_page(request, db, "Choose one kind of file at a time (a spreadsheet, a document, or pictures).", 400)
    kind = kinds.pop()
    if kind != "images" and len(uploads) > 1:
        return _upload_page(request, db, "Only pictures can be sent several at a time; choose one spreadsheet or one document.", 400)
    if kind == "images" and target not in ("", "new"):
        return _upload_page(request, db, "Pictures are read as a new paper; they can't be added to an existing one.", 400)
    paper = _target_paper(db, target)
    if any(not data for _, data in uploads):
        return _upload_page(request, db, "One of the files is empty.", 400)

    _sweep_old()
    token = uuid.uuid4().hex
    path = os.path.join(IMPORT_DIR, token)
    os.makedirs(path)
    meta = {"kind": kind, "names": [n for n, _ in uploads], "paper_id": paper.id if paper else None,
            "exts": [os.path.splitext(n)[1].lower() or ".bin" for n, _ in uploads],
            "sha256": [hashlib.sha256(d).hexdigest() for _, d in uploads]}
    for i, (_, data) in enumerate(uploads):
        with open(os.path.join(path, f"file_{i:02d}{meta['exts'][i]}"), "wb") as fh:
            fh.write(data)
    with open(os.path.join(path, "meta.json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh)
    try:
        ctx = _prepare(db, meta, [d for _, d in uploads], {})
    except file_import.FileImportError as e:
        shutil.rmtree(path, ignore_errors=True)
        return _upload_page(request, db, str(e), 400)
    return _page(request, db, token, meta, ctx)


@router.post("/admin/import/file/preview")
async def preview(request: Request, db: Session = Depends(get_db)):
    form = dict(await request.form())
    token = str(form.get("token", ""))
    meta = _meta(token)
    try:
        ctx = _prepare(db, meta, _files(token, meta), form, switch_sheet="switch_sheet" in form)
    except file_import.FileImportError as e:
        return _page(request, db, token, meta, {"kind": meta["kind"], "table": None, "mapping": {}, "remembered": None, "parsed": None,
                                                 "images": len(meta["names"])}, form, str(e), 400)
    return _page(request, db, token, meta, ctx, form)


@router.post("/admin/import/file/apply")
async def apply(request: Request, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    form = dict(await request.form())
    token = str(form.get("token", ""))
    meta = _meta(token)
    files = _files(token, meta)
    paper = _target_paper(db, str(meta["paper_id"])) if meta.get("paper_id") else None
    allow_duplicate = str(form.get("allow_duplicate", "")).lower() == "true"

    def g(name: str) -> str:
        return str(form.get(name, "") or "").strip()

    def refuse(message: str, ctx=None):
        try:
            ctx = ctx or _prepare(db, meta, files, form)
        except file_import.FileImportError as e:
            message = str(e)
            ctx = {"kind": meta["kind"], "table": None, "mapping": {}, "remembered": None, "parsed": None, "images": len(files)}
        return _page(request, db, token, meta, ctx, form, message, 400)

    try:
        ctx = _prepare(db, meta, files, form)
    except file_import.FileImportError as e:
        return refuse(str(e))
    parsed = ctx["parsed"]
    if parsed is not None and not parsed.ok:
        return refuse("The import can't go ahead until the problems listed below are fixed.", ctx)

    scheme = None
    exam_kind = year_value = None
    if paper is None:
        if not g("title"):
            return refuse("Give the new paper a title.", ctx)
        try:
            scheme = _parse_scheme(g("expected_total"), g("marks_per_question"), g("negative_fraction"), g("duration_minutes"))
            exam_kind = models.ExamType(g("exam_type") or "full_length")
            year_value = int(g("year")) if g("year") else None
        except ValueError as e:
            return refuse(str(e) if str(e) else "Check the paper type, the year and the marking scheme.", ctx)
        if g("source_type") and g("source_type") not in models.SourceType.ALL:
            return refuse("Choose Official PYQ or Coaching test as the source.", ctx)
        digest = "file:" + hashlib.sha256("".join(meta["sha256"]).encode()).hexdigest()
        if not allow_duplicate:
            same_file = db.query(models.Paper).filter(models.Paper.file_hash == digest).first()
            if same_file:
                return refuse(f"This exact file was already imported as “{same_file.title}”. Tick “Import anyway” to import it again.", ctx)
            same_test = _existing_same_test(db, g("source_name"), g("test_name"), g("test_number"), g("series"))
            if same_test:
                return refuse(f"A paper for this test already exists: “{same_test.title}”. Tick “Import anyway” if this is a different "
                              "set of questions for the same test.", ctx)

    if meta["kind"] == "images":
        try:
            ocr_extract.tesseract_cmd()
        except ocr_extract.OcrUnavailable as e:
            return refuse(str(e), ctx)

    backup_name = backup.create_backup("auto")
    if meta["kind"] == "images":
        os.makedirs(ingest.PDF_DIR, exist_ok=True)
        pdf_path = os.path.join(ingest.PDF_DIR, f"{time.strftime('%Y%m%d%H%M%S')}_pictures_{token[:8]}.pdf")
        try:
            pages = file_import.images_to_pdf(files, pdf_path)
        except file_import.FileImportError as e:
            return refuse(str(e), ctx)
        paper = models.Paper(
            title=g("title"), year=year_value, exam_type=exam_kind, status="processing", source_type=g("source_type") or None,
            source_name=g("source_name") or None, test_name=g("test_name") or None, test_number=g("test_number") or None,
            series=g("series").upper()[:1] or None, **scheme, key_source=g("key_source") or None, key_version=g("key_version") or None,
            file_hash=digest, source_pdf_path=pdf_path, layout="auto")
        db.add(paper)
        db.flush()
        audit.log(db, request.state.user, "paper.file_import", "paper", paper.id, paper_id=paper.id,
                  detail={"kind": "images", "files": meta["names"], "pages": pages, "sha256": [h[:16] for h in meta["sha256"]], "backup": backup_name})
        db.commit()
        shutil.rmtree(_dir(token), ignore_errors=True)
        background_tasks.add_task(ingest.process_paper, paper.id, None, "auto")
        flash(request, f"{pages} picture{'s' if pages != 1 else ''} are being read as a scanned paper (OCR). This page refreshes by itself.", "notice")
        return RedirectResponse(url=f"/review/{paper.id}", status_code=303)

    created_paper = paper is None
    if paper is None:
        answered = any(q["answer"] for q in parsed.questions)
        paper = models.Paper(
            title=g("title"), year=year_value, exam_type=exam_kind, status="ready", source_type=g("source_type") or None,
            source_name=g("source_name") or None, test_name=g("test_name") or None, test_number=g("test_number") or None,
            series=g("series").upper()[:1] or None, **scheme,
            key_source=g("key_source") or (f"Imported {meta['kind']} file — unverified" if answered else None),
            key_version=g("key_version") or None, file_hash=digest)
        db.add(paper)
        db.flush()
    counts = file_import.save_questions(db, request.state.user, parsed, paper)
    remembered = None
    if meta["kind"] == "table":
        table = ctx["table"]
        file_import.remember_mapping(db, table.headers, ctx["mapping"])
        remembered = {f: table.headers[i] for f, i in ctx["mapping"].items()}
    audit.log(db, request.state.user, "paper.file_import", "paper", paper.id, paper_id=paper.id, detail={
        "kind": meta["kind"], "format": parsed.method, "files": meta["names"], "sha256": [h[:16] for h in meta["sha256"]],
        "new_paper": created_paper, "warnings": len(parsed.warnings), "backup": backup_name, "mapping": remembered, **counts})
    db.commit()
    shutil.rmtree(_dir(token), ignore_errors=True)

    message = (f"Imported {counts['created']} question{'s' if counts['created'] != 1 else ''} from {meta['names'][0]} — "
               "each one needs your review before students can see it. There is no original page to compare with, so check them against your source.")
    if counts["skipped_existing"]:
        message += f" {counts['skipped_existing']} number{'s were' if counts['skipped_existing'] != 1 else ' was'} already in the paper and skipped."
    found = counts["duplicates"]["exact"] + counts["duplicates"]["near"]
    if found:
        message += f" {found} possible duplicate{'s' if found != 1 else ''} found — see Duplicates."
    flash(request, message, "notice")
    return RedirectResponse(url=f"/review/{paper.id}", status_code=303)
