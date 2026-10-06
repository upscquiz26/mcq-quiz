"""Admin: validate and import a chapter-wise JSON book as one review-gated collection."""
import hashlib
import json
import os
import re
import shutil
import time
import uuid

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app import audit, backup, book_json_import, duplicates, ingest, models
from app.database import DATA_DIR, get_db
from app.models import QStatus
from app.web import flash, require_admin, templates

router = APIRouter(dependencies=[Depends(require_admin)])
STAGE_DIR = os.path.join(DATA_DIR, "book_json_imports")
TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")
KEEP_SECONDS = 24 * 3600


def _stage(token: str) -> str:
    if not TOKEN_RE.fullmatch(token or ""):
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="That book import doesn't exist")
    path = os.path.join(STAGE_DIR, token)
    if not os.path.isdir(path):
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="That book import expired; start again")
    return path


def _sweep() -> None:
    if not os.path.isdir(STAGE_DIR):
        return
    for name in os.listdir(STAGE_DIR):
        path = os.path.join(STAGE_DIR, name)
        if os.path.isdir(path) and time.time() - os.path.getmtime(path) > KEEP_SECONDS:
            shutil.rmtree(path, ignore_errors=True)


def _subject(db: Session, name: str) -> models.Subject:
    subject = db.query(models.Subject).filter(models.Subject.name.ilike(name)).first()
    if subject is None:
        subject = models.Subject(name=name)
        db.add(subject)
        db.flush()
    return subject


def _topic(db: Session, subject_id: int, name: str) -> models.Topic:
    topic = (db.query(models.Topic)
             .filter(models.Topic.subject_id == subject_id, models.Topic.name.ilike(name)).first())
    if topic is None:
        topic = models.Topic(name=name, subject_id=subject_id)
        db.add(topic)
        db.flush()
    return topic


def _report_page(request: Request, token: str, payload: dict, report: book_json_import.BookImportReport,
                 error: str | None = None, status_code: int = 200):
    return templates.TemplateResponse(
        "book_json_report.html",
        {"request": request, "token": token, "payload": payload, "report": report,
         "error": error, "flags": ingest.FLAG_LABELS,
         "flash": request.session.pop("flash", None)},
        status_code=status_code,
    )


@router.get("/admin/books/import/json")
def book_json_form(request: Request):
    return templates.TemplateResponse(
        "book_json_import.html", {"request": request, "error": None, "flash": request.session.pop("flash", None)}
    )


@router.post("/admin/books/import/json/validate")
async def validate_book_json(
    request: Request,
    title: str = Form(""),
    subject: str = Form(""),
    files: list[UploadFile] = File(default=[]),
):
    parts = []
    for upload in files:
        if not upload.filename:
            continue
        filename = os.path.basename(upload.filename.replace("\\", "/"))
        if not filename.lower().endswith(".json"):
            continue
        parts.append((filename, await upload.read()))
    parts.sort(key=lambda part: part[0].casefold())
    form = {"title": title.strip(), "subject": subject.strip()}
    if not parts:
        return templates.TemplateResponse(
            "book_json_import.html", {"request": request, "error": "Select one or more chapter JSON files.",
                                       "flash": request.session.pop("flash", None)}, status_code=400)

    report = book_json_import.build_report(parts, form["subject"])
    if not form["title"]:
        report.add("error", "Enter a title for this book.")
    if not form["subject"] and not report.subject:
        report.add("error", "Enter the subject for this book.")
    payload = {"title": form["title"], "subject": form["subject"] or report.subject,
               "parts": [{"name": name, "data": data.decode("utf-8-sig", errors="replace")} for name, data in parts],
               "sha256": [hashlib.sha256(data).hexdigest() for _, data in parts]}

    _sweep()
    token = uuid.uuid4().hex
    path = os.path.join(STAGE_DIR, token)
    os.makedirs(path)
    with open(os.path.join(path, "payload.json"), "w", encoding="utf-8") as staged:
        json.dump(payload, staged, ensure_ascii=False)
    with open(os.path.join(path, "report.json"), "w", encoding="utf-8") as staged:
        json.dump(report.as_dict(), staged, ensure_ascii=False)
    return _report_page(request, token, payload, report)


@router.post("/admin/books/import/json/apply")
def apply_book_json(request: Request, token: str = Form(...), db: Session = Depends(get_db)):
    path = _stage(token)
    with open(os.path.join(path, "payload.json"), encoding="utf-8") as staged:
        payload = json.load(staged)
    parts = [(part["name"], part["data"].encode("utf-8")) for part in payload["parts"]]
    report = book_json_import.build_report(parts, payload["subject"])
    if not report.ok:
        return _report_page(request, token, payload, report, "Fix validation errors and upload the corrected chapter files.", 400)

    digest = "book-json:" + hashlib.sha256("".join(payload["sha256"]).encode()).hexdigest()
    duplicate = db.query(models.Paper).filter_by(file_hash=digest).first()
    if duplicate:
        return _report_page(request, token, payload, report,
                            f"These exact chapter files were already imported as {duplicate.title!r}.", 400)

    backup.create_backup("auto")
    subject = _subject(db, payload["subject"])
    paper = models.Paper(
        title=payload["title"], exam_type=models.ExamType.sectional, source_type=models.SourceType.BOOK,
        source_name=payload["subject"], test_name=payload["title"], expected_total=len(report.questions),
        file_hash=digest, status="ready", publish_status="draft",
    )
    db.add(paper)
    db.flush()

    topics = {chapter["name"]: _topic(db, subject.id, chapter["name"]) for chapter in report.chapters}
    for item in report.questions:
        opts = item["options"]
        flags = []
        if item["answer"] is None:
            flags.append("no_answer_found")
        if item["source_flags"]:
            flags.append("book_source_flag")
        question = models.Question(
            paper_id=paper.id, question_number=item["number"], text=item["text"],
            option_a=opts["a"], option_b=opts["b"], option_c=opts["c"], option_d=opts["d"],
            option_e=opts.get("e"), correct_answer=item["answer"], explanation=item["explanation"],
            subject_id=subject.id, topic_id=topics[item["chapter"]].id,
            source_ref=json.dumps({"external_id": item["external_id"], "chapter": item["chapter"],
                                   "chapter_no": item["chapter_no"], "chapter_question_number": item["chapter_number"],
                                   "page": item["page_label"], "citations": item["sources"],
                                   "needs_review": item["source_flags"]}, ensure_ascii=False),
            source="book_json", extraction_method="json", answer_source="book_json" if item["answer"] else None,
            explanation_status="unverified" if item["explanation"] else None,
            status=QStatus.NEEDS_REVIEW, needs_review=True,
            ocr_flags=",".join(flags) or None, uncertain=bool(item["source_flags"]),
        )
        db.add(question)
        db.flush()
        question.norm_hash = duplicates.norm_hash(question)

    audit.log(db, request.state.user, "book.json_import", "paper", paper.id, paper_id=paper.id,
              detail={"title": paper.title, "subject": subject.name, "chapters": len(report.chapters),
                      "questions": len(report.questions), "sha256": payload["sha256"]})
    db.commit()
    shutil.rmtree(path, ignore_errors=True)
    flash(request, f"Imported {len(report.questions)} questions from {len(report.chapters)} chapters. They are draft and need review before students can practise them.", "notice")
    return RedirectResponse(url=f"/review/{paper.id}", status_code=303)
