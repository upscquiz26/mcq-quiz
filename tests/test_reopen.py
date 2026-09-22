"""Sending confirmed questions (or a whole paper's) back to review, and getting to the ones awaiting review from the papers list."""
import itertools
import json

from app import models
from app.models import QStatus
from app.practice import pool

_n = itertools.count(1)


def confirmed_paper(db, make_paper, n=4, live=False):
    paper = make_paper(f"Reopen paper {next(_n)}", n=n, publish_status="published" if live else "draft")
    db.rollback()
    for q in db.query(models.Question).filter_by(paper_id=paper.id):
        q.status = QStatus.LIVE if live else QStatus.VERIFIED
        q.reviewed_by, q.reviewed_at = 1, models.datetime.utcnow()
    db.commit()
    return paper


def statuses(db, paper):
    db.rollback()
    return {q.question_number: q.status for q in db.query(models.Question).filter_by(paper_id=paper.id)}


def test_a_confirmed_question_can_be_sent_back_to_review(admin, db, make_paper):
    paper = confirmed_paper(db, make_paper)
    q = db.query(models.Question).filter_by(paper_id=paper.id, question_number=2).one()
    page = admin.get(f"/review/{paper.id}").text
    assert page.count(">Review again</button>") == 4 and f"/review/{paper.id}/question/{q.id}/reopen" in page

    r = admin.post(f"/review/{paper.id}/question/{q.id}/reopen")
    assert r.status_code == 303 and r.headers["location"] == f"/review/{paper.id}#q2"
    s = statuses(db, paper)
    assert s[2] == QStatus.NEEDS_REVIEW and s[1] == s[3] == s[4] == QStatus.VERIFIED
    db.rollback()
    q = db.get(models.Question, q.id)
    assert q.reviewed_by is None and q.reviewed_at is None and q.flags_acknowledged is False
    entry = db.query(models.AuditLog).filter_by(action="question.reopen", entity_id=q.id).one()
    assert json.loads(entry.detail_json) == {"number": 2, "was": "verified"} and entry.paper_id == paper.id
    page = admin.get(f"/review/{paper.id}").text
    assert page.count(">Review again</button>") == 3 and "Confirm question" in page         # 2 offers Confirm again instead


def test_reopening_a_live_question_removes_it_from_students_at_once(admin, db, make_paper):
    paper = confirmed_paper(db, make_paper, live=True)
    db.rollback()
    q = db.query(models.Question).filter_by(paper_id=paper.id, question_number=1).one()
    assert pool.live_questions(db).filter(models.Question.paper_id == paper.id).count() == 4
    r = admin.post(f"/review/{paper.id}/question/{q.id}/reopen")
    assert r.status_code == 303
    db.rollback()
    assert {x.question_number for x in pool.live_questions(db).filter(models.Question.paper_id == paper.id)} == {2, 3, 4}
    assert "Students can&#39;t see it until you confirm it again" in admin.get(f"/review/{paper.id}").text
    assert json.loads(db.query(models.AuditLog).filter_by(action="question.reopen", entity_id=q.id).one().detail_json)["was"] == "live"


def test_a_question_that_is_not_confirmed_has_nothing_to_send_back(admin, db, make_paper):
    paper = make_paper(f"Reopen unconfirmed {next(_n)}", n=2)
    db.rollback()
    q = db.query(models.Question).filter_by(paper_id=paper.id, question_number=1).one()
    assert "Review again" not in admin.get(f"/review/{paper.id}").text
    admin.post(f"/review/{paper.id}/question/{q.id}/reopen")
    assert "nothing to send back" in admin.get(f"/review/{paper.id}").text
    assert statuses(db, paper)[1] == QStatus.NEEDS_REVIEW
    assert not db.query(models.AuditLog).filter_by(action="question.reopen", entity_id=q.id).count()


def test_a_whole_paper_can_be_sent_back_for_a_second_pass(admin, db, make_paper):
    paper = confirmed_paper(db, make_paper, n=5, live=True)
    db.rollback()
    q4 = db.query(models.Question).filter_by(paper_id=paper.id, question_number=4).one()
    q4.status = QStatus.NEEDS_REVIEW                                                          # already waiting
    db.commit()
    page = admin.get(f"/review/{paper.id}").text
    assert "Review again: send all 4 confirmed back" in page
    r = admin.post(f"/review/{paper.id}/reopen-all")
    assert r.status_code == 303 and r.headers["location"] == f"/review/{paper.id}?show=to_confirm"
    assert set(statuses(db, paper).values()) == {QStatus.NEEDS_REVIEW}
    assert pool.live_questions(db).filter(models.Question.paper_id == paper.id).count() == 0
    entry = db.query(models.AuditLog).filter_by(action="question.reopen_bulk", paper_id=paper.id).one()
    assert json.loads(entry.detail_json) == {"count": 4, "live": 4, "numbers": [1, 2, 3, 5]}
    assert "Review again: send all" not in admin.get(f"/review/{paper.id}").text              # nothing left to send back
    admin.post(f"/review/{paper.id}/reopen-all")                                              # a second time: nothing to do
    assert "No confirmed questions to send back" in admin.get(f"/review/{paper.id}?show=to_confirm").text


def test_only_admins_can_send_questions_back(make_user, db, make_paper, anon):
    paper = confirmed_paper(db, make_paper, n=2)
    db.rollback()
    q = db.query(models.Question).filter_by(paper_id=paper.id, question_number=1).one()
    student = make_user("reopenstudent")
    for url in (f"/review/{paper.id}/question/{q.id}/reopen", f"/review/{paper.id}/reopen-all"):
        assert student.post(url).status_code == 403
        r = anon.post(url)
        assert r.status_code == 303 and r.headers["location"].startswith("/login")
    assert set(statuses(db, paper).values()) == {QStatus.VERIFIED}


def test_an_unknown_question_or_one_from_another_paper_is_404(admin, db, make_paper):
    a, b = confirmed_paper(db, make_paper, n=2), confirmed_paper(db, make_paper, n=2)
    db.rollback()
    qb = db.query(models.Question).filter_by(paper_id=b.id, question_number=1).one()
    assert admin.post(f"/review/{a.id}/question/{qb.id}/reopen").status_code == 404
    assert admin.post(f"/review/{a.id}/question/999999/reopen").status_code == 404
    assert admin.post("/review/999999/reopen-all").status_code == 404
    assert statuses(db, b)[1] == QStatus.VERIFIED


def test_the_papers_list_links_straight_to_the_questions_awaiting_review_and_the_confirmed_ones(admin, db, make_paper, make_user):
    paper = make_paper(f"Reopen list paper {next(_n)}", n=5)
    db.rollback()
    for q in db.query(models.Question).filter_by(paper_id=paper.id).filter(models.Question.question_number <= 2):
        q.status = QStatus.VERIFIED
    db.commit()
    home = admin.get("/").text
    assert f'href="/review/{paper.id}?show=to_confirm">3 awaiting review</a>' in home
    assert f'href="/review/{paper.id}?show=confirmed">2 confirmed</a>' in home
    assert f'>Review 3 awaiting</a>' in home and f'action="/review/{paper.id}/reopen-all"' in home and "Review again (2)" in home
    student_home = make_user("reopenlistviewer").get("/").text
    assert "Review again" not in student_home and "reopen-all" not in student_home


def test_the_review_page_can_show_only_confirmed_questions(admin, db, make_paper):
    paper = make_paper(f"Reopen filter paper {next(_n)}", n=4)
    db.rollback()
    for q in db.query(models.Question).filter_by(paper_id=paper.id).filter(models.Question.question_number <= 1):
        q.status = QStatus.VERIFIED
    db.commit()
    page = admin.get(f"/review/{paper.id}?show=confirmed").text
    assert 'id="q1"' in page and 'id="q2"' not in page
    assert "To confirm (3)" in page and "Confirmed (1)" in page
    to_confirm = admin.get(f"/review/{paper.id}?show=to_confirm").text
    assert 'id="q1"' not in to_confirm and 'id="q2"' in to_confirm
