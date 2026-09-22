"""
Student stage 9: report-a-problem (and the admin's queue), the daily target and the streak.
"""
import itertools
from datetime import date, datetime, time, timedelta, timezone

import pytest

from app import models
from app.practice import activity, reports
from test_practice_stage2 import answer, attempt_of, live_paper, questions_of, start, user_id
from test_revision_stage5 import page
from test_timed_stage3 import letter_for, save, start_full, timed_paper

_n = itertools.count(1)


def new_paper(db, make_paper, n=6):
    return live_paper(db, make_paper, f"Report paper {next(_n)}", n=n)


def practise(db, make_paper, make_user, label, answered=1, n=6, session=None):
    """A student who has answered `answered` questions of a fresh paper in a practice session (so their answers are showing)."""
    paper = new_paper(db, make_paper, n)
    name = f"rep{next(_n)}{label}"
    student = make_user(name)
    attempt = attempt_of(db, start(student, paper.year, str(session or n)))
    for position in range(1, answered + 1):
        answer(student, attempt.id, position, "A")
    db.rollback()
    first = db.get(models.Attempt, attempt.id).responses[0].question_id
    return student, name, paper, attempt, first


def reports_of(db, question_id, status=None):
    db.rollback()
    q = db.query(models.QuestionReport).filter_by(question_id=question_id)
    return (q.filter_by(status=status) if status else q).all()


def send(student, question_id, kind="wrong_answer", note="", next_url=""):
    return student.post(f"/questions/{question_id}/report", data={"kind": kind, "note": note, "next": next_url})


# =========================================================================== report a problem

def test_a_student_can_report_a_question_whose_answer_is_showing(db, make_paper, make_user):
    student, name, paper, attempt, qid = practise(db, make_paper, make_user, "a")
    key = db.get(models.Question, qid).correct_answer
    r = send(student, qid, "wrong_answer", "Article 21 says the key should be B", next_url=f"/attempts/{attempt.id}/q/1")
    assert r.status_code == 303 and r.headers["location"] == f"/attempts/{attempt.id}/q/1"
    rows = reports_of(db, qid)
    assert len(rows) == 1
    assert (rows[0].kind, rows[0].status, rows[0].note, rows[0].answer_at_report) == \
        ("wrong_answer", "open", "Article 21 says the key should be B", key)
    assert rows[0].user_id == user_id(db, name)
    assert "your report was sent to the admin" in page(student, f"/attempts/{attempt.id}/q/1")


def test_reporting_changes_nothing_about_the_question_or_the_students_results(db, make_paper, make_user):
    student, name, paper, attempt, qid = practise(db, make_paper, make_user, "b", answered=2)
    db.rollback()
    before_q = db.get(models.Question, qid)
    snapshot = (before_q.correct_answer, before_q.status, before_q.explanation, before_q.text)
    responses = [(r.selected_answer, r.is_correct, r.marks_awarded) for r in db.get(models.Attempt, attempt.id).responses]
    send(student, qid, "wrong_answer", "wrong key")
    db.rollback()
    after_q = db.get(models.Question, qid)
    assert (after_q.correct_answer, after_q.status, after_q.explanation, after_q.text) == snapshot
    assert [(r.selected_answer, r.is_correct, r.marks_awarded) for r in db.get(models.Attempt, attempt.id).responses] == responses
    assert "Question" in page(student, f"/questions/{qid}")                   # still viewable


def test_the_form_shows_only_once_the_answer_is_showing(db, make_paper, make_user):
    paper = new_paper(db, make_paper)
    student = make_user(f"repform{next(_n)}")
    attempt = attempt_of(db, start(student, paper.year, "3"))
    assert "Report a problem" not in page(student, f"/attempts/{attempt.id}/q/1")            # not answered yet
    answer(student, attempt.id, 1, "A")
    text = page(student, f"/attempts/{attempt.id}/q/1")
    assert "Report a problem with this question" in text and "What looks wrong?" in text
    assert "Report a problem" not in page(student, f"/attempts/{attempt.id}/q/2")            # a different, unanswered one


def test_the_form_is_on_the_questions_own_page_too(db, make_paper, make_user):
    student, name, paper, attempt, qid = practise(db, make_paper, make_user, "c")
    assert "Report a problem with this question" in page(student, f"/questions/{qid}")


def test_a_question_in_a_running_test_cannot_be_reported_and_shows_no_form(db, make_paper, make_user):
    paper = timed_paper(db, make_paper, f"Running report paper {next(_n)}", n=4)
    student = make_user(f"reprun{next(_n)}")
    attempt = attempt_of(db, start_full(student, paper))
    assert "Report a problem" not in page(student, f"/attempts/{attempt.id}/q/1")
    db.rollback()
    qid = db.get(models.Attempt, attempt.id).responses[0].question_id
    r = send(student, qid, "wrong_answer", "peeking")
    assert r.status_code == 303 and not reports_of(db, qid)
    assert "once its answer has been shown" in page(student, r.headers["location"])
    student.post(f"/attempts/{attempt.id}/finish")
    assert send(student, qid, "wrong_answer", "now it is over").status_code == 303
    assert len(reports_of(db, qid)) == 1                                                       # allowed after the test ends


def test_a_question_the_student_never_met_or_that_is_not_live_is_404(db, make_paper, make_user):
    student, name, paper, attempt, qid = practise(db, make_paper, make_user, "d", answered=1, n=6, session=3)
    db.rollback()
    drawn = {r.question_id for r in db.get(models.Attempt, attempt.id).responses}
    unseen = next(q.id for q in questions_of(db, paper).values() if q.id not in drawn)      # never in this student's session
    assert send(student, unseen).status_code == 404 and not reports_of(db, unseen)
    stranger = make_user(f"repstranger{next(_n)}")
    assert send(stranger, qid).status_code == 404                        # someone else's question: not theirs to report
    assert not reports_of(db, qid)
    db.rollback()
    db.get(models.Paper, paper.id).publish_status = "draft"
    db.commit()
    assert send(student, qid).status_code == 404                         # no longer live
    assert send(student, 999999).status_code == 404


def test_reports_are_validated(db, make_paper, make_user):
    student, name, paper, attempt, qid = practise(db, make_paper, make_user, "e")
    assert "choose what is wrong" in page(student, send(student, qid, "nonsense").headers["location"])
    assert "say what the problem is" in page(student, send(student, qid, "other", "   ").headers["location"])
    assert "under 500 characters" in page(student, send(student, qid, "wrong_answer", "x" * 501).headers["location"])
    assert not reports_of(db, qid)
    assert send(student, qid, "other", "x" * 500).status_code == 303 and len(reports_of(db, qid)) == 1


def test_only_one_open_report_per_student_per_question(db, make_paper, make_user, admin):
    student, name, paper, attempt, qid = practise(db, make_paper, make_user, "f")
    send(student, qid, "wrong_answer", "first")
    r = send(student, qid, "wrong_text", "second")
    assert "already reported this question" in page(student, r.headers["location"])
    assert len(reports_of(db, qid)) == 1
    assert "You reported this on" in page(student, f"/questions/{qid}") and "What looks wrong?" not in page(student, f"/questions/{qid}")
    admin.post(f"/admin/reports/question/{qid}/close", data={"status": "resolved", "note": "fixed"})
    text = page(student, f"/questions/{qid}")
    assert "was resolved" in text and "What looks wrong?" in text                 # they may report again
    send(student, qid, "wrong_answer", "again")
    assert len(reports_of(db, qid)) == 2 and len(reports_of(db, qid, "open")) == 1


def test_a_student_can_only_send_so_many_reports_a_day(db, make_paper, make_user):
    student, name, paper, attempt, qid = practise(db, make_paper, make_user, "g")
    uid = user_id(db, name)
    other_paper = new_paper(db, make_paper, n=reports.DAILY_LIMIT)
    for q in questions_of(db, other_paper).values():
        db.add(models.QuestionReport(user_id=uid, question_id=q.id, kind="other", note="x", status="resolved"))
    db.commit()
    r = send(student, qid, "wrong_answer", "one too many")
    assert "a lot of reports today" in page(student, r.headers["location"]) and not reports_of(db, qid)
    old = db.query(models.QuestionReport).filter_by(user_id=uid).first()
    old.created_at = datetime.utcnow() - timedelta(hours=25)
    db.commit()
    send(student, qid, "wrong_answer", "now fine")
    assert len(reports_of(db, qid)) == 1


# --------------------------------------------------------------------------- the admin's queue

def test_the_queue_shows_the_question_the_report_and_who_sent_it(db, make_paper, make_user, admin):
    student, name, paper, attempt, qid = practise(db, make_paper, make_user, "h")
    send(student, qid, "wrong_answer", 'Key is wrong <script>alert(1)</script>')
    raw = admin.get("/admin/reports").text
    assert "<script>alert(1)" not in raw and "&lt;script&gt;alert(1)&lt;/script&gt;" in raw
    text = page(admin, "/admin/reports")
    assert paper.title in text and "The marked answer looks wrong" in text and name in text and "marked correct" in text
    number = db.get(models.Question, qid).question_number
    assert f'href="/review/{paper.id}#q{number}"' in raw and f"Q{number}" in text


def test_the_queue_notes_when_the_answer_key_has_changed_since(db, make_paper, make_user, admin):
    student, name, paper, attempt, qid = practise(db, make_paper, make_user, "i")
    send(student, qid, "wrong_answer", "wrong key")
    db.rollback()
    q = db.get(models.Question, qid)
    was = q.correct_answer
    q.correct_answer = "D" if was != "D" else "C"
    db.commit()
    assert f"the answer key was {was} then, {q.correct_answer} now" in page(admin, "/admin/reports")


def test_resolving_closes_every_open_report_on_the_question_and_is_audited(db, make_paper, make_user, admin):
    paper = new_paper(db, make_paper)
    s1, s2 = make_user(f"rep2a{next(_n)}"), make_user(f"rep2b{next(_n)}")
    attempts = []
    for s in (s1, s2):
        a = attempt_of(db, start(s, paper.year, "6"))
        for p in range(1, 7):
            answer(s, a.id, p, "A")
        attempts.append(a)
    db.rollback()
    qid = questions_of(db, paper)[1].id
    send(s1, qid, "wrong_answer", "from one")
    send(s2, qid, "wrong_explanation", "from two")
    text = page(admin, "/admin/reports")
    assert "2 reports · 2 open" in text and "Resolve all 2" in text

    r = admin.post(f"/admin/reports/question/{qid}/close", data={"status": "resolved", "note": "key corrected to C"})
    assert r.status_code == 303 and "2 reports resolved" in page(admin, "/admin/reports")
    rows = reports_of(db, qid)
    assert {x.status for x in rows} == {"resolved"} and all(x.resolved_by and x.resolved_at for x in rows)
    assert {x.resolution_note for x in rows} == {"key corrected to C"}
    logged = db.query(models.AuditLog).filter_by(action="report.resolve", entity_id=qid).all()
    assert len(logged) == 2 and all(e.paper_id == paper.id and e.username == "admin" for e in logged)

    resolved_view = page(admin, "/admin/reports?show=resolved")
    assert "Resolved by admin" in resolved_view and "key corrected to C" in resolved_view
    assert f"/admin/reports/question/{qid}/close" not in admin.get("/admin/reports").text          # nothing left to close


def test_dismissing_is_recorded_as_dismissed(db, make_paper, make_user, admin):
    student, name, paper, attempt, qid = practise(db, make_paper, make_user, "j")
    send(student, qid, "other", "not really a problem")
    admin.post(f"/admin/reports/question/{qid}/close", data={"status": "dismissed", "note": "key is right"})
    assert [x.status for x in reports_of(db, qid)] == ["dismissed"]
    assert db.query(models.AuditLog).filter_by(action="report.dismiss", entity_id=qid).count() == 1
    assert "closed without a change" in page(student, f"/questions/{qid}")


def test_closing_needs_a_valid_choice_and_an_open_report(db, make_paper, make_user, admin):
    student, name, paper, attempt, qid = practise(db, make_paper, make_user, "k")
    assert admin.post(f"/admin/reports/question/{qid}/close", data={"status": "resolved"}).status_code == 404     # nothing open
    send(student, qid, "wrong_answer", "x")
    assert admin.post(f"/admin/reports/question/{qid}/close", data={"status": "banana"}).status_code == 400
    assert admin.post(f"/admin/reports/question/{qid}/close", data={}).status_code == 400
    assert len(reports_of(db, qid, "open")) == 1


def test_the_dashboard_and_top_bar_count_open_reports(db, make_paper, make_user, admin):
    def badge(html_text):
        import re
        m = re.search(r'href="/admin/reports"[^>]*>(?:(?!</a>).)*?<span class="badge">(\d+)</span>', html_text, re.S)
        return int(m.group(1)) if m and m.group(1) else 0
    base = badge(admin.get("/admin").text)
    student, name, paper, attempt, qid = practise(db, make_paper, make_user, "l")
    send(student, qid, "wrong_answer", "x")
    assert badge(admin.get("/admin").text) == base + 1
    assert f"{base + 1} open" in page(admin, "/admin") and "Review reports" in page(admin, "/admin")
    admin.post(f"/admin/reports/question/{qid}/close", data={"status": "resolved"})
    assert badge(admin.get("/admin").text) == base


def test_students_and_visitors_cannot_use_the_queue(db, make_paper, make_user, anon):
    student, name, paper, attempt, qid = practise(db, make_paper, make_user, "m")
    send(student, qid, "wrong_answer", "x")
    assert student.get("/admin/reports").status_code == 403
    assert student.post(f"/admin/reports/question/{qid}/close", data={"status": "resolved"}).status_code == 403
    assert anon.get("/admin/reports").status_code == 303
    assert len(reports_of(db, qid, "open")) == 1


def test_a_reports_question_page_still_works_when_it_has_been_quarantined(db, make_paper, make_user, admin):
    student, name, paper, attempt, qid = practise(db, make_paper, make_user, "n")
    send(student, qid, "wrong_answer", "x")
    db.rollback()
    db.get(models.Question, qid).status = models.QStatus.QUARANTINED
    db.commit()
    text = page(admin, "/admin/reports")
    assert "Question status: quarantined" in text and "Quarantine page" in text


# =========================================================================== streaks and the daily target

TODAY = datetime.utcnow().date()


def days_ago(*offsets):
    return {TODAY - timedelta(days=d): 1 for d in offsets}


@pytest.mark.parametrize("offsets, current, best", [
    ([], 0, 0),
    ([0], 1, 1),
    ([0, 1, 2], 3, 3),
    ([1, 2, 3], 3, 3),               # nothing yet today, but the streak is still alive
    ([2, 3], 0, 2),                  # yesterday was missed: broken
    ([0, 1, 3, 4, 5, 6], 2, 4),      # a gap; the older run was longer
    ([0, 2], 1, 1),
])
def test_streak_rules(offsets, current, best):
    assert activity.streaks(days_ago(*offsets), TODAY) == (current, best)


def test_days_in_the_future_and_zero_counts_are_ignored():
    days = {TODAY + timedelta(days=1): 5, TODAY: 0, TODAY - timedelta(days=1): 2}
    assert activity.streaks(days, TODAY) == (1, 1)


def answered_at(db, uid, question_ids, moments):
    """One finished practice attempt with one answered question per moment (naive UTC times)."""
    attempt = models.Attempt(user_id=uid, kind="topic", status="submitted", started_at=min(moments), completed_at=max(moments),
                             total_questions=len(question_ids), counts_for_rank=False)
    db.add(attempt)
    db.flush()
    for position, (qid, when) in enumerate(zip(question_ids, moments), start=1):
        db.add(models.Response(attempt_id=attempt.id, question_id=qid, position=position, visited=True, selected_answer="A",
                               is_correct=True, answered_at=when, marks_if_correct=2.0, penalty_if_wrong=0.5, marks_awarded=2.0))
    db.commit()


@pytest.fixture()
def bank_of_questions(db, make_paper):
    return [q.id for q in questions_of(db, new_paper(db, make_paper, n=12)).values()]


def moment(day_offset, hh=12, mm=0):
    return datetime.combine(TODAY - timedelta(days=day_offset), time(hh, mm))


def student_row(db, make_user, label, target=None):
    name = f"act{next(_n)}{label}"
    make_user(name)
    db.rollback()
    user = db.query(models.User).filter_by(username=name).one()
    user.daily_target = target
    db.commit()
    return name, user.id


def test_summary_counts_todays_answers_against_the_target(db, make_user, bank_of_questions):
    name, uid = student_row(db, make_user, "a", target=8)
    answered_at(db, uid, bank_of_questions[:5], [moment(0, 9), moment(0, 10), moment(0, 11), moment(1), moment(2)])
    db.rollback()
    s = activity.summary(db, db.get(models.User, uid), today=TODAY, tz=timezone.utc)
    assert (s["today"], s["target"], s["percent"], s["remaining"], s["met"]) == (3, 8, 38, 5, False)
    assert (s["streak"], s["best"]) == (3, 3)

    answered_at(db, uid, bank_of_questions[5:10], [moment(0, 13)] * 5)
    db.rollback()
    s = activity.summary(db, db.get(models.User, uid), today=TODAY, tz=timezone.utc)
    assert (s["today"], s["percent"], s["remaining"], s["met"]) == (8, 100, 0, True)


def test_no_target_means_no_percentage(db, make_user, bank_of_questions):
    name, uid = student_row(db, make_user, "b")
    answered_at(db, uid, bank_of_questions[:2], [moment(0), moment(0)])
    s = activity.summary(db, db.get(models.User, uid), today=TODAY, tz=timezone.utc)
    assert s["today"] == 2 and s["target"] is None and s["percent"] is None and s["remaining"] is None and s["met"] is False


def test_the_day_is_the_students_local_day_not_utc(db, make_user, bank_of_questions):
    name, uid = student_row(db, make_user, "c")
    answered_at(db, uid, bank_of_questions[:1], [moment(1, 23, 30)])                    # 23:30 UTC yesterday
    ist = timezone(timedelta(hours=5, minutes=30))                                       # = 05:00 today in India
    db.rollback()
    user = db.get(models.User, uid)
    assert activity.summary(db, user, today=TODAY, tz=ist)["today"] == 1
    assert activity.summary(db, user, today=TODAY, tz=timezone.utc)["today"] == 0


def test_unanswered_and_cleared_responses_do_not_count(db, make_user, bank_of_questions):
    name, uid = student_row(db, make_user, "d")
    attempt = models.Attempt(user_id=uid, kind="topic", status="in_progress", started_at=moment(0), total_questions=2,
                             counts_for_rank=False)
    db.add(attempt)
    db.flush()
    db.add(models.Response(attempt_id=attempt.id, question_id=bank_of_questions[0], position=1, visited=True))
    db.add(models.Response(attempt_id=attempt.id, question_id=bank_of_questions[1], position=2, selected_answer=None,
                           answered_at=moment(0)))
    db.commit()
    assert activity.summary(db, db.get(models.User, uid), today=TODAY, tz=timezone.utc)["today"] == 0


def test_only_the_students_own_answers_count(db, make_user, bank_of_questions):
    mine, my_id = student_row(db, make_user, "e")
    theirs, their_id = student_row(db, make_user, "f")
    answered_at(db, their_id, bank_of_questions[:4], [moment(0)] * 4)
    assert activity.summary(db, db.get(models.User, my_id), today=TODAY, tz=timezone.utc)["today"] == 0


# --------------------------------------------------------------------------- the profile field

def test_the_daily_target_can_be_set_changed_and_cleared(db, make_user):
    name, uid = student_row(db, make_user, "g")
    student = make_user(name)
    assert 'name="daily_target"' in student.get("/account").text

    def save_target(value):
        return student.post("/account/profile", data={"display_name": "", "show_on_leaderboard": "true", "daily_target": value})

    assert save_target("25").status_code == 303
    db.rollback()
    assert db.get(models.User, uid).daily_target == 25 and 'value="25"' in student.get("/account").text
    assert save_target(" 30 ").status_code == 303
    db.rollback()
    assert db.get(models.User, uid).daily_target == 30
    assert save_target("").status_code == 303
    db.rollback()
    assert db.get(models.User, uid).daily_target is None


@pytest.mark.parametrize("bad", ["abc", "0", "-3", "501", "2.5", "1e3"])
def test_a_bad_daily_target_is_refused_and_nothing_changes(db, make_user, bad):
    name, uid = student_row(db, make_user, "h", target=12)
    student = make_user(name)
    r = student.post("/account/profile", data={"display_name": "", "show_on_leaderboard": "true", "daily_target": bad})
    assert r.status_code == 400 and "whole number of questions" in r.text
    db.rollback()
    assert db.get(models.User, uid).daily_target == 12


def test_admins_are_not_offered_a_daily_target(admin):
    assert 'name="daily_target"' not in admin.get("/account").text


# --------------------------------------------------------------------------- on the home page

def test_the_home_page_shows_progress_and_the_streak(db, make_paper, make_user):
    paper = new_paper(db, make_paper)
    name = f"acthome{next(_n)}"
    student = make_user(name)
    text = page(student, "/")
    assert "Today 0 questions answered" in text and "No streak yet" in text and "Set a daily target" in text

    student.post("/account/profile", data={"display_name": "", "show_on_leaderboard": "true", "daily_target": "4"})
    text = page(student, "/")
    assert "Today 0 of 4 questions answered" in text and "4 to go" in text and "Set a daily target" not in text

    attempt = attempt_of(db, start(student, paper.year, "6"))
    for position in (1, 2):
        answer(student, attempt.id, position, "A")
    text = page(student, "/")
    assert "2 of 4 questions answered" in text and "2 to go" in text and "1-day streak" in text
    assert 'style="width: 50%"' in student.get("/").text

    for position in (3, 4):
        answer(student, attempt.id, position, "A")
    text = page(student, "/")
    assert "4 of 4 questions answered" in text and "Target reached" in text


def test_a_student_can_never_see_anothers_progress_on_home(db, make_paper, make_user):
    paper = new_paper(db, make_paper)
    busy = make_user(f"actbusy{next(_n)}")
    quiet = make_user(f"actquiet{next(_n)}")
    attempt = attempt_of(db, start(busy, paper.year, "6"))
    for position in range(1, 5):
        answer(busy, attempt.id, position, "A")
    assert "Today 0 questions answered" in page(quiet, "/")
    assert "Today 4 questions answered" in page(busy, "/")
