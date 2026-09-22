"""
Student-side Stage 3: timed sectional and full-length tests — the server clock, autosave, resume, the palette,
the submit screen, marking with negative marks, automatic submission when time runs out, and privacy.
"""
import html
import os
import re
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, text

from app import database, ingest, models
from app.models import AttemptKind, AttemptStatus, QStatus
from app.practice import attempts as engine
from app.practice import pool
from conftest import _client, _login, question_form
from test_practice_stage2 import _year, attempt_of, live_paper, questions_of, user_id

JSON = {"Accept": "application/json"}


def timed_paper(db, make_paper, title, n=10, **fields):
    """A published paper of `n` live questions, all History, ready for a full-length test.
    Correct answers cycle A, B, C, D, A, B, ...  Defaults: 2 marks each, one third negative, 30 minutes."""
    fields.setdefault("duration_minutes", 30)
    paper = live_paper(db, make_paper, title, n=n, **fields)
    history = db.query(models.Subject).filter_by(name="History").one()
    for q in db.query(models.Question).filter_by(paper_id=paper.id).all():
        q.subject_id = history.id
    db.commit()
    return paper


def start_full(client, paper):
    return client.post("/tests/start", data={"mode": "full", "paper_id": str(paper.id)})


def start_section(client, paper, subject_id):
    return client.post("/tests/start", data={"mode": "section", "paper_id": str(paper.id), "subject_id": str(subject_id)})


def save(client, attempt_id, position, **data):
    return client.post(f"/attempts/{attempt_id}/q/{position}/save", data=data, headers=JSON)


def get_attempt(db, attempt_id):
    db.rollback()
    return db.get(models.Attempt, attempt_id)


def letter_for(position):          # the correct answer of question `position` in a timed_paper
    return "ABCD"[(position - 1) % 4]


def wrong_for(position):
    return "ABCD"[position % 4]


def force_deadline(db, attempt_id, seconds_ago):
    """Pretend the test's clock ran out `seconds_ago` seconds ago (keeping started_at consistent)."""
    a = get_attempt(db, attempt_id)
    duration = a.deadline_at - a.started_at
    a.deadline_at = datetime.utcnow() - timedelta(seconds=seconds_ago)
    a.started_at = a.deadline_at - duration
    db.commit()


# --------------------------------------------------------------------------- schema

def test_duration_minutes_column_is_added_to_existing_databases(tmp_path, monkeypatch):
    eng = create_engine(f"sqlite:///{tmp_path / 'old_papers.db'}")
    with eng.begin() as c:
        c.execute(text("CREATE TABLE papers (id INTEGER PRIMARY KEY, title VARCHAR NOT NULL)"))
    monkeypatch.setattr(database, "engine", eng)
    assert ("papers", "duration_minutes") in database.ensure_columns()


# --------------------------------------------------------------------------- the tests page

def test_the_tests_page_lists_only_what_can_be_started(db, make_paper, make_user):
    ready = timed_paper(db, make_paper, "Catalogue ready paper", n=4, duration_minutes=45)
    unset = timed_paper(db, make_paper, "Catalogue unset paper", n=3, marks=None, negative=None, duration_minutes=None)
    draft = make_paper("Catalogue draft paper", n=2, year=next(_year))                       # not published
    student = make_user("cataloguestudent")

    page = student.get("/tests")
    assert page.status_code == 200
    assert "Catalogue ready paper" in page.text and "4 questions · 45 minutes" in page.text
    assert "2 marks each" in page.text and "−0.67 for a wrong answer" in page.text
    assert 'name="mode" value="full"' in page.text and 'value="' + str(ready.id) + '"' in page.text
    assert "Catalogue unset paper" in page.text and "the marking scheme hasn't been set" in page.text
    assert "Catalogue draft paper" not in page.text
    assert "Custom timed test" in page.text and "never ranked" in page.text
    assert "History" in page.text                                                            # a section is offered


def test_full_length_start_buttons_are_absent_for_papers_without_a_scheme(db, make_paper, make_user):
    unset = timed_paper(db, make_paper, "No start button paper", n=2, marks=None, negative=None)
    page = make_user("nostartstudent").get("/tests").text
    block = page.split("No start button paper")[1].split("</li>")[0]
    assert "<form" not in block and "hasn't been set" in block


# --------------------------------------------------------------------------- starting a full-length test

def test_a_full_length_test_uses_the_papers_own_order_time_and_marking(db, make_paper, make_user):
    paper = timed_paper(db, make_paper, "Full start paper", n=6, marks=2.5, negative=0.25, duration_minutes=30)
    q = questions_of(db, paper)
    q[3].status = QStatus.NEEDS_REVIEW                                                       # not live: never included
    db.commit()
    student = make_user("fullstartstudent")

    attempt = attempt_of(db, start_full(student, paper))
    assert attempt.kind == AttemptKind.FULL and attempt.status == AttemptStatus.IN_PROGRESS
    assert attempt.mode == models.ExamType.full_length and attempt.timer_strict is True
    assert attempt.paper_id == paper.id and attempt.total_questions == 5
    numbers = [db.get(models.Question, r.question_id).question_number for r in attempt.responses]
    assert numbers == [1, 2, 4, 5, 6]                                                        # paper order, only live ones
    assert attempt.deadline_at - attempt.started_at == timedelta(minutes=30) and attempt.time_limit_minutes == 30
    assert all(r.marks_if_correct == 2.5 and r.penalty_if_wrong == pytest.approx(0.625) for r in attempt.responses)
    assert re.fullmatch(rf"paper:{paper.id}:full:[0-9a-f]{{8}}", attempt.rank_key)
    assert attempt.counts_for_rank is True


def test_without_a_stated_duration_a_test_gets_72_seconds_a_question(db, make_paper, make_user):
    paper = timed_paper(db, make_paper, "Default time paper", n=5, duration_minutes=None)
    attempt = attempt_of(db, start_full(make_user("defaulttimestudent"), paper))
    assert attempt.deadline_at - attempt.started_at == timedelta(seconds=5 * engine.SECONDS_PER_QUESTION)


@pytest.mark.parametrize("marks,negative", [(None, None), (2.0, None), (None, 1 / 3)])
def test_full_length_needs_the_papers_real_marking_scheme(db, make_paper, make_user, marks, negative):
    paper = timed_paper(db, make_paper, f"Unset scheme {marks} {negative}", n=2, marks=marks, negative=negative)
    student = make_user("schemerefusedstudent")
    before = db.query(models.Attempt).filter_by(user_id=user_id(db, "schemerefusedstudent")).count()
    r = start_full(student, paper)
    assert r.status_code == 303 and r.headers["location"] == "/tests"
    assert "marking scheme" in student.get("/tests").text
    db.rollback()
    assert db.query(models.Attempt).filter_by(user_id=user_id(db, "schemerefusedstudent")).count() == before


def test_a_zero_negative_scheme_counts_as_set(db, make_paper, make_user):
    paper = timed_paper(db, make_paper, "Zero negative timed", n=2, marks=1.0, negative=0.0)
    assert attempt_of(db, start_full(make_user("zeronegtimed"), paper)).kind == AttemptKind.FULL


def test_starting_the_same_ranked_test_twice_resumes_the_open_one(db, make_paper, make_user):
    paper = timed_paper(db, make_paper, "Resume instead paper", n=3)
    student = make_user("resumeinsteadstudent")
    first = attempt_of(db, start_full(student, paper))
    again = start_full(student, paper)
    assert again.headers["location"] == f"/attempts/{first.id}"
    assert "carrying on where you left off" in student.get(f"/attempts/{first.id}/q/1").text
    db.rollback()
    assert db.query(models.Attempt).filter_by(user_id=user_id(db, "resumeinsteadstudent")).count() == 1


def test_only_the_first_attempt_at_a_test_can_ever_count_for_ranking(db, make_paper, make_user):
    paper = timed_paper(db, make_paper, "First attempt paper", n=3)
    student = make_user("firstattemptstudent")
    first = attempt_of(db, start_full(student, paper))
    student.post(f"/attempts/{first.id}/finish")

    second = attempt_of(db, start_full(student, paper))
    third_student = attempt_of(db, start_full(make_user("otherfirststudent"), paper))
    assert get_attempt(db, first.id).counts_for_rank is True
    assert second.counts_for_rank is False and second.rank_key == first.rank_key
    assert third_student.counts_for_rank is True                                             # someone else's first attempt


def test_an_abandoned_first_attempt_still_uses_up_the_ranked_slot(db, make_paper, make_user):
    paper = timed_paper(db, make_paper, "Abandoned first paper", n=3)
    student = make_user("abandonstudent")
    first = attempt_of(db, start_full(student, paper))                                         # peek, then walk away
    force_deadline(db, first.id, seconds_ago=60)
    retake = attempt_of(db, start_full(student, paper))
    assert retake.id != first.id and retake.counts_for_rank is False
    assert get_attempt(db, first.id).status == AttemptStatus.EXPIRED and get_attempt(db, first.id).counts_for_rank is True


def test_the_rank_key_changes_if_the_set_of_live_questions_changes(db, make_paper, make_user, admin):
    paper = timed_paper(db, make_paper, "Fingerprint paper", n=4)
    student = make_user("fingerprintstudent")
    first = attempt_of(db, start_full(student, paper))
    student.post(f"/attempts/{first.id}/finish")

    q = questions_of(db, paper)[4]
    admin.post(f"/review/{paper.id}/question/{q.id}/quarantine", data={"reason": "Bad key"})   # 3 questions now
    second = attempt_of(db, start_full(student, paper))
    assert second.rank_key != first.rank_key and second.counts_for_rank is True                # a different test, a new board


# --------------------------------------------------------------------------- sectional and custom tests

def test_a_section_test_is_one_papers_whole_subject_section(db, make_paper, make_user):
    paper = timed_paper(db, make_paper, "Section paper", n=6, duration_minutes=None, marks=None, negative=None)
    geography = db.query(models.Subject).filter_by(name="Geography").one()
    history = db.query(models.Subject).filter_by(name="History").one()
    q = questions_of(db, paper)
    q[2].subject_id = q[4].subject_id = geography.id
    q[5].status = QStatus.NEEDS_REVIEW
    db.commit()

    attempt = attempt_of(db, start_section(make_user("sectionstudent"), paper, history.id))
    numbers = [db.get(models.Question, r.question_id).question_number for r in attempt.responses]
    assert numbers == [1, 3, 6]                                                                # History, live, in paper order
    assert attempt.kind == AttemptKind.SECTIONAL and attempt.subject_id == history.id and attempt.paper_id == paper.id
    assert attempt.deadline_at - attempt.started_at == timedelta(seconds=3 * engine.SECONDS_PER_QUESTION)
    assert re.fullmatch(rf"paper:{paper.id}:subject:{history.id}:[0-9a-f]{{8}}", attempt.rank_key)
    assert attempt.counts_for_rank is True
    assert attempt.responses[0].marks_if_correct == 2.0                                        # sectional may use the default scheme
    other = attempt_of(db, start_section(make_user("sectionstudent"), paper, geography.id))
    assert other.rank_key != attempt.rank_key and len(other.responses) == 2


def test_a_section_with_no_live_questions_is_refused(db, make_paper, make_user):
    paper = timed_paper(db, make_paper, "Empty section paper", n=2)
    polity = db.query(models.Subject).filter_by(name="Polity").one()
    student = make_user("emptysectionstudent")
    r = start_section(student, paper, polity.id)
    # (messages are HTML-escaped when shown: isn't -> isn&#39;t, so compare against the unescaped page)
    assert r.headers["location"] == "/tests" and "isn't available" in html.unescape(student.get("/tests").text)


def test_a_custom_test_is_random_timed_and_never_ranked(db, make_paper, make_user):
    year = next(_year)
    timed_paper(db, make_paper, "Custom paper", n=8, year=year)
    history = db.query(models.Subject).filter_by(name="History").one()
    student = make_user("customstudent")

    r = student.post("/tests/start", data={"mode": "custom", "subject_id": str(history.id), "year": str(year), "count": "5"})
    attempt = attempt_of(db, r)
    assert attempt.kind == AttemptKind.SECTIONAL and attempt.total_questions == 5
    assert attempt.rank_key is None and attempt.counts_for_rank is False
    assert attempt.deadline_at - attempt.started_at == timedelta(seconds=5 * engine.SECONDS_PER_QUESTION)
    assert len({x.question_id for x in attempt.responses}) == 5
    again = attempt_of(db, student.post("/tests/start", data={"mode": "custom", "subject_id": str(history.id),
                                                              "year": str(year), "count": "5"}))
    assert again.id != attempt.id                                                             # custom tests can be repeated freely

    capped = attempt_of(db, student.post("/tests/start", data={"mode": "custom", "subject_id": str(history.id),
                                                               "year": str(year), "count": "50"}))
    assert capped.total_questions == 8                                                        # only 8 exist


@pytest.mark.parametrize("form,fragment", [
    ({"mode": "custom", "count": "5"}, "Choose a subject"),
    ({"mode": "custom", "subject_id": "1", "count": "abc"}, "valid number of questions"),
    ({"mode": "custom", "subject_id": "1", "year": "1"}, "No questions match"),
    ({"mode": "nonsense"}, "Choose a test"),
    ({"mode": "full", "paper_id": "abc"}, "valid test"),
    ({"mode": "full", "paper_id": "999999"}, "isn't available"),
    ({"mode": "section", "paper_id": "999999", "subject_id": "1"}, "isn't available"),
])
def test_bad_start_requests_are_refused_with_a_message(db, make_user, form, fragment):
    student = make_user("badtimedstartstudent")
    before = db.query(models.Attempt).filter_by(user_id=user_id(db, "badtimedstartstudent")).count()
    r = student.post("/tests/start", data=form)
    assert r.status_code == 303 and r.headers["location"] == "/tests"
    assert fragment in html.unescape(student.get("/tests").text)
    db.rollback()
    assert db.query(models.Attempt).filter_by(user_id=user_id(db, "badtimedstartstudent")).count() == before


def test_unpublished_papers_cannot_be_started(db, make_paper, make_user, admin):
    paper = timed_paper(db, make_paper, "Unpublished timed paper", n=2)
    admin.post(f"/papers/{paper.id}/unpublish")
    student = make_user("unpublishedstartstudent")
    assert start_full(student, paper).headers["location"] == "/tests"
    assert "isn't available" in html.unescape(student.get("/tests").text)


# --------------------------------------------------------------------------- the server clock

def _clock(html):
    return (re.search(r'data-deadline="([^"]+)"', html).group(1), re.search(r'data-server-now="([^"]+)"', html).group(1))


def test_the_deadline_is_fixed_by_the_server_and_survives_refreshes_and_other_devices(db, make_paper, make_user):
    paper = timed_paper(db, make_paper, "Clock paper", n=3, duration_minutes=20)
    student = make_user("clockstudent")
    attempt = attempt_of(db, start_full(student, paper))

    deadline1, now1 = _clock(student.get(f"/attempts/{attempt.id}/q/1").text)
    deadline2, _ = _clock(student.get(f"/attempts/{attempt.id}/q/1").text)                     # refresh
    deadline3, _ = _clock(student.get(f"/attempts/{attempt.id}/q/2").text)                     # another question
    other_device = _login(_client(), "clockstudent", "studentpass1")
    deadline4, _ = _clock(other_device.get(f"/attempts/{attempt.id}/q/1").text)                # a different browser
    assert deadline1 == deadline2 == deadline3 == deadline4
    assert deadline1.endswith("Z") and now1.endswith("Z")

    assert deadline1 == get_attempt(db, attempt.id).deadline_at.isoformat() + "Z"
    left = (datetime.fromisoformat(deadline1[:-1]) - datetime.fromisoformat(now1[:-1])).total_seconds()
    assert 19 * 60 <= left <= 20 * 60


def test_remaining_seconds(db, make_paper, make_user):
    paper = timed_paper(db, make_paper, "Remaining paper", n=2, duration_minutes=10)
    attempt = attempt_of(db, start_full(make_user("remainingstudent"), paper))
    now = attempt.started_at
    assert engine.remaining_seconds(attempt, now) == 600
    assert engine.remaining_seconds(attempt, now + timedelta(seconds=599.2)) == 1
    assert engine.remaining_seconds(attempt, now + timedelta(minutes=11)) == 0

    practice_year = next(_year)
    live_paper(db, make_paper, "Untimed remaining paper", n=2, year=practice_year)
    practice = attempt_of(db, make_user("remainingstudent").post(
        "/practice/start", data={"year": str(practice_year), "count": "2"}))
    assert engine.remaining_seconds(practice) is None and engine.time_left_label(practice) == ""
    assert engine.time_left_label(attempt).endswith("min left")


def test_the_test_page_has_a_countdown_a_palette_and_no_feedback(db, make_paper, make_user):
    paper = timed_paper(db, make_paper, "Screen paper", n=4)
    student = make_user("screenstudent")
    attempt = attempt_of(db, start_full(student, paper))
    page = student.get(f"/attempts/{attempt.id}/q/1").text
    for expected in ('id="timer"', 'data-deadline=', 'src="/static/test.js"', '<details class="palette" id="palette">',
                     "Submit test", "Mark for review", "Clear response", "How sure are you?", "Sure", "Guessed", "No idea",
                     'name="viewport"'):
        assert expected in page, expected
    for leak in ("Correct answer", "Explanation for question", "verdict", "Your answer"):
        assert leak not in page


# --------------------------------------------------------------------------- autosave

def test_answers_are_saved_as_you_go_and_can_be_changed(db, make_paper, make_user):
    paper = timed_paper(db, make_paper, "Autosave paper", n=3)
    student = make_user("autosavestudent")
    attempt = attempt_of(db, start_full(student, paper))

    r = save(student, attempt.id, 1, answer="B", confidence="guessed")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert r.json()["entry"] == {"pos": 1, "state": "answered", "marked": False} and r.json()["answered"] == 1
    saved = get_attempt(db, attempt.id).responses[0]
    assert (saved.selected_answer, saved.confidence, saved.answered_at is not None) == ("B", models.Confidence.guessed, True)

    save(student, attempt.id, 1, answer="D", confidence="sure")                                # a timed test allows changes
    changed = get_attempt(db, attempt.id).responses[0]
    assert (changed.selected_answer, changed.confidence) == ("D", models.Confidence.sure)

    save(student, attempt.id, 1, confidence="no_idea")                                         # confidence alone
    partial = get_attempt(db, attempt.id).responses[0]
    assert (partial.selected_answer, partial.confidence) == ("D", models.Confidence.no_idea)

    save(student, attempt.id, 2, confidence="sure")                                            # confidence with no answer yet
    only_conf = get_attempt(db, attempt.id).responses[1]
    assert only_conf.selected_answer is None and only_conf.answered_at is None
    assert engine.answered_count(get_attempt(db, attempt.id)) == 1                             # not counted as answered


def test_mark_for_review_is_independent_of_the_answer(db, make_paper, make_user):
    paper = timed_paper(db, make_paper, "Mark paper", n=2)
    student = make_user("markstudent")
    attempt = attempt_of(db, start_full(student, paper))

    assert save(student, attempt.id, 1, marked="1").json()["entry"] == {"pos": 1, "state": "unanswered", "marked": True}
    save(student, attempt.id, 1, answer="A")
    row = get_attempt(db, attempt.id).responses[0]
    assert row.marked_for_review is True and row.selected_answer == "A"                        # answering didn't unmark it
    assert save(student, attempt.id, 1, marked="0").json()["entry"]["marked"] is False
    assert get_attempt(db, attempt.id).responses[0].selected_answer == "A"                     # unmarking didn't clear it


def test_clear_response_removes_the_answer_and_confidence(db, make_paper, make_user):
    paper = timed_paper(db, make_paper, "Clear paper", n=2)
    student = make_user("clearstudent")
    attempt = attempt_of(db, start_full(student, paper))
    save(student, attempt.id, 1, answer="C", confidence="sure", marked="1")

    r = save(student, attempt.id, 1, clear="1")
    assert r.json()["entry"] == {"pos": 1, "state": "unanswered", "marked": True}              # still marked for review
    row = get_attempt(db, attempt.id).responses[0]
    assert (row.selected_answer, row.confidence, row.answered_at) == (None, models.Confidence.skipped, None)

    # "Clear" wins even if the form still had an option ticked (the no-JavaScript case).
    save(student, attempt.id, 1, answer="C")
    save(student, attempt.id, 1, clear="1", answer="C", confidence="sure")
    assert get_attempt(db, attempt.id).responses[0].selected_answer is None


@pytest.mark.parametrize("data,fragment", [
    ({"answer": "E"}, "Choose one of the options"), ({"answer": "AB"}, "Choose one of the options"),
    ({"answer": "A", "confidence": "certain"}, "how sure"),
])
def test_invalid_saves_are_rejected_and_change_nothing(db, make_paper, make_user, data, fragment):
    paper = timed_paper(db, make_paper, f"Invalid save {data}", n=1)
    student = make_user("invalidsavestudent")
    attempt = attempt_of(db, start_full(student, paper))
    r = save(student, attempt.id, 1, **data)
    assert r.status_code == 400 and r.json()["ok"] is False and fragment in r.json()["error"]
    assert get_attempt(db, attempt.id).responses[0].selected_answer is None


def test_nothing_about_correctness_is_revealed_or_stored_during_the_test(db, make_paper, make_user):
    paper = timed_paper(db, make_paper, "No feedback paper", n=2)
    student = make_user("nofeedbackstudent")
    attempt = attempt_of(db, start_full(student, paper))

    body = save(student, attempt.id, 1, answer=letter_for(1), confidence="sure").json()
    assert "correct" not in str(body).lower() and "is_correct" not in body                    # not in the save response
    page = student.get(f"/attempts/{attempt.id}/q/1").text
    assert "Correct." not in page and "Not this time" not in page and "Explanation" not in page
    assert f'value="{letter_for(1)}" checked' in page                                        # but the choice is remembered
    row = get_attempt(db, attempt.id).responses[0]
    assert row.is_correct is None and row.marks_awarded is None                              # marked only at the end


def test_the_topic_practice_and_test_routes_do_not_mix(db, make_paper, make_user):
    paper = timed_paper(db, make_paper, "Mix routes paper", n=2)
    student = make_user("mixroutesstudent")
    test = attempt_of(db, start_full(student, paper))
    student.post(f"/attempts/{test.id}/q/1/answer", data={"answer": "A", "confidence": "sure"})   # practice route on a test
    assert "timed test" in student.get(f"/attempts/{test.id}/q/1").text
    assert get_attempt(db, test.id).responses[0].selected_answer is None

    year = next(_year)
    live_paper(db, make_paper, "Practice for mix routes", n=1, year=year)
    practice = attempt_of(db, student.post("/practice/start", data={"year": str(year), "count": "1"}))
    r = save(student, practice.id, 1, answer="A")                                                # test route on practice
    assert r.status_code == 400 and "isn't a timed test" in r.json()["error"]


def test_saving_without_javascript_redirects_and_can_move_on(db, make_paper, make_user):
    paper = timed_paper(db, make_paper, "No JS paper", n=3)
    student = make_user("nojsstudent")
    attempt = attempt_of(db, start_full(student, paper))
    url = f"/attempts/{attempt.id}/q/2/save"

    r = student.post(url, data={"answer": "B", "confidence": "sure", "marked": "1"})
    assert r.status_code == 303 and r.headers["location"] == f"/attempts/{attempt.id}/q/2"
    assert student.post(url, data={"answer": "C", "go": "next"}).headers["location"] == f"/attempts/{attempt.id}/q/3"
    assert student.post(url, data={"go": "prev"}).headers["location"] == f"/attempts/{attempt.id}/q/1"
    last = f"/attempts/{attempt.id}/q/3/save"
    assert student.post(last, data={"answer": "A", "go": "next"}).headers["location"] == f"/attempts/{attempt.id}/q/3"
    row = get_attempt(db, attempt.id).responses[1]
    assert row.selected_answer == "C" and row.marked_for_review is True                       # "" left the mark unchanged


# --------------------------------------------------------------------------- resuming and the palette

def test_a_test_resumes_where_you_left_off_from_any_device(db, make_paper, make_user):
    paper = timed_paper(db, make_paper, "Resume test paper", n=5)
    student = make_user("resumetestststudent"[:20])
    attempt = attempt_of(db, start_full(student, paper))
    save(student, attempt.id, 1, answer="A", confidence="sure")
    student.get(f"/attempts/{attempt.id}/q/4")                                                # wandered on to question 4
    save(student, attempt.id, 4, answer="B")

    elsewhere = _login(_client(), "resumetestststudent"[:20], "studentpass1")
    assert elsewhere.get(f"/attempts/{attempt.id}").headers["location"] == f"/attempts/{attempt.id}/q/4"
    assert 'value="B" checked' in elsewhere.get(f"/attempts/{attempt.id}/q/4").text
    assert 'value="A" checked' in elsewhere.get(f"/attempts/{attempt.id}/q/1").text
    home = elsewhere.get("/").text
    assert "Continue where you left off" in home and "min left" in home and "2 of 5 answered" in home
    assert "min left" in elsewhere.get("/practice").text


def test_the_palette_shows_answered_unanswered_not_visited_and_marked(db, make_paper, make_user):
    paper = timed_paper(db, make_paper, "Palette paper", n=5)
    student = make_user("palettestudent")
    attempt = attempt_of(db, start_full(student, paper))

    def states():
        db.rollback()
        return {p["pos"]: (p["state"], p["marked"]) for p in engine.palette(db.get(models.Attempt, attempt.id))}

    student.get(f"/attempts/{attempt.id}/q/1")
    assert states() == {1: ("unanswered", False), 2: ("notvisited", False), 3: ("notvisited", False),
                        4: ("notvisited", False), 5: ("notvisited", False)}
    save(student, attempt.id, 1, answer="A")
    student.get(f"/attempts/{attempt.id}/q/2")
    save(student, attempt.id, 3, marked="1")                                                   # marking counts as a visit target
    save(student, attempt.id, 4, answer="D", marked="1")
    result = states()
    assert result[1] == ("answered", False) and result[2] == ("unanswered", False)
    assert result[3][1] is True and result[4] == ("answered", True) and result[5] == ("notvisited", False)

    page = student.get(f"/attempts/{attempt.id}/q/5").text
    assert re.search(r'id="pal-1"[^>]*class="pal answered', page) and "pal notvisited" in page
    assert re.search(r'id="pal-4"[^>]*class="pal answered marked', page)
    assert "1</span>/5 answered" in page.replace("\n", "") or "/5 answered" in page


# --------------------------------------------------------------------------- the submit screen

def test_the_submit_screen_shows_what_is_still_open(db, make_paper, make_user):
    paper = timed_paper(db, make_paper, "Submit screen paper", n=6)
    student = make_user("submitscreenstudent")
    attempt = attempt_of(db, start_full(student, paper))
    student.get(f"/attempts/{attempt.id}/q/1")
    student.get(f"/attempts/{attempt.id}/q/2")
    save(student, attempt.id, 1, answer="A", confidence="sure")
    save(student, attempt.id, 2, answer="B")                                                   # no confidence given
    save(student, attempt.id, 3, marked="1")

    page = student.get(f"/attempts/{attempt.id}/submit").text
    counts = re.findall(r'<div class="stat-num">(\d+)</div><div class="paper-meta">([^<]+)</div>', page)
    assert dict((label, int(n)) for n, label in counts) == {
        "answered": 2, "not answered": 4, "marked for review": 1, "not visited": 4}
    assert "1 answer was saved without saying how sure you were" in page
    assert "Submit now" in page and "Go back to the test" in page
    assert f"/attempts/{attempt.id}/q/3" in page                                              # jump to first unanswered / marked
    assert 'id="timer"' in page and "data-deadline" in page


def test_the_submit_screen_redirects_when_it_does_not_apply(db, make_paper, make_user):
    paper = timed_paper(db, make_paper, "Submit redirect paper", n=2)
    student = make_user("submitredirectstudent")
    test = attempt_of(db, start_full(student, paper))
    student.post(f"/attempts/{test.id}/finish")
    assert student.get(f"/attempts/{test.id}/submit").headers["location"] == f"/attempts/{test.id}/result"

    year = next(_year)
    live_paper(db, make_paper, "Practice submit redirect", n=1, year=year)
    practice = attempt_of(db, student.post("/practice/start", data={"year": str(year), "count": "1"}))
    assert student.get(f"/attempts/{practice.id}/submit").headers["location"] == f"/attempts/{practice.id}"


# --------------------------------------------------------------------------- marking (worked by hand)

def test_scoring_with_negative_marking_worked_by_hand(db, make_paper, make_user):
    """10 questions, 2 marks each, a wrong answer costs a third of the marks (2/3).
       6 right  = 6 x 2        = +12
       3 wrong  = 3 x (-2/3)   = -2
       1 skipped               =  0   (never penalised)
       score = 12 - 2 = 10 out of a possible 20."""
    paper = timed_paper(db, make_paper, "Hand scoring paper", n=10, marks=2.0, negative=1 / 3)
    student = make_user("handscorestudent")
    attempt = attempt_of(db, start_full(student, paper))
    for pos in range(1, 7):
        save(student, attempt.id, pos, answer=letter_for(pos), confidence="sure")
    for pos in (7, 8, 9):
        save(student, attempt.id, pos, answer=wrong_for(pos), confidence="guessed")
    student.get(f"/attempts/{attempt.id}/q/10")                                              # seen, not answered

    r = student.post(f"/attempts/{attempt.id}/finish")
    assert r.status_code == 303 and r.headers["location"] == f"/attempts/{attempt.id}/result"

    done = get_attempt(db, attempt.id)
    assert done.status == AttemptStatus.SUBMITTED
    assert (done.correct_count, done.wrong_count, done.skipped_count, done.total_questions) == (6, 3, 1, 10)
    assert done.score == pytest.approx(10.0) and done.max_marks == pytest.approx(20.0)
    marks = [r.marks_awarded for r in done.responses]
    assert marks[:6] == [2.0] * 6 and marks[6:9] == [pytest.approx(-2 / 3)] * 3 and marks[9] is None
    assert [r.is_correct for r in done.responses] == [True] * 6 + [False] * 3 + [None]
    assert 0 <= done.time_taken_seconds <= 5 and done.completed_at is not None

    page = student.get(f"/attempts/{attempt.id}/result").text
    assert "10 <span" in page and "/ 20" in page                                              # the score card: 10 / 20
    assert "+12 for right answers" in page and "−2 for wrong answers" in page and "the 1 you skipped" in page
    assert "67%" in page                                                                       # 6 of 9 answered were right
    assert "wrong · -0.67" in page.replace("\n", " ") or "-0.67" in page


def test_each_scheme_gives_the_expected_score(db, make_paper, make_user):
    cases = [                     # (marks, negative, right, wrong, skipped) -> hand-computed score
        (1.0, 0.0, 3, 2, 0, 3.0),               # no negative marking: wrong answers cost nothing
        (2.5, 0.2, 2, 1, 1, 4.5),               # 5.0 - 0.5
        (4.0, 0.25, 1, 2, 1, 2.0),              # 4.0 - 2.0
        (2.0, 1 / 3, 0, 3, 0, -2.0),            # negative total is allowed: 3 x (-2/3)
    ]
    for marks, negative, right, wrong, skipped, expected in cases:
        n = right + wrong + skipped
        paper = timed_paper(db, make_paper, f"Scheme {marks}/{negative}/{right}{wrong}{skipped}", n=n,
                            marks=marks, negative=negative)
        student = make_user("schemesstudent")
        attempt = attempt_of(db, start_full(student, paper))
        pos = 0
        for _ in range(right):
            pos += 1
            save(student, attempt.id, pos, answer=letter_for(pos))
        for _ in range(wrong):
            pos += 1
            save(student, attempt.id, pos, answer=wrong_for(pos))
        student.post(f"/attempts/{attempt.id}/finish")
        done = get_attempt(db, attempt.id)
        assert done.score == pytest.approx(expected), (marks, negative, right, wrong, skipped)
        assert done.max_marks == pytest.approx(marks * n)


def test_only_the_final_answer_counts_and_confidence_alone_is_not_an_answer(db, make_paper, make_user):
    paper = timed_paper(db, make_paper, "Final answer paper", n=3, marks=2.0, negative=0.5)
    student = make_user("finalanswerstudent")
    attempt = attempt_of(db, start_full(student, paper))
    save(student, attempt.id, 1, answer=wrong_for(1), confidence="sure")
    save(student, attempt.id, 1, answer=letter_for(1), confidence="sure")                      # changed their mind: now right
    save(student, attempt.id, 2, answer=letter_for(2), confidence="sure")
    save(student, attempt.id, 2, clear="1")                                                    # ...then cleared: skipped
    save(student, attempt.id, 3, confidence="guessed")                                         # rated but never answered
    student.post(f"/attempts/{attempt.id}/finish")
    done = get_attempt(db, attempt.id)
    assert (done.correct_count, done.wrong_count, done.skipped_count) == (1, 0, 2)
    assert done.score == pytest.approx(2.0)


def test_results_use_the_marks_a_test_started_with_even_if_the_paper_changes(db, make_paper, make_user, admin):
    paper = timed_paper(db, make_paper, "Snapshot marks paper", n=2, marks=2.0, negative=0.5)
    student = make_user("snapshotmarksstudent")
    attempt = attempt_of(db, start_full(student, paper))
    save(student, attempt.id, 1, answer=letter_for(1))
    save(student, attempt.id, 2, answer=wrong_for(2))

    r = admin.post(f"/papers/{paper.id}/settings", data={"marks_per_question": "10", "negative_fraction": "1"})
    assert r.status_code == 303
    student.post(f"/attempts/{attempt.id}/finish")
    assert get_attempt(db, attempt.id).score == pytest.approx(2.0 - 1.0)                       # 2 marks, -1 for the wrong one
    new_attempt = attempt_of(db, start_full(student, paper))
    assert new_attempt.responses[0].marks_if_correct == 10.0                                   # new tests use the new scheme


def test_the_key_is_read_when_marking_so_a_corrected_answer_counts(db, make_paper, make_user, admin):
    paper = timed_paper(db, make_paper, "Corrected key paper", n=1)
    student = make_user("correctedkeystudent")
    attempt = attempt_of(db, start_full(student, paper))
    save(student, attempt.id, 1, answer="B")                                                   # the key says A
    q = questions_of(db, paper)[1]
    q.correct_answer = "B"                                                                     # the admin fixes the key
    db.commit()
    student.post(f"/attempts/{attempt.id}/finish")
    assert get_attempt(db, attempt.id).correct_count == 1


# --------------------------------------------------------------------------- time per question in a test

def test_time_on_a_question_keeps_counting_while_answers_can_change(db, make_paper, make_user):
    paper = timed_paper(db, make_paper, "Test timing paper", n=2)
    student = make_user("testtimingstudent")
    attempt = attempt_of(db, start_full(student, paper))

    def rewind(seconds):
        a = get_attempt(db, attempt.id)
        a.last_event_at = datetime.utcnow() - timedelta(seconds=seconds)
        db.commit()

    student.get(f"/attempts/{attempt.id}/q/1")
    rewind(20)
    save(student, attempt.id, 1, answer="A")
    first = get_attempt(db, attempt.id).responses[0].time_spent_seconds
    assert 19 <= first <= 22
    rewind(10)
    save(student, attempt.id, 1, answer="B")                                                   # thinking again after answering
    assert 29 <= get_attempt(db, attempt.id).responses[0].time_spent_seconds <= 33


# --------------------------------------------------------------------------- time runs out

def test_a_test_whose_time_ran_out_is_marked_automatically_with_what_was_saved(db, make_paper, make_user):
    paper = timed_paper(db, make_paper, "Expiry paper", n=4, marks=2.0, negative=0.5, duration_minutes=10)
    student = make_user("expirystudent")
    attempt = attempt_of(db, start_full(student, paper))
    save(student, attempt.id, 1, answer=letter_for(1), confidence="sure")                    # +2
    save(student, attempt.id, 2, answer=wrong_for(2), confidence="guessed")                  # -1
    force_deadline(db, attempt.id, seconds_ago=30)                                            # the clock ran out; nobody submitted

    r = student.get(f"/attempts/{attempt.id}/q/1")                                            # the next time anyone looks
    assert r.status_code == 200
    done = get_attempt(db, attempt.id)
    assert done.status == AttemptStatus.EXPIRED
    assert (done.correct_count, done.wrong_count, done.skipped_count) == (1, 1, 2)
    assert done.score == pytest.approx(1.0) and done.max_marks == pytest.approx(8.0)
    assert done.completed_at == done.deadline_at                                              # it ended AT the deadline
    assert done.time_taken_seconds == 600

    result = student.get(f"/attempts/{attempt.id}/result").text
    assert "Time ran out" in result and "10m 00s" in result
    assert "Correct." in student.get(f"/attempts/{attempt.id}/q/1").text                     # review mode now shows answers


def test_changes_after_time_is_up_are_refused_but_a_short_grace_is_allowed(db, make_paper, make_user):
    paper = timed_paper(db, make_paper, "Grace paper", n=2)
    student = make_user("gracestudent")
    attempt = attempt_of(db, start_full(student, paper))

    force_deadline(db, attempt.id, seconds_ago=1)                                             # 1 s late: network delay
    ok = save(student, attempt.id, 1, answer="A")
    assert ok.status_code == 200 and ok.json()["ok"] is True
    assert get_attempt(db, attempt.id).status == AttemptStatus.IN_PROGRESS

    force_deadline(db, attempt.id, seconds_ago=engine.GRACE_SECONDS + 5)                      # clearly too late
    late = save(student, attempt.id, 2, answer="B")
    assert late.status_code == 409 and late.json() == {
        "ok": False, "expired": True, "redirect": f"/attempts/{attempt.id}/result"}
    done = get_attempt(db, attempt.id)
    assert done.status == AttemptStatus.EXPIRED
    assert [r.selected_answer for r in done.responses] == ["A", None]                        # the late answer was not recorded

    no_js = student.post(f"/attempts/{attempt.id}/q/2/save", data={"answer": "C"})
    assert no_js.status_code == 303 and no_js.headers["location"] == f"/attempts/{attempt.id}/result"


def test_submitting_after_the_deadline_is_recorded_as_expired_at_the_deadline(db, make_paper, make_user):
    paper = timed_paper(db, make_paper, "Auto submit paper", n=2, duration_minutes=10)
    student = make_user("autosubmitstudent")
    attempt = attempt_of(db, start_full(student, paper))
    save(student, attempt.id, 1, answer=letter_for(1))
    force_deadline(db, attempt.id, seconds_ago=1)                                             # the browser's auto-submit arrives 1 s late
    student.post(f"/attempts/{attempt.id}/finish")
    done = get_attempt(db, attempt.id)
    assert done.status == AttemptStatus.EXPIRED and done.completed_at == done.deadline_at and done.time_taken_seconds == 600


def test_finishing_early_is_a_normal_submission_with_the_real_time_taken(db, make_paper, make_user):
    paper = timed_paper(db, make_paper, "Early finish paper", n=2, duration_minutes=60)
    student = make_user("earlyfinishstudent")
    attempt = attempt_of(db, start_full(student, paper))
    a = get_attempt(db, attempt.id)
    a.started_at -= timedelta(minutes=7)
    a.deadline_at -= timedelta(minutes=7)
    db.commit()
    student.post(f"/attempts/{attempt.id}/finish")
    done = get_attempt(db, attempt.id)
    assert done.status == AttemptStatus.SUBMITTED and 7 * 60 <= done.time_taken_seconds <= 7 * 60 + 5


def test_abandoned_tests_are_marked_by_the_sweep_without_the_student_returning(db, make_paper, make_user):
    paper = timed_paper(db, make_paper, "Sweep paper", n=3, marks=2.0, negative=0.5)
    gone, present = make_user("sweepgonestudent"), make_user("sweeppresentstudent")
    abandoned = attempt_of(db, start_full(gone, paper))
    save(gone, abandoned.id, 1, answer=letter_for(1))
    other = attempt_of(db, start_full(present, paper))
    force_deadline(db, abandoned.id, seconds_ago=60)
    force_deadline(db, other.id, seconds_ago=60)

    present.get("/practice")                                                                    # touching your own list settles YOUR tests only
    assert get_attempt(db, other.id).status == AttemptStatus.EXPIRED
    assert get_attempt(db, abandoned.id).status == AttemptStatus.IN_PROGRESS

    db.rollback()
    assert engine.expire_overdue(db) >= 1                                                       # what app start-up does for everyone
    done = get_attempt(db, abandoned.id)
    assert done.status == AttemptStatus.EXPIRED and done.score == pytest.approx(2.0) and done.correct_count == 1


def test_finished_tests_are_never_reopened_or_re_marked(db, make_paper, make_user):
    paper = timed_paper(db, make_paper, "No reopen paper", n=2)
    student = make_user("noreopenstudent")
    attempt = attempt_of(db, start_full(student, paper))
    save(student, attempt.id, 1, answer=letter_for(1))
    student.post(f"/attempts/{attempt.id}/finish")
    before = get_attempt(db, attempt.id)
    snapshot = (before.score, before.completed_at, before.status)

    r = save(student, attempt.id, 2, answer="A")
    assert r.status_code == 409 and r.json()["expired"] is True
    student.post(f"/attempts/{attempt.id}/finish")
    engine.expire_overdue(db)
    after = get_attempt(db, attempt.id)
    assert (after.score, after.completed_at, after.status) == snapshot


# --------------------------------------------------------------------------- privacy and live-only

def test_nobody_can_touch_someone_elses_test(db, make_paper, make_user, admin, anon):
    paper = timed_paper(db, make_paper, "Private test paper", n=3)
    owner = make_user("privateownerstudent")
    attempt = attempt_of(db, start_full(owner, paper))
    save(owner, attempt.id, 1, answer="A")
    a = attempt.id

    for who, client in {"another student": make_user("privateintruder"), "the admin": admin}.items():
        for method, url, data in [("get", f"/attempts/{a}/submit", None),
                                  ("post", f"/attempts/{a}/q/1/save", {"answer": "D"}),
                                  ("post", f"/attempts/{a}/q/1/save", {"clear": "1"}),
                                  ("get", f"/attempts/{a}/q/1", None), ("post", f"/attempts/{a}/finish", None)]:
            r = getattr(client, method)(url, **({"data": data, "headers": JSON} if data else {}))
            assert r.status_code == 404, f"{who}: {method.upper()} {url} -> {r.status_code}"
    for url in (f"/attempts/{a}/submit", f"/tests"):
        assert anon.get(url).status_code == 303
    assert anon.post(f"/attempts/{a}/q/1/save", data={"answer": "B"}).status_code == 303

    untouched = get_attempt(db, a)
    assert untouched.status == AttemptStatus.IN_PROGRESS and untouched.responses[0].selected_answer == "A"


def test_unpublishing_mid_test_removes_the_question_and_refuses_answers_to_it(db, make_paper, make_user, admin):
    paper = timed_paper(db, make_paper, "Unpublish mid test", n=2)
    student = make_user("unpublishmidstudent")
    attempt = attempt_of(db, start_full(student, paper))
    save(student, attempt.id, 1, answer=letter_for(1), confidence="sure")
    admin.post(f"/papers/{paper.id}/unpublish")

    page = student.get(f"/attempts/{attempt.id}/q/2").text
    assert "no longer available" in page and 'name="answer"' not in page
    r = save(student, attempt.id, 2, answer="A")
    assert r.status_code == 400 and "no longer available" in r.json()["error"]
    assert get_attempt(db, attempt.id).responses[1].selected_answer is None
    student.post(f"/attempts/{attempt.id}/finish")                                             # what was saved still gets marked
    assert get_attempt(db, attempt.id).correct_count == 1


def test_image_dependent_questions_show_their_snapshot_in_a_test(db, make_paper, make_user):
    paper = timed_paper(db, make_paper, "Test snapshot paper", n=1)
    folder = ingest.images_dir_for(paper.id)
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "q1.jpg"), "wb") as f:
        f.write(b"\xff\xd8\xff fake")
    q = questions_of(db, paper)[1]
    q.has_image, q.source_image_path = True, "q1.jpg"
    db.commit()
    student = make_user("testsnapshotstudent")
    attempt = attempt_of(db, start_full(student, paper))
    assert f"/media/{paper.id}/q1.jpg" in student.get(f"/attempts/{attempt.id}/q/1").text


# --------------------------------------------------------------------------- results pages and lists

def test_a_finished_test_appears_under_recent_sessions_with_its_marks(db, make_paper, make_user):
    paper = timed_paper(db, make_paper, "Recent test paper", n=2)
    student = make_user("recenttestwstudent"[:19])
    attempt = attempt_of(db, start_full(student, paper))
    save(student, attempt.id, 1, answer=letter_for(1))
    student.post(f"/attempts/{attempt.id}/finish")
    page = student.get("/practice").text
    assert "Recent sessions" in page and "Full-length test" in page and f"/attempts/{attempt.id}/result" in page


def test_topic_practice_results_have_no_score_card(db, make_paper, make_user):
    year = next(_year)
    live_paper(db, make_paper, "No score card paper", n=2, year=year)
    student = make_user("noscorecardstudent")
    attempt = attempt_of(db, student.post("/practice/start", data={"year": str(year), "count": "2"}))
    student.post(f"/attempts/{attempt.id}/finish")
    page = student.get(f"/attempts/{attempt.id}/result").text
    assert "Your score" not in page and "time taken" not in page


# --------------------------------------------------------------------------- admin: marking scheme and time

def test_the_admin_can_set_the_marking_scheme_and_time_and_it_is_audited(admin, db, make_paper):
    paper = make_paper("Settings paper", n=1)
    r = admin.post(f"/papers/{paper.id}/settings", data={
        "expected_total": "100", "marks_per_question": "2", "negative_fraction": "1/3", "duration_minutes": "120"})
    assert r.status_code == 303
    db.rollback()
    saved = db.get(models.Paper, paper.id)
    assert (saved.expected_total, saved.marks_per_question, saved.duration_minutes) == (100, 2.0, 120)
    assert saved.negative_fraction == pytest.approx(0.3333)
    entry = db.query(models.AuditLog).filter_by(action="paper.settings", paper_id=paper.id).one()
    assert "duration_minutes" in entry.detail_json
    page = admin.get(f"/review/{paper.id}").text
    assert "Marking scheme and time" in page and "120" in page

    admin.post(f"/papers/{paper.id}/settings", data={"marks_per_question": "", "negative_fraction": ""})   # blank = unset
    db.rollback()
    cleared = db.get(models.Paper, paper.id)
    assert cleared.marks_per_question is None and cleared.negative_fraction is None and cleared.duration_minutes is None
    assert "full-length tests are unavailable" in admin.get(f"/review/{paper.id}").text


@pytest.mark.parametrize("field,value,fragment", [
    ("marks_per_question", "0", "Marks per question"), ("negative_fraction", "3/2", "Negative marking"),
    ("negative_fraction", "1/0", "Negative marking"), ("duration_minutes", "0", "Duration"),
    ("duration_minutes", "601", "Duration"), ("expected_total", "-4", "Expected number"),
])
def test_bad_scheme_values_are_refused_and_nothing_is_saved(admin, db, make_paper, field, value, fragment):
    paper = make_paper(f"Bad settings {field}{value}", n=1)
    admin.post(f"/papers/{paper.id}/settings", data={field: value})
    assert fragment in admin.get(f"/review/{paper.id}").text
    db.rollback()
    assert db.query(models.AuditLog).filter_by(action="paper.settings", paper_id=paper.id).count() == 0


def test_the_upload_form_takes_a_time_allowed(admin):
    page = admin.get("/upload").text
    assert 'name="duration_minutes"' in page and "72 seconds per question" in page
    bad = admin.post("/upload", data={"title": "Bad minutes", "exam_type": "full_length", "duration_minutes": "0"},
                     files={"pdf_file": ("x.pdf", b"%PDF-1.4", "application/pdf")})
    assert bad.status_code == 400 and "Duration" in bad.text


# --------------------------------------------------------------------------- pages

def test_the_test_screen_is_built_for_phones_and_desktops(db, make_paper, make_user):
    paper = timed_paper(db, make_paper, "Layout paper", n=2)
    student = make_user("layoutstudent")
    attempt = attempt_of(db, start_full(student, paper))
    page = student.get(f"/attempts/{attempt.id}/q/1").text
    assert 'class="container wide"' in page and 'name="viewport"' in page
    css_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app", "static", "style.css")
    css = open(css_path, encoding="utf-8").read()
    assert ".test-bar" in css and "position: sticky" in css and "@media (min-width: 900px)" in css
    js = open(os.path.join(os.path.dirname(css_path), "test.js"), encoding="utf-8").read()
    assert "keepalive" in js and "finishForm.submit()" in js and "data-server-now" not in js.replace("dataset.serverNow", "")
