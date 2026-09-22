"""
Turns an uploaded paper (question PDF + optional answer PDF) into Question rows.
Runs in a background thread so the upload request returns immediately; progress
and the outcome are written to the Paper row (status / pages_done / status_message).
"""
import os
import re

from app import audit, duplicates, key_parse, models, subject_hints, text_extract, versions
from app.answer_key import AnswerKeyError, parse_answer_key
from app.json_import import AI_FLAGS, LANGUAGE_FLAGS
from app.database import DATA_DIR, SessionLocal
from app.ocr_extract import extract_questions

PDF_DIR = os.path.join(DATA_DIR, "pdfs")
IMAGES_DIR = os.path.join(DATA_DIR, "images")

FLAG_LABELS = {
    "options_not_found": "Couldn't find options (a)–(d) — fix the text using the snapshot",
    "empty_stem": "Question text is empty",
    "empty_option": "An option is empty",
    "very_long_option": "An option is very long — two options may have merged",
    "odd_characters": "Odd characters — likely OCR errors",
    "check_code_row": "Code row isn't a permutation of 1–4 — check the digits",
    "no_answer_in_key": "This number isn't in the answer key",
    "hindi_text": "Contains Hindi text — this app reads English only",
    "check_table": "Lists or a table — the reading order may be scrambled; check against the snapshot",
    "no_answer_found": "No answer was printed under this question — set it before publishing",
    "answer_unclear": "The printed answer can't be read (text printed over text) — check the snapshot and set it",
    "maybe_merged": "This looks like two questions merged into one — split it using the snapshot",
    "answer_conflict": "The answer printed in the paper differs from the answer key — check both",
    "explanation_mismatch": "The explanation states a different answer from the key — check both",
    "source_conflict": "Another copy of this question (in another paper) has a different answer — see Duplicates and check both",
    **AI_FLAGS,
    **LANGUAGE_FLAGS,
}


def images_dir_for(paper_id: int) -> str:
    return os.path.join(IMAGES_DIR, str(paper_id))


def parse_subject_ranges(text: str, subject_names: list[str]) -> list[tuple[int, int, str]]:
    """'1-30 History, 31-60 Geography' -> [(1, 30, 'History'), (31, 60, 'Geography')].
    Raises ValueError with a readable message on anything it can't understand."""
    by_lower = {n.lower(): n for n in subject_names}
    ranges = []
    for part in re.split(r"[,;\n]+", text or ""):
        part = part.strip()
        if not part:
            continue
        m = re.fullmatch(r"(\d+)\s*(?:-|–|—|to)\s*(\d+)\s*[:=]?\s*(.+)", part, re.I)
        if not m:
            raise ValueError(f"Couldn't read “{part}”. Use the form: 1-30 History, 31-60 Geography")
        start, end, name = int(m.group(1)), int(m.group(2)), m.group(3).strip()
        if start > end:
            raise ValueError(f"Range {start}-{end} is backwards.")
        if name.lower() not in by_lower:
            raise ValueError(f"Unknown subject “{name}”. Known subjects: {', '.join(subject_names)}")
        ranges.append((start, end, by_lower[name.lower()]))
    return ranges


def apply_subject_ranges(db, paper_id: int, ranges: list[tuple[int, int, str]], user=None,
                         keep_history: bool = False) -> int:
    """Sets the subject on every question in each range (later ranges win). Returns questions changed.
    With keep_history, each changed question gets a version snapshot first, so the change can be undone."""
    subjects = {s.name: s.id for s in db.query(models.Subject).all()}
    questions = db.query(models.Question).filter(models.Question.paper_id == paper_id).all()
    changed = 0
    for q in questions:
        if q.question_number is None:
            continue
        target = None
        for start, end, name in ranges:
            if start <= q.question_number <= end:
                target = subjects[name]           # a later range overrides an earlier one
        if target is not None and q.subject_id != target:
            if keep_history:
                versions.snapshot(db, q, user, "bulk subject change")
            q.subject_id = target
            q.suggested_subject_id = None
            if q.topic is not None and q.topic.subject_id != target:
                q.topic_id = None                       # a topic belongs to one subject
            changed += 1
    return changed


def read_questions(pdf_path: str, images_dir: str, on_progress=None, layout: str = "auto"):
    """Reads a question paper: from its text layer when it has one (exact, fast), by OCR when it is a scan.
    Returns (questions, warnings, method) with method 'text' or 'ocr'. If the text can't be turned into questions
    (or reading it fails) the paper is read by OCR instead, and the warnings say so."""
    warnings: list[str] = []
    if text_extract.has_text_layer(pdf_path):
        try:
            questions, found = text_extract.extract_questions(pdf_path, images_dir, on_progress, layout)
        except Exception as e:                      # a layout the text reader can't handle must not sink the import
            questions, found = [], [f"Reading the PDF's text failed ({type(e).__name__}: {e})."]
        if questions:
            return questions, found, "text"
        warnings.extend(found)
        warnings.append("Fell back to OCR.")
    questions, found = extract_questions(pdf_path, images_dir, on_progress=on_progress)
    return questions, warnings + found, "ocr"


def process_paper(paper_id: int, subject_ranges: list[tuple[int, int, str]] | None = None, layout: str = "auto"):
    db = SessionLocal()
    paper = db.get(models.Paper, paper_id)
    try:
        def progress(done, total):
            paper.pages_done, paper.pages_total = done, total
            db.commit()

        questions, warnings, method = read_questions(
            paper.source_pdf_path, images_dir_for(paper_id), on_progress=progress, layout=layout
        )

        if paper.expected_total and len(questions) != paper.expected_total:
            warnings.append(f"Found {len(questions)} questions but you expected {paper.expected_total}.")

        key = {}
        if paper.answer_pdf_path:
            try:
                key = parse_answer_key(paper.answer_pdf_path)
            except AnswerKeyError as e:
                warnings.append(f"Answer key not used: {e}")
        if key:
            extra = sorted(set(key) - {q["question_number"] for q in questions})
            if extra:
                warnings.append(f"The answer key has answers for questions that weren't extracted: "
                                f"{', '.join(map(str, extra))}.")

        inline_answers = 0
        for item in questions:
            n = item["question_number"]
            flags = list(item["flags"])
            entry = key.get(n)                          # from a separate answer PDF
            printed = item.get("answer")                # printed under the question in the paper itself
            if key and not entry:
                flags.append("no_answer_in_key")
            if entry:
                flags = [f for f in flags if f not in ("no_answer_found", "answer_unclear")]
                answer, answer_source = entry["answer"], "key_pdf"
                if printed and printed != answer:       # two sources disagree: never pick one silently
                    flags.append("answer_conflict")
            elif printed:
                answer, answer_source = printed, "inline"
                inline_answers += 1
            else:
                answer, answer_source = None, None
            explanation = (entry["explanation"] if entry and entry["explanation"] else None) or item.get("explanation")
            says = key_parse.explanation_says(explanation)
            if says and answer and says != answer:
                flags.append("explanation_mismatch")
            db.add(models.Question(
                paper_id=paper_id,
                question_number=n,
                text=item["text"],
                option_a=item["option_a"], option_b=item["option_b"],
                option_c=item["option_c"], option_d=item["option_d"],
                correct_answer=answer,
                explanation=explanation,
                source_image_path=item["source_image"],
                page_number=item.get("page_number"),
                ocr_flags=",".join(flags) or None,
                status=models.QStatus.NEEDS_REVIEW,
                source="pdf_text" if method == "text" else "pdf_ocr",
                extraction_method=method,
                answer_source=answer_source,
                explanation_status="unverified" if explanation else None,
                explanation_says=says,
            ))
        block = getattr(questions, "key_block", None)
        if block and not key and not inline_answers:            # never applied automatically: the admin previews it first
            found = key_parse.parse_key_text(block, paper.series)
            if found.answers:
                warnings.append(f"An answer key block was found at the end of the question PDF ({len(found.answers)} answers). "
                                "Open “Answer key” on this paper to preview it and apply it.")
        db.flush()
        if subject_ranges:
            apply_subject_ranges(db, paper_id, subject_ranges)
        subject_hints.suggest_for_paper(db, paper_id)            # only a hint beside the subject box; never applied by itself
        duplicates.scan(db, paper_id)                            # possible duplicates are listed for the admin; nothing is merged

        paper.status = "ready"
        paper.status_message = "\n".join(warnings) or None
        audit.log(db, None, "paper.import_done", "paper", paper_id, paper_id=paper_id,
                  detail={"questions": len(questions), "method": method, "layout": layout,
                          "answers_from_key": len(key), "answers_printed_in_paper": inline_answers,
                          "warnings": warnings})
        db.commit()
    except Exception as e:  # surfaced on the review page so the admin can see what went wrong
        db.rollback()
        paper = db.get(models.Paper, paper_id)
        paper.status = "failed"
        paper.status_message = f"{type(e).__name__}: {e}"
        audit.log(db, None, "paper.import_failed", "paper", paper_id, paper_id=paper_id,
                  detail={"error": paper.status_message})
        db.commit()
    finally:
        db.close()


def rerun_pages(paper_id: int, first_page: int, last_page: int, layout: str = "auto"):
    """Read the paper again and use the result ONLY for questions that start on pages first_page..last_page.

    Protection: a question that has been confirmed (verified or live), or edited by hand, is never touched. A question still waiting
    for review, with no edits, is replaced by the new reading (its old text is kept in its history). A number the first reading
    missed is added. The paper stays usable throughout: it goes back to "ready" whatever happens."""
    import shutil
    import tempfile

    db = SessionLocal()
    paper = db.get(models.Paper, paper_id)
    try:
        def progress(done, total):
            paper.pages_done, paper.pages_total = done, total
            db.commit()

        from app import sample_audit
        counts = {"replaced": 0, "added": 0, "protected": 0}
        images = images_dir_for(paper_id)
        os.makedirs(images, exist_ok=True)
        with tempfile.TemporaryDirectory() as tmp:
            questions, warnings, method = read_questions(paper.source_pdf_path, tmp, on_progress=progress, layout=layout)
            existing = {q.question_number: q for q in paper.questions if q.question_number is not None}
            edited = sample_audit.edited_ids(db, paper)
            for item in questions:
                page = item.get("page_number")
                if page is None or not first_page <= page <= last_page:
                    continue
                n = item["question_number"]
                old = existing.get(n)
                fields = dict(
                    text=item["text"], option_a=item["option_a"], option_b=item["option_b"], option_c=item["option_c"],
                    option_d=item["option_d"], page_number=page, ocr_flags=",".join(item["flags"]) or None,
                    source="pdf_text" if method == "text" else "pdf_ocr", extraction_method=method,
                )
                if old is not None and (old.status not in (models.QStatus.DRAFT, models.QStatus.NEEDS_REVIEW) or old.id in edited):
                    counts["protected"] += 1
                    continue
                if old is None:
                    old = models.Question(paper_id=paper_id, question_number=n, status=models.QStatus.NEEDS_REVIEW,
                                          correct_answer=item.get("answer"), answer_source="inline" if item.get("answer") else None,
                                          explanation=item.get("explanation"),
                                          explanation_status="unverified" if item.get("explanation") else None, **fields)
                    db.add(old)
                    counts["added"] += 1
                else:
                    versions.snapshot(db, old, None, f"pages {first_page}-{last_page} read again")
                    for key, value in fields.items():
                        setattr(old, key, value)
                    if not old.correct_answer and item.get("answer"):
                        old.correct_answer, old.answer_source = item["answer"], "inline"
                    counts["replaced"] += 1
                if item.get("source_image") and os.path.exists(os.path.join(tmp, item["source_image"])):
                    shutil.copyfile(os.path.join(tmp, item["source_image"]), os.path.join(images, item["source_image"]))
                    old.source_image_path = item["source_image"]
        db.flush()
        subject_hints.suggest_for_paper(db, paper_id)
        duplicates.scan(db, paper_id)
        paper.status = "ready"
        paper.status_message = (f"Pages {first_page}–{last_page} were read again: {counts['replaced']} question(s) replaced, "
                                f"{counts['added']} added, {counts['protected']} left alone because they were already confirmed or edited.")
        audit.log(db, None, "paper.rerun_pages", "paper", paper_id, paper_id=paper_id,
                  detail={"first": first_page, "last": last_page, "method": method, **counts})
        db.commit()
    except Exception as e:
        db.rollback()
        paper = db.get(models.Paper, paper_id)
        paper.status = "ready"                                  # the paper itself is fine; only the re-reading failed
        paper.status_message = f"Re-reading pages {first_page}–{last_page} failed ({type(e).__name__}: {e}). Nothing was changed."
        audit.log(db, None, "paper.rerun_failed", "paper", paper_id, paper_id=paper_id, detail={"error": str(e)})
        db.commit()
    finally:
        db.close()


def mark_interrupted_papers():
    """Papers still 'processing' at startup were cut off by a restart; nothing is working on them any more."""
    db = SessionLocal()
    try:
        stuck = db.query(models.Paper).filter(models.Paper.status == "processing").all()
        for paper in stuck:
            paper.status = "failed"
            paper.status_message = "Reading was interrupted (the app restarted). Archive this paper and upload it again."
        db.commit()
    finally:
        db.close()
