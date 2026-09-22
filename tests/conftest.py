"""
Test setup. Everything runs against a throwaway data folder, so your real
database, PDFs and backups are never touched.

The environment must be set BEFORE the app is imported (the database engine is
created at import time), which is why it happens at the top of this file.
"""
import io
import os
import sys
import tempfile

_DATA_DIR = tempfile.mkdtemp(prefix="upsc_test_")
os.environ["UPSC_DATA_DIR"] = _DATA_DIR
os.environ["APP_USERNAME"] = "admin"
os.environ["APP_PASSWORD"] = "adminpass1"
os.environ["SECRET_KEY"] = "test-secret-key"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import auth, models
from app.database import SessionLocal
from app.main import app

ADMIN_PASSWORD = "adminpass1"


@pytest.fixture(scope="session")
def data_dir():
    return _DATA_DIR


@pytest.fixture()
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _client() -> TestClient:
    return TestClient(app, follow_redirects=False)


def _login(client: TestClient, username: str, password: str):
    r = client.post("/login", data={"username": username, "password": password, "next": "/"})
    assert r.status_code == 303, f"login failed for {username}: {r.status_code}"
    return client


@pytest.fixture()
def admin():
    return _login(_client(), "admin", ADMIN_PASSWORD)


@pytest.fixture()
def anon():
    return _client()


@pytest.fixture()
def make_user(db):
    """Creates an approved (non-admin) user and returns a logged-in client for them."""
    def factory(username="student1", password="studentpass1"):
        if not db.query(models.User).filter(models.User.username == username).first():
            db.add(models.User(username=username, password_hash=auth.hash_password(password),
                               is_admin=False, status=models.UserStatus.approved))
            db.commit()
        return _login(_client(), username, password)
    return factory


def make_student_client(db, username="student1", password="studentpass1") -> TestClient:
    """An approved student created directly in the database, returned already logged in."""
    if not db.query(models.User).filter(models.User.username == username).first():
        db.add(models.User(username=username, password_hash=auth.hash_password(password),
                           is_admin=False, status=models.UserStatus.approved))
        db.commit()
    return _login(_client(), username, password)


@pytest.fixture()
def make_paper(db):
    """Creates a ready paper with `n` questions (needs_review, A-D answers cycling) without any OCR."""
    def factory(title="Test paper", n=5, **paper_fields):
        paper = models.Paper(title=title, exam_type=models.ExamType.full_length, status="ready", **paper_fields)
        db.add(paper)
        db.flush()
        for i in range(1, n + 1):
            db.add(models.Question(
                paper_id=paper.id, question_number=i, text=f"Question {i}?",
                option_a="alpha", option_b="beta", option_c="gamma", option_d="delta",
                correct_answer="ABCD"[(i - 1) % 4], status=models.QStatus.NEEDS_REVIEW,
                source="pdf_ocr",
            ))
        db.commit()
        db.refresh(paper)
        return paper
    return factory


def pass_audit(db, paper):
    """Marks a paper's sample audit as passed, for tests about publishing that aren't about the audit itself."""
    db.rollback()
    row = db.get(models.Paper, paper.id)
    row.audit_state = "passed"
    db.commit()


def question_form(q, **overrides):
    """The form a browser would post for a question, with any field overridden."""
    data = {
        "text": q.text, "option_a": q.option_a, "option_b": q.option_b, "option_c": q.option_c,
        "option_d": q.option_d, "correct_answer": q.correct_answer or "A",
        "subject_id": str(q.subject_id or ""), "topic_name": "", "difficulty": q.difficulty or "",
    }
    data.update(overrides)
    return data


def blank_pdf_bytes(pages: int = 1, colour="white") -> bytes:
    """A valid PDF with blank page(s), for exercising the upload path without real OCR work."""
    buf = io.BytesIO()
    first = Image.new("RGB", (600, 800), colour)
    rest = [Image.new("RGB", (600, 800), colour) for _ in range(pages - 1)]
    first.save(buf, "PDF", save_all=bool(rest), append_images=rest)
    return buf.getvalue()
