"""
Slow end-to-end check with a real paper. Skipped when the PDFs aren't present.

    pytest -m slow

Uses PYQ/test 1: an image-only, two-column bilingual coaching paper (OCR path)
with a text answer PDF. It must always yield 150 questions and 150 answers.
"""
import os
import re

import pytest

from app import models

PAPER_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "PYQ", "test 1")
QUESTIONS_PDF = os.path.join(PAPER_DIR, "PCSPre-2026 Test-1.pdf")
ANSWERS_PDF = os.path.join(PAPER_DIR, "PCSPre-2026 Test-1 Ans & Explanations.pdf")

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not (os.path.exists(QUESTIONS_PDF) and os.path.exists(ANSWERS_PDF)),
                       reason="real test paper not found in PYQ/test 1"),
]


POLITY_PDF = os.path.join(os.path.dirname(PAPER_DIR), "UPPCS 2026 Polity Test Answer Key .pdf")

# The 75 answers printed in that PDF, in order; Q11 prints "Answer: (db)" (text over text), which is reported, not guessed.
POLITY_ANSWERS = "bcbcdbbabc?dbbdcdbdbbacadacccccbacddcddbcbcccddabbdbabdccaadcdbabdbcacbbacd"
POLITY_TABLE_QUESTIONS = {1, 3, 22, 24, 29, 37, 63, 69, 74}


@pytest.mark.skipif(not os.path.exists(POLITY_PDF), reason="UPPCS Polity test not found in PYQ")
def test_real_text_pdf_polity_test_reads_75_questions_with_printed_answers(admin, db):
    """A real two-column text PDF with a giant watermark and the answer under every question. No OCR is involved."""
    with open(POLITY_PDF, "rb") as f:
        r = admin.post("/upload", data={"title": "Real Polity Test", "exam_type": "sectional", "source_type": "coaching_test",
                                        "expected_total": "75", "subject_ranges": "1-75 Polity"},
                       files={"pdf_file": ("polity.pdf", f.read(), "application/pdf")})
    assert r.status_code == 303
    db.rollback()
    paper = db.query(models.Paper).filter_by(title="Real Polity Test").one()
    assert paper.status == "ready" and paper.status_message is None, paper.status_message
    questions = {q.question_number: q for q in db.query(models.Question).filter_by(paper_id=paper.id)}

    assert sorted(questions) == list(range(1, 76))
    assert all(q.source == "pdf_text" and q.extraction_method == "text" and q.status == models.QStatus.NEEDS_REVIEW
               for q in questions.values())
    assert "".join((questions[n].correct_answer or "?").lower() for n in range(1, 76)) == POLITY_ANSWERS
    assert all(q.answer_source == "inline" for q in questions.values() if q.correct_answer)
    assert sum(1 for q in questions.values() if q.explanation) >= 70
    assert sum(1 for q in questions.values() if q.source_image_path) == 75
    assert all(q.subject_id for q in questions.values())

    flagged = {n: set(q.ocr_flags.split(",")) for n, q in questions.items() if q.ocr_flags}
    assert flagged.pop(11) == {"answer_unclear"}
    assert set(flagged) == POLITY_TABLE_QUESTIONS and all(f == {"check_table"} for f in flagged.values())

    # Checked by hand against the printed pages.
    assert questions[2].option_c == "Its elections were held on the basis of universal adult franchise."
    assert questions[4].text.startswith("The Constituent Assembly of India, convened to draft the Constitution")
    assert "4. Demarcation of territories in North-East India" in questions[4].text.splitlines()
    assert (questions[4].option_a, questions[4].option_d) == ("1 only", "4")
    assert (questions[37].option_a, questions[37].option_d) == ("3 2 1 4", "4 3 2 1")
    assert questions[7].option_a.startswith("Both (A) and (R) are true and (R) is the correct explanation")   # A/R labels kept
    assert questions[72].correct_answer == "B" and questions[73].correct_answer == "A" and questions[75].correct_answer == "D"
    assert "UPPCS" not in " ".join(q.text for q in questions.values())                                        # the watermark is gone
    assert not any("t.me/uppcs1" in (q.option_d or "") for q in questions.values())                           # ...and the footer


def test_real_paper_reads_150_questions_with_answers(admin, db):
    with open(QUESTIONS_PDF, "rb") as q, open(ANSWERS_PDF, "rb") as a:
        r = admin.post("/upload", data={
            "title": "Real Test-1", "exam_type": "full_length", "source_type": "coaching_test",
            "source_name": "Dhyeya", "test_name": "PCS Pre 2026", "test_number": "1", "series": "A",
            "expected_total": "150", "marks_per_question": "1.3333", "negative_fraction": "1/3",
            "duration_minutes": "120",
            "subject_ranges": "1-30 History, 31-60 Geography",
        }, files={"pdf_file": ("q.pdf", q.read(), "application/pdf"),
                  "answer_file": ("a.pdf", a.read(), "application/pdf")})
    assert r.status_code == 303

    db.rollback()
    paper = db.query(models.Paper).filter_by(title="Real Test-1").one()
    assert paper.status == "ready", paper.status_message
    questions = db.query(models.Question).filter_by(paper_id=paper.id).all()

    assert len(questions) == 150
    assert sorted(q.question_number for q in questions) == list(range(1, 151))
    assert all(q.correct_answer in ("A", "B", "C", "D") for q in questions)
    assert all(q.status == models.QStatus.NEEDS_REVIEW and q.source == "pdf_ocr" for q in questions)
    assert sum(1 for q in questions if q.explanation) >= 140
    assert sum(1 for q in questions if q.source_image_path) == 150
    assert sum(1 for q in questions if q.ocr_flags) <= 20         # measured 14; a jump means a regression
    assert sum(1 for q in questions if q.subject_id) == 60

    # Spot-check questions I compared with the printed paper.
    by_number = {q.question_number: q for q in questions}
    assert by_number[26].option_d == "1 and 4"
    assert by_number[1].correct_answer == "A" and by_number[5].correct_answer == "A"
    assert re.search(r"Slave Coast", by_number[6].text)

    page = admin.get(f"/review/{paper.id}")
    assert page.status_code == 200 and "(expected 150)" in page.text
    assert admin.post(f"/review/{paper.id}/confirm-clean").status_code == 303

    # Publishing is refused while the flagged questions are still unconfirmed...
    admin.post(f"/papers/{paper.id}/publish")
    db.rollback()
    assert db.get(models.Paper, paper.id).publish_status == "draft"

    # ...so confirm each of them by hand (as a reviewer would after checking the snapshot), then publish.
    from conftest import question_form
    for q in db.query(models.Question).filter_by(paper_id=paper.id, status=models.QStatus.NEEDS_REVIEW).all():
        assert admin.post(f"/review/{paper.id}/question/{q.id}", data=question_form(q)).status_code == 303

    # ...and the sample audit has to be done: 10% of the unedited confirmed questions, each checked against the page.
    admin.post(f"/papers/{paper.id}/publish")
    db.rollback()
    assert db.get(models.Paper, paper.id).publish_status == "draft"
    assert admin.post(f"/review/{paper.id}/audit/start").status_code == 303
    db.rollback()
    picked = db.query(models.Question).filter_by(paper_id=paper.id, audit_pick=True).all()
    assert 5 <= len(picked) <= 16
    for q in picked:
        admin.post(f"/review/{paper.id}/audit/{q.id}/check", data={"verdict": "ok"})
    db.rollback()
    assert db.get(models.Paper, paper.id).audit_state == "passed"
    assert admin.post(f"/papers/{paper.id}/publish").status_code == 303

    from app.practice import pool
    db.rollback()
    assert pool.live_questions(db).filter(models.Question.paper_id == paper.id).count() == 150

    from conftest import make_student_client
    student = make_student_client(db, "realpapercheck")
    assert "Real Test-1" in student.get("/").text

    # A real practice session on the real paper: 5 questions from the History block (Q1-30), answered, finished.
    history = db.query(models.Subject).filter_by(name="History").one()
    r = student.post("/practice/start", data={"subject_id": str(history.id), "count": "5"})
    assert r.status_code == 303
    attempt_id = int(r.headers["location"].rsplit("/", 1)[1])
    db.rollback()
    attempt = db.get(models.Attempt, attempt_id)
    assert attempt.total_questions == 5
    assert all(db.get(models.Question, x.question_id).question_number <= 30 for x in attempt.responses)
    assert all(x.marks_if_correct == pytest.approx(1.3333) for x in attempt.responses)   # the paper's own scheme
    for position, resp in enumerate(attempt.responses, start=1):
        page = student.get(f"/attempts/{attempt_id}/q/{position}").text
        assert 'name="answer"' in page and "Explanation" not in page                      # nothing revealed yet
        right = db.get(models.Question, resp.question_id).correct_answer
        student.post(f"/attempts/{attempt_id}/q/{position}/answer", data={"answer": right, "confidence": "sure"})
        feedback = student.get(f"/attempts/{attempt_id}/q/{position}").text
        assert "Correct." in feedback and "Explanation" in feedback
    assert student.post(f"/attempts/{attempt_id}/finish").status_code == 303
    db.rollback()
    done = db.get(models.Attempt, attempt_id)
    assert (done.correct_count, done.wrong_count, done.skipped_count) == (5, 0, 0)
    assert done.score == pytest.approx(5 * 1.3333)

    # A real timed FULL-LENGTH test: 150 questions in paper order, 120 minutes, the paper's own scheme.
    from datetime import timedelta
    r = student.post("/tests/start", data={"mode": "full", "paper_id": str(paper.id)})
    assert r.status_code == 303
    test_id = int(r.headers["location"].rsplit("/", 1)[1])
    db.rollback()
    test = db.get(models.Attempt, test_id)
    assert test.total_questions == 150 and test.kind == "full" and test.counts_for_rank is True
    assert test.deadline_at - test.started_at == timedelta(minutes=120)
    assert [db.get(models.Question, x.question_id).question_number for x in test.responses] == list(range(1, 151))

    # Worked by hand: 4 right, 2 wrong, the other 144 skipped.
    #   4 x 1.3333 = 5.3332   2 x (1.3333 / 3) = 0.88887   ->  5.3332 - 0.88887 = 4.44433
    for x in test.responses[:6]:
        right = db.get(models.Question, x.question_id).correct_answer
        letter = right if x.position <= 4 else next(l for l in "ABCD" if l != right)
        saved = student.post(f"/attempts/{test_id}/q/{x.position}/save",
                             data={"answer": letter, "confidence": "sure"}, headers={"Accept": "application/json"})
        assert saved.json()["ok"] is True
    assert student.post(f"/attempts/{test_id}/finish").status_code == 303
    db.rollback()
    done = db.get(models.Attempt, test_id)
    assert (done.correct_count, done.wrong_count, done.skipped_count) == (4, 2, 144)
    assert done.score == pytest.approx(4.44433, abs=0.001) and done.max_marks == pytest.approx(150 * 1.3333, abs=0.01)

    # The results analysis on the real test. Both wrong answers were "sure" with no measurable time, so both are
    # suggested as misconceptions. The estimate uses the student's WHOLE finished record, which here is the earlier
    # practice session (5 right of 5) plus this test's 6 answers (4 right): 9 right of 11 = 81.8%, far above the 25%
    # break-even for a one-third penalty, so every one of the 144 skipped questions is "worth attempting":
    #   each = 9/11 x 1.3333 - 2/11 x (1.3333 / 3) = +1.0101   ->   144 x 1.0101 = +145.45
    from app.practice import grading
    assert sorted(x.mistake_reason.name for x in done.responses if x.is_correct is False) == ["conceptual_confusion"] * 2
    worth = grading.worth_attempting(db, done)
    assert worth["skipped"] == 144 and worth["worth"] == 144 and worth["break_even_percent"] == 25
    assert worth["your_accuracy_percent"] == 67                                    # this test alone: 4 of 6
    assert worth["expected_gain"] == pytest.approx(144 * (9 / 11 * 1.3333 - 2 / 11 * 1.3333 / 3), abs=0.5)
    result_page = student.get(f"/attempts/{test_id}/result").text
    for heading in ("Guessing report", "Why the wrong answers went wrong", "By subject", "Should you have attempted more?"):
        assert heading in result_page

    # Revision on the real data: the test's 2 wrong answers are on the schedule (due tomorrow), the 4 right ones are not.
    from datetime import date
    from app.practice import revision
    uid = db.query(models.User).filter_by(username="realpapercheck").one().id
    entries = revision.notebook(db, uid)
    assert len(entries) == 2 and all(e["state"] == "scheduled" and e["days_until"] == 1 for e in entries)
    assert revision.due_count(db, uid) == 0
    assert student.get("/revision").status_code == 200
    r = student.post("/revision/start", data={"mode": "all", "count": "10"})
    assert r.status_code == 303 and r.headers["location"].startswith("/attempts/")
    mistake = db.get(models.Attempt, int(r.headers["location"].rsplit("/", 1)[1]))
    db.rollback()
    mistake = db.get(models.Attempt, mistake.id)
    assert mistake.kind == "mistake" and mistake.total_questions == 2
    assert {x.question_id for x in mistake.responses} == {e["question"].id for e in entries}

    # A History section test: exactly Q1-30, at 72 seconds each.
    r = student.post("/tests/start", data={"mode": "section", "paper_id": str(paper.id), "subject_id": str(history.id)})
    section = db.get(models.Attempt, int(r.headers["location"].rsplit("/", 1)[1]))
    db.rollback()
    section = db.get(models.Attempt, section.id)
    assert section.total_questions == 30 and section.deadline_at - section.started_at == timedelta(seconds=30 * 72)

    home = student.get("/").text
    assert "Recent tests" in home and "Real Test-1" in home.split("Recent tests")[1].split("</section>")[0]   # their own history
    assert admin.post(f"/papers/{paper.id}/unpublish").status_code == 303
    after = student.get("/").text
    assert "Real Test-1" not in after.split("<h2>Papers</h2>")[1]           # no longer offered as a paper to practise...
    assert "Real Test-1" in after.split("Recent tests")[1].split("</section>")[0]      # ...but the student's own record stays
