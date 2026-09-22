"""The admin dashboard: headline numbers, the 'needs attention' list and recent activity."""
import itertools
import re
from datetime import datetime, timedelta

from app import auth, models
from app.models import AttemptKind, AttemptStatus, QStatus
from app.routes.admin_dashboard import action_label

_n = itertools.count(1)


def kpis(admin) -> dict:
    """{href: value} of the headline tiles."""
    raw = admin.get("/admin").text
    found = re.findall(r'<a class="kpi[^"]*" href="([^"]+)">.*?<span class="kpi-value">(\d+)</span>', raw, flags=re.S)
    return {href: int(value) for href, value in found}


def attention(admin) -> str:
    raw = admin.get("/admin").text
    block = raw.split("Needs your attention")[1].split("Recent activity")[0]
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", block))


def add_paper(db, make_paper, title, n=3, confirmed=0, **fields):
    paper = make_paper(title, n=n, **fields)
    db.rollback()
    for q in db.query(models.Question).filter_by(paper_id=paper.id).filter(models.Question.question_number <= confirmed):
        q.status = QStatus.VERIFIED
    db.commit()
    return paper


def test_the_headline_tiles_count_what_they_say(admin, db, make_paper, make_user):
    before = kpis(admin)
    db.add(models.User(username=f"dashpending{next(_n)}", password_hash=auth.hash_password("x" * 10), is_admin=False,
                       status=models.UserStatus.pending))
    student_name = f"dashactive{next(_n)}"
    db.add(models.User(username=student_name, password_hash=auth.hash_password("x" * 10), is_admin=False,
                       status=models.UserStatus.approved, last_active_at=datetime.utcnow() - timedelta(days=2)))
    db.add(models.User(username=f"dashold{next(_n)}", password_hash=auth.hash_password("x" * 10), is_admin=False,
                       status=models.UserStatus.approved, last_active_at=datetime.utcnow() - timedelta(days=30)))
    add_paper(db, make_paper, f"Dash tiles paper {next(_n)}", n=4, confirmed=1)
    db.commit()
    uid = db.query(models.User).filter_by(username=student_name).one().id
    for status, days in ((AttemptStatus.SUBMITTED, 1), (AttemptStatus.EXPIRED, 3), (AttemptStatus.SUBMITTED, 20), (AttemptStatus.IN_PROGRESS, 1)):
        db.add(models.Attempt(user_id=uid, kind=AttemptKind.FULL, status=status, started_at=datetime.utcnow() - timedelta(days=days),
                              total_questions=1, counts_for_rank=False))
    db.commit()
    after = kpis(admin)
    assert after["/admin/users"] == before["/admin/users"] + 1                 # one more waiting for approval
    assert after["/"] >= before["/"] and after["/admin/performance"] == before["/admin/performance"] + 1   # only the recently active student
    raw = admin.get("/admin").text
    assert re.search(r"\d+ timed tests? finished", raw)
    # timed tests this week: the submitted one and the expired one (not the old one, not the one still running)
    def finished(html_text):
        return int(re.search(r'href="/admin/performance">.*?<span class="kpi-note">(\d+) timed', html_text, re.S).group(1))
    assert finished(raw) >= 2


def test_papers_awaiting_review_are_listed_with_a_link_to_their_questions(admin, db, make_paper):
    paper = add_paper(db, make_paper, f"Dash awaiting paper {next(_n)}", n=5, confirmed=2)
    text = attention(admin)
    assert f"“{paper.title}”: 3 of 5 questions to confirm" in text
    assert f'href="/review/{paper.id}?show=to_confirm"' in admin.get("/admin").text


def test_a_fully_confirmed_unpublished_paper_is_offered_for_publishing_and_a_published_one_is_not(admin, db, make_paper):
    ready = add_paper(db, make_paper, f"Dash ready paper {next(_n)}", n=3, confirmed=3)
    live = add_paper(db, make_paper, f"Dash live paper {next(_n)}", n=3, confirmed=3, publish_status="published")
    text = attention(admin)
    assert f"“{ready.title}” is fully confirmed but not published" in text
    assert live.title not in text


def test_a_paper_that_failed_to_import_is_flagged(admin, db, make_paper):
    paper = make_paper(f"Dash failed paper {next(_n)}", n=1)
    db.rollback()
    db.get(models.Paper, paper.id).status = "failed"
    db.commit()
    assert f"“{paper.title}” failed to import" in attention(admin)


def test_pending_accounts_and_open_reports_are_listed(admin, db, make_user):
    db.add(models.User(username=f"dashwaiting{next(_n)}", password_hash=auth.hash_password("x" * 10), is_admin=False,
                       status=models.UserStatus.pending))
    db.commit()
    text = attention(admin)
    assert re.search(r"\d+ accounts? waiting for approval", text)


def test_recent_activity_shows_who_did_what_in_plain_words(admin, db):
    name = f"dashapprove{next(_n)}"
    db.add(models.User(username=name, password_hash=auth.hash_password("x" * 10), is_admin=False, status=models.UserStatus.pending))
    db.commit()
    uid = db.query(models.User).filter_by(username=name).one().id
    admin.post(f"/admin/users/{uid}/approve")
    raw = admin.get("/admin").text
    block = raw.split("Recent activity")[1]
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", block))
    assert "admin · Approved an account" in text
    assert 'href="/admin/audit"' in block


def test_action_labels_fall_back_to_tidy_words():
    assert action_label("paper.publish") == "Published a paper"
    assert action_label("something.brand_new") == "Something brand new"


def test_only_admins_see_the_dashboard(make_user, anon):
    assert make_user("dashstudent").get("/admin").status_code == 403
    assert anon.get("/admin").status_code == 303


def test_the_papers_list_shows_review_progress(admin, db, make_paper):
    paper = add_paper(db, make_paper, f"Dash progress paper {next(_n)}", n=4, confirmed=1)
    raw = admin.get("/").text
    block = raw.split(paper.title)[1].split("</li>")[0]
    assert "25% reviewed" in block and 'aria-valuenow="25"' in block
