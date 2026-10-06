"""Admin: apply an answer key to a paper — pasted text, an uploaded key file, or a key block found at the end of the question PDF.

Two steps. PREVIEW parses the key and compares it with the paper's questions without changing anything. APPLY then writes:
by default it only FILLS questions that have no answer; replacing an answer that is already there needs an explicit tick. Changing
a confirmed question's answer sends it back to review, and every change keeps the old version in the question's history."""
import hashlib
import json
import os
import re
import shutil
import time
import uuid
from collections import Counter

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app import audit, duplicates, ingest, key_parse, models, sample_audit, text_extract, versions
from app.database import DATA_DIR, get_db
from app.models import QStatus
from app.web import flash, require_admin, templates

router = APIRouter(dependencies=[Depends(require_admin)])

KEY_DIR = os.path.join(DATA_DIR, "key_imports")
KEEP_SECONDS = 24 * 3600
TOKEN = re.compile(r"[0-9a-f]{32}")
SPREAD_WARN_SHARE = 0.5          # more than half of the answers being one letter is suspicious...
SPREAD_MIN_ANSWERS = 10          # ...but only worth saying with enough answers to judge

SOURCE_LABELS = {
    "inline": "printed under each question", "key_pdf": "a key file", "pasted": "a pasted key", "paper_end": "the key at the end of the paper",
    "json": "AI-supplied (JSON)", "manual": "typed by hand", "file": "an imported file",
}


# --------------------------------------------------------------------------- staging and comparing

def _paper(db: Session, paper_id: int) -> models.Paper:
    paper = db.get(models.Paper, paper_id)
    if not paper:
        raise HTTPException(status_code=404, detail="Paper not found")
    return paper


def _dir(token: str) -> str:
    if not TOKEN.fullmatch(token or ""):
        raise HTTPException(status_code=404, detail="That preview doesn't exist")
    path = os.path.join(KEY_DIR, token)
    if not os.path.isdir(path):
        raise HTTPException(status_code=404, detail="That preview has expired — start again")
    return path


def _sweep_old() -> None:
    if os.path.isdir(KEY_DIR):
        for name in os.listdir(KEY_DIR):
            path = os.path.join(KEY_DIR, name)
            if os.path.isdir(path) and time.time() - os.path.getmtime(path) > KEEP_SECONDS:
                shutil.rmtree(path, ignore_errors=True)


def active_questions(paper: models.Paper) -> dict[int, models.Question]:
    return {q.question_number: q for q in paper.questions if q.question_number is not None and q.status != QStatus.QUARANTINED}


def compare(paper: models.Paper, parse: key_parse.KeyParse) -> dict:
    """What applying this key would do to this paper's questions."""
    questions = active_questions(paper)
    new, same, different, extra = [], [], [], []
    for number, letter in sorted(parse.answers.items()):
        q = questions.get(number)
        if q is None:
            extra.append(number)
        elif not q.correct_answer:
            new.append(number)
        elif q.correct_answer == letter:
            same.append(number)
        else:
            different.append({"number": number, "current": q.correct_answer, "key": letter, "status": q.status})
    missing = [n for n in sorted(questions) if n not in parse.answers]
    counts = Counter(parse.answers.values())
    top_letter, top_count = counts.most_common(1)[0] if counts else (None, 0)
    spread = None
    if len(parse.answers) >= SPREAD_MIN_ANSWERS and top_count / len(parse.answers) > SPREAD_WARN_SHARE:
        spread = {"letter": top_letter, "share": round(100 * top_count / len(parse.answers))}
    mismatches = [n for n, q in questions.items() if n in parse.answers and q.explanation_says and q.explanation_says != parse.answers[n]]
    return {
        "questions": len(questions), "found": len(parse.answers), "new": new, "same": same, "different": different, "extra": extra,
        "missing": missing, "distribution": {letter: counts.get(letter, 0) for letter in "ABCDE"}, "spread": spread,
        "mismatches": mismatches,
    }


def answer_summary(paper: models.Paper) -> dict:
    """The answers a paper has now, by where they came from — shown on its review page."""
    questions = active_questions(paper)
    by_source = Counter((q.answer_source or "manual") for q in questions.values() if q.correct_answer)
    with_answer = sum(by_source.values())
    letters = Counter(q.correct_answer for q in questions.values() if q.correct_answer)
    top_letter, top_count = letters.most_common(1)[0] if letters else (None, 0)
    return {
        "total": len(questions), "with_answer": with_answer, "without": len(questions) - with_answer,
        "sources": [(SOURCE_LABELS.get(s, s), n) for s, n in by_source.most_common()],
        "spread": {"letter": top_letter, "share": round(100 * top_count / with_answer)}
        if with_answer >= SPREAD_MIN_ANSWERS and top_count / with_answer > SPREAD_WARN_SHARE else None,
        "mismatches": sorted(n for n, q in questions.items()
                             if q.correct_answer and q.explanation_says and q.explanation_says != q.correct_answer),
        "key_source": paper.key_source, "key_version": paper.key_version,
        "count_off": paper.expected_total is not None and paper.expected_total != len(questions),
    }


# --------------------------------------------------------------------------- pages

def _form_page(request: Request, db: Session, paper: models.Paper, error: str | None = None, status_code: int = 200,
               form: dict | None = None):
    return templates.TemplateResponse(
        "answer_key.html",
        {"request": request, "paper": paper, "summary": answer_summary(paper), "error": error, "preview": None,
         "form": form or {}, "flash": request.session.pop("flash", None),
         "can_read_paper": bool(paper.source_pdf_path and os.path.exists(paper.source_pdf_path))},
        status_code=status_code,
    )


@router.get("/review/{paper_id}/key")
def key_form(request: Request, paper_id: int, db: Session = Depends(get_db)):
    return _form_page(request, db, _paper(db, paper_id))


@router.post("/review/{paper_id}/key/preview")
def key_preview(
    request: Request, paper_id: int, pasted: str = Form(""), from_paper: bool = Form(False), key_source: str = Form(""),
    key_version: str = Form(""), key_file: UploadFile | None = File(None), db: Session = Depends(get_db),
):
    paper = _paper(db, paper_id)
    form = {"pasted": pasted, "key_source": key_source, "key_version": key_version}
    has_file = key_file is not None and bool(key_file.filename)
    chosen = [name for name, on in (("pasted", bool(pasted.strip())), ("file", has_file), ("paper", from_paper)) if on]
    if not chosen:
        return _form_page(request, db, paper, "Paste a key, choose a key file, or tick “look inside the question PDF”.", 400, form)
    if len(chosen) > 1:
        return _form_page(request, db, paper, "Use one source at a time — paste, upload or read from the question PDF.", 400, form)

    if chosen[0] == "pasted":
        parse, label, source, data = key_parse.parse_key_text(pasted, paper.series), "pasted text", "pasted", pasted.encode("utf-8")
    elif chosen[0] == "file":
        data = key_file.file.read()
        name = os.path.basename((key_file.filename or "key").replace("\\", "/"))
        parse, label, source = key_parse.parse_key_bytes(data, name, paper.series), name, "key_pdf"
    else:
        if not paper.source_pdf_path or not os.path.exists(paper.source_pdf_path):
            return _form_page(request, db, paper, "This paper has no question PDF on file to look inside.", 400, form)
        block = text_extract.find_key_block(paper.source_pdf_path)
        if not block:
            return _form_page(request, db, paper, "No answer-key block was found at the end of the question PDF. "
                                                  "(It has to start with a line like “ANSWER KEY” followed by the answers.)", 400, form)
        parse, label, source, data = key_parse.parse_key_text(block, paper.series), "the end of the question PDF", "paper_end", block.encode("utf-8")

    _sweep_old()
    token = uuid.uuid4().hex
    path = os.path.join(KEY_DIR, token)
    os.makedirs(path)
    with open(os.path.join(path, "parsed.json"), "w", encoding="utf-8") as f:
        json.dump({"paper_id": paper.id, "format": parse.format, "series": parse.series, "label": label, "answer_source": source,
                   "sha256": hashlib.sha256(data).hexdigest(), "answers": {str(k): v for k, v in parse.answers.items()},
                   "explanations": {str(k): v for k, v in parse.explanations.items()},
                   "errors": len(parse.errors)}, f)
    return templates.TemplateResponse(
        "answer_key.html",
        {"request": request, "paper": paper, "summary": answer_summary(paper), "error": None, "form": form, "flash": None,
         "can_read_paper": bool(paper.source_pdf_path and os.path.exists(paper.source_pdf_path)),
         "preview": {"token": token, "parse": parse, "compare": compare(paper, parse), "label": label,
                     "explanations": len(parse.explanations)}},
    )


@router.post("/review/{paper_id}/key/apply")
def key_apply(
    request: Request, paper_id: int, token: str = Form(...), key_source: str = Form(""), key_version: str = Form(""),
    replace: bool = Form(False), db: Session = Depends(get_db),
):
    paper = _paper(db, paper_id)
    path = _dir(token)
    staged = json.load(open(os.path.join(path, "parsed.json"), encoding="utf-8"))
    if staged["paper_id"] != paper.id:
        raise HTTPException(status_code=404, detail="That preview belongs to a different paper")
    if staged["errors"]:
        raise HTTPException(status_code=400, detail="That key has errors and can't be applied")
    source_text = key_source.strip()
    parse = key_parse.KeyParse(format=staged["format"], series=staged["series"],
                               answers={int(k): v for k, v in staged["answers"].items()},
                               explanations={int(k): v for k, v in staged["explanations"].items()})
    if not source_text:
        flash(request, "Say where this key came from (for example “institute answer sheet” or “UPSC final key”) before applying it.")
        return RedirectResponse(url=f"/review/{paper_id}/key", status_code=303)

    user = request.state.user
    questions = active_questions(paper)
    counts = {"filled": 0, "replaced": 0, "kept": 0, "unchanged": 0, "sent_back": 0, "explanations": 0, "ignored": 0}
    changed_ids = []
    for number, letter in sorted(parse.answers.items()):
        q = questions.get(number)
        if q is None:
            counts["ignored"] += 1
            continue
        if letter == "E" and not q.option_e:
            counts["ignored"] += 1
            continue
        current = q.correct_answer
        if current == letter:
            counts["unchanged"] += 1
            continue
        if current and not replace:
            counts["kept"] += 1
            continue
        versions.snapshot(db, q, user, "answer key applied")
        q.correct_answer, q.answer_source = letter, staged["answer_source"]
        changed_ids.append(q.id)
        counts["replaced" if current else "filled"] += 1
        if q.status in (QStatus.VERIFIED, QStatus.LIVE):              # a confirmed answer changed: it must be confirmed again
            q.status, q.reviewed_by, q.reviewed_at, q.flags_acknowledged = QStatus.NEEDS_REVIEW, None, None, False
            counts["sent_back"] += 1
        q.ocr_flags = ",".join(f for f in (q.ocr_flags or "").split(",")
                               if f and f not in ("no_answer_found", "answer_unclear", "no_answer_in_key")) or None
        key_parse.refresh_mismatch_flag(q)
    for number, text in parse.explanations.items():                   # explanations only ever fill gaps, and stay unverified
        q = questions.get(number)
        if q is not None and not q.explanation:
            q.explanation, q.explanation_status = text, "unverified"
            q.explanation_says = key_parse.explanation_says(text)
            counts["explanations"] += 1

    if counts["sent_back"]:
        sample_audit.invalidate(paper)
    db.flush()
    duplicates.refresh_around(db, changed_ids)                        # a changed answer may now disagree with (or stop disagreeing with) another copy
    paper.key_source, paper.key_version = source_text, key_version.strip() or None
    audit.log(db, user, "key.apply", "paper", paper.id, paper_id=paper.id, detail={
        "source": source_text, "version": paper.key_version, "from": staged["label"], "format": staged["format"], "series": staged["series"],
        "sha256": staged["sha256"][:16], "replace": replace, **counts})
    db.commit()
    shutil.rmtree(path, ignore_errors=True)

    parts = [f"{counts['filled']} filled"]
    if counts["replaced"]:
        parts.append(f"{counts['replaced']} replaced")
    if counts["kept"]:
        parts.append(f"{counts['kept']} left as they were (they already had a different answer)")
    if counts["sent_back"]:
        parts.append(f"{counts['sent_back']} confirmed question{'s' if counts['sent_back'] != 1 else ''} sent back to review")
    if counts["explanations"]:
        parts.append(f"{counts['explanations']} explanation{'s' if counts['explanations'] != 1 else ''} added (unverified)")
    flash(request, "Answer key applied: " + ", ".join(parts) + ".", "notice")
    return RedirectResponse(url=f"/review/{paper_id}", status_code=303)
