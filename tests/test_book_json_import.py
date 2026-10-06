import json
import re

from app import models
from app.practice import catalogue, pool
from conftest import make_student_client


def chapter(number=1, name="Mechanics", questions=None):
    return json.dumps({
        "subject": "Physics",
        "chapter_no": number,
        "chapter": name,
        "question_count": len(questions or []),
        "questions": questions or [],
    })


def item(number, answer="e"):
    return {
        "id": f"physics-{number}", "number": number,
        "question": f"Physics question {number}: which answer is correct?",
        "options": {"a": "Choice A", "b": "Choice B", "c": "Choice C", "d": "Choice D", "e": "Choice E"},
        "answer": answer, "explanation": "The fifth option is correct.",
        "source": ["Physics book, page 12"], "page": "G-12", "needs_review": [],
    }


def test_book_import_validates_chapter_json_and_reports_missing_answers(admin):
    response = admin.post("/admin/books/import/json/validate", data={"title": "Physics Book", "subject": "Physics"},
                          files=[("files", ("chapter1.json", chapter(1, "Mechanics", [item(1), item(2, answer=None)]), "application/json"))])
    assert response.status_code == 200
    assert "Validation passed" in response.text
    assert "No answer is supplied" in response.text
    assert "2 valid questions" in response.text


def test_book_import_creates_one_draft_collection_with_chapters_and_five_options(admin, db):
    response = admin.post("/admin/books/import/json/validate", data={"title": "Physics Book", "subject": "Physics"},
                          files=[
                              ("files", ("chapter2.json", chapter(2, "Optics", [item(1)]), "application/json")),
                              ("files", ("chapter1.json", chapter(1, "Mechanics", [item(1), item(2, answer=None)]), "application/json")),
                          ])
    token = re.search(r'name="token" value="([0-9a-f]{32})"', response.text).group(1)
    result = admin.post("/admin/books/import/json/apply", data={"token": token})
    assert result.status_code == 303
    paper_id = int(result.headers["location"].rsplit("/", 1)[1])
    paper = db.get(models.Paper, paper_id)
    assert paper.source_type == models.SourceType.BOOK and paper.publish_status == "draft"
    assert paper.expected_total == 3
    questions = db.query(models.Question).filter_by(paper_id=paper_id).order_by(models.Question.question_number).all()
    assert [(q.question_number, q.topic.name, q.correct_answer) for q in questions] == [
        (1, "Mechanics", "E"), (2, "Mechanics", None), (3, "Optics", "E")]
    assert questions[0].option_e == "Choice E" and questions[0].status == models.QStatus.NEEDS_REVIEW
    reference = json.loads(questions[0].source_ref)
    assert reference["chapter_question_number"] == 1 and reference["page"] == "G-12"
    assert "no_answer_found" in (questions[1].ocr_flags or "")


def test_published_book_questions_are_practice_only_and_e_is_rendered(admin, db):
    validated = admin.post("/admin/books/import/json/validate", data={"title": "Physics Practice Book", "subject": "Physics"},
                           files=[("files", ("chapter.json", chapter(1, "Mechanics", [item(1)]), "application/json"))])
    token = re.search(r'name="token" value="([0-9a-f]{32})"', validated.text).group(1)
    applied = admin.post("/admin/books/import/json/apply", data={"token": token})
    paper_id = int(applied.headers["location"].rsplit("/", 1)[1])
    question = db.query(models.Question).filter_by(paper_id=paper_id).one()
    question.status = models.QStatus.LIVE
    db.get(models.Paper, paper_id).publish_status = "published"
    db.commit()

    assert paper_id not in {entry["paper"].id for entry in catalogue.full_tests(db, 1)}
    eligible_count = pool.live_questions(db).filter(models.Question.id == question.id).count()
    assert eligible_count == 1, {
        "status": question.status, "answer": question.correct_answer, "valid_letters": pool.ANSWER_LETTERS,
        "has_image": question.has_image, "snapshot": question.source_image_path,
        "paper_status": question.paper.status, "publish_status": question.paper.publish_status,
        "archived_at": question.paper.archived_at,
    }
    student = make_student_client(db, "bookpractice")
    subject = db.query(models.Subject).filter_by(name="Physics").one()
    topic = db.query(models.Topic).filter_by(subject_id=subject.id, name="Mechanics").one()
    response = student.post("/practice/start", data={"subject_id": str(subject.id), "topic_id": str(topic.id), "count": "1"})
    attempt_id = int(response.headers["location"].split("/")[2])
    page = student.get(f"/attempts/{attempt_id}/q/1")
    assert 'value="E"' in page.text and "Mechanics · printed Q1" in page.text
