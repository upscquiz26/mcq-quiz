"""Stage 1 (foundation): schema upgrade, source fields, audit log, version history, quarantine, archive, backups."""
import json
import os
import sqlite3

import pytest
from sqlalchemy import create_engine, inspect, text

from app import audit, backup, database, models, versions
from app.models import QStatus
from conftest import blank_pdf_bytes, question_form


def actions(db):
    db.rollback()
    return [a for (a,) in db.query(models.AuditLog.action).all()]


def get_q(db, paper, number):
    db.rollback()
    return db.query(models.Question).filter_by(paper_id=paper.id, question_number=number).one()


@pytest.fixture
def needs_tesseract():
    """A blank (image-only) PDF is read by OCR, so uploading one needs Tesseract on this machine."""
    from app import ocr_extract
    try:
        ocr_extract.tesseract_cmd()
    except ocr_extract.OcrUnavailable:
        pytest.skip("Tesseract isn't installed")


def pdf_files(pdf):
    return {"pdf_file": ("paper.pdf", pdf, "application/pdf")}


# --------------------------------------------------------------------------- schema

def test_ensure_columns_upgrades_an_old_database(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with engine.begin() as c:
        c.execute(text("CREATE TABLE papers (id INTEGER PRIMARY KEY, title VARCHAR NOT NULL)"))
        c.execute(text("CREATE TABLE questions (id INTEGER PRIMARY KEY, paper_id INTEGER, text TEXT, needs_review BOOLEAN)"))
        c.execute(text("CREATE TABLE subjects (id INTEGER PRIMARY KEY, name VARCHAR)"))
        c.execute(text("INSERT INTO questions (paper_id, text, needs_review) VALUES (1, 'unconfirmed', 1), (1, 'confirmed', 0)"))
    monkeypatch.setattr(database, "engine", engine)

    added = database.ensure_columns()

    columns = {c["name"] for c in inspect(engine).get_columns("questions")}
    assert {"status", "quarantine_reason", "norm_hash", "reviewed_by"} <= columns
    assert ("papers", "source_type") in added and ("subjects", "is_active") in added
    with engine.connect() as c:
        rows = c.execute(text("SELECT text, status FROM questions ORDER BY id")).fetchall()
    assert [tuple(r) for r in rows] == [("unconfirmed", "needs_review"), ("confirmed", "verified")]
    assert database.ensure_columns() == []      # running it again changes nothing


def test_fixed_subject_list_includes_csat_and_other(db):
    names = {s.name for s in db.query(models.Subject).all()}
    assert {"Polity", "History", "Geography", "Economy", "Environment", "Science & Tech",
            "Current Affairs", "CSAT", "Other"} <= names


# --------------------------------------------------------------------------- existing behaviour still works

def test_logged_out_visitors_are_sent_to_login(anon):
    for path in ("/", "/upload", "/admin/users", "/quarantine", "/admin/audit", "/admin/backups"):
        r = anon.get(path)
        assert r.status_code == 303 and r.headers["location"].startswith("/login"), path


def test_account_request_and_approval_flow(anon, admin, db):
    form = {"username": "carol", "password": "carolpass1", "confirm": "carolpass1"}
    assert anon.post("/signup", data=form).status_code == 303
    assert anon.post("/signup", data=form).status_code == 400          # requested once only
    r = anon.post("/login", data={"username": "carol", "password": "carolpass1", "next": "/"})
    assert r.status_code == 403 and "awaiting admin approval" in r.text

    db.rollback()
    carol = db.query(models.User).filter_by(username="carol").one()
    assert admin.post(f"/admin/users/{carol.id}/approve").status_code == 303
    assert anon.post("/login", data={"username": "carol", "password": "carolpass1", "next": "/"}).status_code == 303
    assert {"user.signup_request", "user.approved", "user.login"} <= set(actions(db))


@pytest.mark.parametrize("method,path", [
    ("get", "/upload"), ("get", "/quarantine"), ("get", "/admin/audit"), ("get", "/admin/backups"),
    ("get", "/admin/users"), ("post", "/admin/backups/create"), ("post", "/papers/1/archive"),
])
def test_non_admins_are_blocked_from_admin_pages(make_user, method, path):
    student = make_user()
    assert getattr(student, method)(path).status_code == 403


# --------------------------------------------------------------------------- upload: source fields, backup, duplicates

def test_upload_records_source_fields_backs_up_and_audits(admin, db, needs_tesseract):
    r = admin.post("/upload", data={
        "title": "Stage1 upload paper", "exam_type": "full_length", "source_type": "official_pyq",
        "source_name": "UPSC", "test_name": "Prelims", "test_number": "1", "series": "a",
        "expected_total": "100", "marks_per_question": "2", "negative_fraction": "1/3",
        "key_source": "UPSC key", "key_version": "final",
    }, files=pdf_files(blank_pdf_bytes(pages=1)))
    assert r.status_code == 303

    db.rollback()
    paper = db.query(models.Paper).filter_by(title="Stage1 upload paper").one()
    assert paper.status == "ready"                       # the blank page was read without error
    assert paper.source_type == "official_pyq" and paper.source_name == "UPSC"
    assert paper.series == "A" and paper.expected_total == 100 and paper.marks_per_question == 2
    assert abs(paper.negative_fraction - 0.3333) < 1e-4
    assert paper.key_source == "UPSC key" and paper.key_version == "final"
    assert len(paper.file_hash) == 64
    assert "No questions were found" in paper.status_message   # nothing was silently skipped

    assert any(b["kind"] == "auto" for b in backup.list_backups())
    upload_entry = db.query(models.AuditLog).filter_by(action="paper.upload", paper_id=paper.id).one()
    assert "paper.pdf" in upload_entry.detail_json and "backup" in upload_entry.detail_json
    assert "paper.import_done" in actions(db)


def test_duplicate_file_and_duplicate_test_are_blocked_unless_allowed(admin, needs_tesseract):
    data = {"title": "Dup A", "exam_type": "full_length", "source_name": "InstituteX",
            "test_name": "Series", "test_number": "7"}
    first = blank_pdf_bytes(pages=3)
    assert admin.post("/upload", data=data, files=pdf_files(first)).status_code == 303

    r = admin.post("/upload", data={**data, "title": "Dup B", "test_number": "8"}, files=pdf_files(first))
    assert r.status_code == 400 and "already uploaded" in r.text        # same file, different test

    r = admin.post("/upload", data={**data, "title": "Dup C"}, files=pdf_files(blank_pdf_bytes(pages=4)))
    assert r.status_code == 400 and "already exists" in r.text          # different file, same test

    r = admin.post("/upload", data={**data, "title": "Dup D", "allow_duplicate": "true"}, files=pdf_files(first))
    assert r.status_code == 303                                          # explicit override


@pytest.mark.parametrize("field,value,message", [
    ("negative_fraction", "5/0", "fraction"),
    ("negative_fraction", "2", "fraction"),
    ("marks_per_question", "-1", "Marks per question"),
    ("expected_total", "0", "Expected number"),
    ("source_type", "made_up", "Official PYQ or Coaching test"),
    ("year", "abc", "Year"),
])
def test_upload_rejects_bad_fields_and_keeps_the_form(admin, field, value, message):
    data = {"title": "Keep my title", "exam_type": "full_length", field: value}
    r = admin.post("/upload", data=data, files=pdf_files(blank_pdf_bytes(pages=1)))
    assert r.status_code == 400 and message in r.text
    assert "Keep my title" in r.text                                     # form is echoed back


def test_upload_rejects_a_file_that_is_not_a_pdf(admin):
    r = admin.post("/upload", data={"title": "Not a pdf", "exam_type": "full_length"},
                   files={"pdf_file": ("x.pdf", b"just some text", "application/pdf")})
    assert r.status_code == 400 and "not a PDF" in r.text


def test_upload_page_offers_marking_presets(admin):
    r = admin.get("/upload")
    assert r.status_code == 200
    for expected in ("UPSC GS Paper I", "UPSC CSAT", "Official PYQ", "Coaching test", "negative_fraction"):
        assert expected in r.text


# --------------------------------------------------------------------------- audit log

def test_audit_log_scrubs_secrets(db):
    audit.log(db, None, "test.scrub", detail={"password": "hunter2", "nested": {"api_key": "sk-123"}, "ok": "visible"})
    db.commit()
    row = db.query(models.AuditLog).filter_by(action="test.scrub").one()
    assert "hunter2" not in row.detail_json and "sk-123" not in row.detail_json
    assert "visible" in row.detail_json and row.detail_json.count("[hidden]") == 2


def test_audit_page_lists_and_filters_entries(admin, db, make_paper):
    paper = make_paper("Audit page paper", n=2)
    q = get_q(db, paper, 1)
    admin.post(f"/review/{paper.id}/question/{q.id}", data=question_form(q, text="changed for audit"))

    r = admin.get("/admin/audit?action=question.edit")
    assert r.status_code == 200 and "question.edit" in r.text and "user.login" not in r.text
    assert admin.get(f"/admin/audit?paper_id={paper.id}").status_code == 200


# --------------------------------------------------------------------------- versions

def test_every_edit_is_versioned_and_can_be_restored(admin, db, make_paper):
    paper = make_paper("Versions paper", n=2)
    q = get_q(db, paper, 1)
    url = f"/review/{paper.id}/question/{q.id}"

    r = admin.post(url, data=question_form(q, text="Edited once?", correct_answer="B"))
    assert r.status_code == 303 and r.headers["location"] == f"/review/{paper.id}#q2"
    q = get_q(db, paper, 1)
    assert q.text == "Edited once?" and q.answer_source == "manual"
    assert q.status == QStatus.VERIFIED and q.reviewed_at is not None
    first = versions.history(db, q.id)
    assert len(first) == 1 and json.loads(first[0].snapshot_json)["text"] == "Question 1?"   # as imported

    admin.post(url, data=question_form(q, text="Edited twice?"))
    assert len(versions.history(db, q.id)) == 2

    admin.post(url, data=question_form(get_q(db, paper, 1)))              # nothing changed: no new version
    assert len(versions.history(db, q.id)) == 2

    history = admin.get(f"{url}/history")
    assert history.status_code == 200 and "Restore this version" in history.text

    original = versions.history(db, q.id)[-1]
    assert admin.post(f"{url}/restore/{original.id}").status_code == 303
    q = get_q(db, paper, 1)
    assert q.text == "Question 1?" and q.correct_answer == "A"
    assert q.status == QStatus.NEEDS_REVIEW                                # restored content must be re-confirmed
    assert len(versions.history(db, q.id)) == 3                            # the pre-restore state was kept too
    assert {"question.edit", "question.confirm", "question.version_restore"} <= set(actions(db))


def test_editing_a_live_question_sends_it_back_to_review(admin, db, make_paper):
    paper = make_paper("Live edit paper", n=2)
    q = get_q(db, paper, 1)
    q.status = QStatus.LIVE
    db.commit()
    url = f"/review/{paper.id}/question/{q.id}"

    admin.post(url, data=question_form(q))                                 # unchanged save keeps it live
    assert get_q(db, paper, 1).status == QStatus.LIVE
    admin.post(url, data=question_form(q, text="Now different?"))
    q = get_q(db, paper, 1)
    assert q.status == QStatus.NEEDS_REVIEW and q.reviewed_at is None
    assert "question.demote" in actions(db)


def test_bulk_subject_change_is_undoable(admin, db, make_paper):
    paper = make_paper("Bulk subject paper", n=3)
    r = admin.post(f"/review/{paper.id}/subjects", data={"subject_ranges": "1-2 History"})
    assert r.status_code == 303
    history_id = db.query(models.Subject).filter_by(name="History").one().id
    q1 = get_q(db, paper, 1)
    assert q1.subject_id == history_id and get_q(db, paper, 3).subject_id is None
    assert len(versions.history(db, q1.id)) == 1

    admin.post(f"/review/{paper.id}/question/{q1.id}/restore/{versions.history(db, q1.id)[0].id}")
    assert get_q(db, paper, 1).subject_id is None
    assert "subjects.bulk_set" in actions(db)


def test_confirm_clean_only_confirms_questions_without_warnings_or_gaps(admin, db, make_paper):
    paper = make_paper("Confirm clean paper", n=4)
    flagged, unanswered = get_q(db, paper, 2), get_q(db, paper, 3)   # (get_q rolls back, so fetch both first)
    flagged.ocr_flags = "odd_characters"
    unanswered.correct_answer = None
    db.commit()

    assert admin.post(f"/review/{paper.id}/confirm-clean").status_code == 303
    assert [get_q(db, paper, n).status for n in (1, 2, 3, 4)] == [
        QStatus.VERIFIED, QStatus.NEEDS_REVIEW, QStatus.NEEDS_REVIEW, QStatus.VERIFIED]
    entry = db.query(models.AuditLog).filter_by(action="question.confirm_bulk", paper_id=paper.id).one()
    assert json.loads(entry.detail_json)["count"] == 2


# --------------------------------------------------------------------------- quarantine

def test_quarantine_hides_a_question_without_deleting_it_and_can_restore(admin, db, make_paper):
    paper = make_paper("Quarantine paper", n=3)
    q = get_q(db, paper, 2)
    url = f"/review/{paper.id}/question/{q.id}"

    admin.post(f"{url}/quarantine", data={"reason": "   "})               # a reason is required
    assert get_q(db, paper, 2).status == QStatus.NEEDS_REVIEW

    assert admin.post(f"{url}/quarantine", data={"reason": "Garbled option C"}).status_code == 303
    q = get_q(db, paper, 2)
    assert q.status == QStatus.QUARANTINED and q.quarantine_reason == "Garbled option C"
    assert db.query(models.Question).filter_by(paper_id=paper.id).count() == 3      # nothing deleted

    page = admin.get(f"/review/{paper.id}")
    assert "1 in quarantine" in page.text and "Q2" not in page.text.split('id="q3"')[0].split('id="q1"')[1]
    admin.post(f"/review/{paper.id}/confirm-clean")
    assert get_q(db, paper, 2).status == QStatus.QUARANTINED                        # bulk confirm skips it
    assert admin.post(url, data=question_form(q)).status_code == 400                # can't be edited while quarantined

    listing = admin.get("/quarantine")
    assert listing.status_code == 200 and "Garbled option C" in listing.text
    assert "Garbled option C" not in admin.get(f"/quarantine?paper_id={paper.id + 999}").text

    assert admin.post(f"/quarantine/{q.id}/restore").status_code == 303
    q = get_q(db, paper, 2)
    assert q.status == QStatus.NEEDS_REVIEW and q.quarantine_reason is None          # must be confirmed again
    assert {"question.quarantine", "question.restore"} <= set(actions(db))


# --------------------------------------------------------------------------- archive (papers are never deleted)

def test_archiving_keeps_everything_and_can_be_undone(admin, make_user, db, make_paper, tmp_path):
    source = tmp_path / "kept.pdf"
    source.write_bytes(b"%PDF-1.4 kept")
    paper = make_paper("Archive me paper", n=2, source_pdf_path=str(source))

    assert admin.post(f"/papers/{paper.id}/archive").status_code == 303
    db.rollback()
    paper = db.get(models.Paper, paper.id)
    assert paper.archived_at is not None
    assert source.exists()                                                           # the file is kept
    assert db.query(models.Question).filter_by(paper_id=paper.id).count() == 2      # so are the questions

    assert "Archive me paper" in admin.get("/").text                                 # admin still finds it
    assert "Archive me paper" not in make_user().get("/").text                       # students don't see it

    assert admin.post(f"/papers/{paper.id}/unarchive").status_code == 303
    db.rollback()
    assert db.get(models.Paper, paper.id).archived_at is None
    assert {"paper.archive", "paper.unarchive"} <= set(actions(db))


def test_a_paper_that_is_still_being_read_cannot_be_archived(admin, db, make_paper):
    paper = make_paper("Still reading paper", n=1)
    paper.status = "processing"
    db.commit()
    r = admin.post(f"/papers/{paper.id}/archive")
    assert r.status_code == 303 and r.headers["location"] == f"/review/{paper.id}"
    db.rollback()
    assert db.get(models.Paper, paper.id).archived_at is None


def test_an_unused_paper_can_be_deleted(admin, db, make_paper, tmp_path):
    source = tmp_path / "gone.pdf"
    source.write_bytes(b"%PDF-1.4 gone")
    paper = make_paper("Delete me paper", n=2, source_pdf_path=str(source))
    pid, qids = paper.id, [q.id for q in paper.questions]
    page = admin.get(f"/review/{pid}")
    assert "Delete this paper" in page.text and "Add one question by hand" in page.text and "Delete question" in page.text
    assert admin.post(f"/papers/{pid}/delete").status_code == 303
    db.rollback()
    assert db.get(models.Paper, pid) is None
    assert db.query(models.Question).filter(models.Question.id.in_(qids)).count() == 0
    assert not source.exists()
    assert {"paper.delete"} <= set(actions(db))


def test_a_paper_with_student_attempts_cannot_be_deleted(admin, db, make_paper, make_user):
    paper = make_paper("Sat paper", n=1)
    make_user("sitter")
    student = db.query(models.User).filter_by(username="sitter").one()
    db.add(models.Attempt(user_id=student.id, paper_id=paper.id, kind=models.AttemptKind.FULL,
                          status=models.AttemptStatus.SUBMITTED))
    db.commit()
    r = admin.post(f"/papers/{paper.id}/delete")
    assert r.status_code == 303
    db.rollback()
    assert db.get(models.Paper, paper.id) is not None
    assert db.query(models.Question).filter_by(paper_id=paper.id).count() == 1


def test_a_question_can_be_added_to_a_paper_and_an_unused_one_deleted(admin, db, make_paper):
    paper = make_paper("Editable paper", n=2)
    r = admin.post(f"/review/{paper.id}/questions/add", data={
        "question_number": "3", "text": "What is 2 + 2?",
        "option_a": "3", "option_b": "4", "option_c": "5", "option_d": "6",
        "correct_answer": "B", "explanation": "Basic arithmetic.",
    })
    assert r.status_code == 303
    db.rollback()
    q = db.query(models.Question).filter_by(paper_id=paper.id, question_number=3).one()
    assert (q.text, q.option_b, q.correct_answer, q.status, q.source) == (
        "What is 2 + 2?", "4", "B", models.QStatus.NEEDS_REVIEW, "manual")
    assert q.explanation == "Basic arithmetic."
    assert admin.post(f"/review/{paper.id}/questions/add", data={
        "question_number": "3", "text": "Duplicate number",
        "option_a": "a", "option_b": "b", "option_c": "c", "option_d": "d",
    }).status_code == 303
    db.rollback()
    assert db.query(models.Question).filter_by(paper_id=paper.id).count() == 3

    assert admin.post(f"/review/{paper.id}/question/{q.id}/delete").status_code == 303
    db.rollback()
    assert db.query(models.Question).filter_by(paper_id=paper.id, question_number=3).first() is None
    assert db.query(models.Question).filter_by(paper_id=paper.id).count() == 2
    assert {"question.add", "question.delete"} <= set(actions(db))


def test_a_question_with_student_answers_cannot_be_deleted(admin, db, make_paper, make_user):
    paper = make_paper("Answered paper", n=1)
    q = db.query(models.Question).filter_by(paper_id=paper.id).one()
    make_user("answerer")
    student = db.query(models.User).filter_by(username="answerer").one()
    attempt = models.Attempt(user_id=student.id, kind=models.AttemptKind.TOPIC, status=models.AttemptStatus.SUBMITTED)
    db.add(attempt)
    db.flush()
    db.add(models.Response(attempt_id=attempt.id, question_id=q.id, position=1, selected_answer="A"))
    db.commit()
    r = admin.post(f"/review/{paper.id}/question/{q.id}/delete")
    assert r.status_code == 303
    db.rollback()
    assert db.get(models.Question, q.id) is not None


# --------------------------------------------------------------------------- backups

def test_manual_backup_is_a_real_copy_and_can_be_downloaded(admin, db):
    assert admin.post("/admin/backups/create").status_code == 303
    manual = [b for b in backup.list_backups() if b["kind"] == "manual"]
    assert manual
    name = manual[0]["name"]

    page = admin.get("/admin/backups")
    assert page.status_code == 200 and name in page.text

    download = admin.get(f"/admin/backups/{name}")
    assert download.status_code == 200 and download.content.startswith(b"SQLite format 3\x00")
    copy = sqlite3.connect(backup.backup_path(name))
    assert copy.execute("SELECT COUNT(*) FROM users").fetchone()[0] >= 1
    copy.close()
    assert {"backup.create", "backup.download"} <= set(actions(db))


def test_backup_downloads_cannot_reach_other_files(admin):
    for name in ("upsc_pyq.db", "..%2Fupsc_pyq.db", "auto_20200101_000000.db", "..\\..\\secret.db"):
        assert admin.get(f"/admin/backups/{name}").status_code == 404
    assert backup.backup_path("../upsc_pyq.db") is None


def test_only_the_newest_automatic_backups_are_kept(monkeypatch):
    monkeypatch.setattr(backup, "AUTO_KEEP", 3)
    manual = backup.create_backup("manual")
    for _ in range(6):
        backup.create_backup("auto")
    kinds = [b["kind"] for b in backup.list_backups()]
    assert kinds.count("auto") == 3
    assert manual in [b["name"] for b in backup.list_backups()]                     # manual backups are never pruned
    assert os.path.isfile(backup.backup_path(manual))
