"""
Student-side Stage 2: choosing what to practise, untimed topic practice with immediate feedback,
saved attempts, resuming, ownership of attempts, and the rule that students only ever see live questions.
"""
import json
import os
import re
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from app import database, ingest, models
from app.models import AttemptStatus, QStatus
from app.practice import attempts as engine
from app.practice import pool
from conftest import _client, _login, question_form

_year = iter(range(1000, 3000))     # every test filters by its own year so shared test data can't interfere


def live_paper(db, make_paper, title, n=6, year=None, source_type="official_pyq", marks=2.0, negative=1 / 3, **fields):
    """A published paper with `n` live questions. Answers cycle A, B, C, D, A, B, ..."""
    paper = make_paper(title, n=n, publish_status="published", year=year or next(_year), source_type=source_type,
                       marks_per_question=marks, negative_fraction=negative, **fields)
    for q in db.query(models.Question).filter_by(paper_id=paper.id).all():
        q.status = QStatus.LIVE
        q.explanation = f"Explanation for question {q.question_number}."
    db.commit()
    return paper


def questions_of(db, paper):
    db.rollback()
    return {q.question_number: q for q in
            db.query(models.Question).filter_by(paper_id=paper.id).order_by(models.Question.question_number)}


def user_id(db, username):
    db.rollback()
    return db.query(models.User).filter_by(username=username).one().id


def start(client, year, count="6", **extra):
    return client.post("/practice/start", data={"year": str(year), "count": count, **extra})


def attempt_of(db, response):
    """The attempt a /practice/start redirect points at."""
    assert response.status_code == 303, response.text[:300]
    match = re.fullmatch(r"/attempts/(\d+)", response.headers["location"])
    assert match, response.headers["location"]
    db.rollback()
    return db.get(models.Attempt, int(match.group(1)))


def answer(client, attempt_id, position, letter="A", confidence="sure"):
    return client.post(f"/attempts/{attempt_id}/q/{position}/answer", data={"answer": letter, "confidence": confidence})


def correct_letter(db, attempt, position):
    db.rollback()
    resp = db.get(models.Attempt, attempt.id).responses[position - 1]
    return db.get(models.Question, resp.question_id).correct_answer


def wrong_letter(db, attempt, position):
    return next(l for l in "ABCD" if l != correct_letter(db, attempt, position))


# --------------------------------------------------------------------------- schema upgrade

def _legacy_tables(engine_):
    with engine_.begin() as c:
        c.execute(text("CREATE TABLE papers (id INTEGER PRIMARY KEY)"))
        c.execute(text("CREATE TABLE attempts (id INTEGER PRIMARY KEY, paper_id INTEGER NOT NULL, mode VARCHAR NOT NULL, "
                       "started_at DATETIME, score FLOAT)"))
        c.execute(text("CREATE TABLE responses (id INTEGER PRIMARY KEY, attempt_id INTEGER NOT NULL, "
                       "question_id INTEGER NOT NULL, selected_answer VARCHAR)"))


def test_the_empty_legacy_attempt_tables_are_rebuilt_in_the_new_shape(tmp_path, monkeypatch):
    eng = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    _legacy_tables(eng)
    monkeypatch.setattr(database, "engine", eng)

    assert database.upgrade_empty_attempt_tables() is True
    database.Base.metadata.create_all(bind=eng)

    cols = {c["name"]: c for c in inspect(eng).get_columns("attempts")}
    assert {"user_id", "kind", "status", "rank_key", "deadline_at", "filters_json"} <= set(cols)
    assert cols["paper_id"]["nullable"] is True                       # sessions can mix papers
    assert {"position", "marks_if_correct", "marks_awarded", "marked_for_review"} <= {
        c["name"] for c in inspect(eng).get_columns("responses")}
    with eng.begin() as c:
        c.execute(text("INSERT INTO users (username, password_hash, is_admin, status, show_on_leaderboard, "
                       "must_change_password, session_version) VALUES ('u','x',0,'approved',1,0,0)"))
        c.execute(text("INSERT INTO attempts (user_id, kind, status, counts_for_rank) VALUES (1,'topic','in_progress',0)"))
        c.execute(text("INSERT INTO responses (attempt_id, question_id, position, confidence, visited, marked_for_review) "
                       "VALUES (1, 5, 1, 'skipped', 0, 0)"))
    with pytest.raises(IntegrityError), eng.begin() as c:              # one response per question per attempt
        c.execute(text("INSERT INTO responses (attempt_id, question_id, position, confidence, visited, marked_for_review) "
                       "VALUES (1, 5, 2, 'skipped', 0, 0)"))
    assert database.upgrade_empty_attempt_tables() is False           # already upgraded: nothing to do


def test_legacy_attempt_tables_that_contain_data_are_never_dropped(tmp_path, monkeypatch):
    eng = create_engine(f"sqlite:///{tmp_path / 'legacy_full.db'}")
    _legacy_tables(eng)
    with eng.begin() as c:
        c.execute(text("INSERT INTO attempts (paper_id, mode) VALUES (1, 'sectional')"))
    monkeypatch.setattr(database, "engine", eng)
    with pytest.raises(RuntimeError, match="cannot"):
        database.upgrade_empty_attempt_tables()
    with eng.connect() as c:
        assert c.execute(text("SELECT COUNT(*) FROM attempts")).scalar() == 1     # untouched


# --------------------------------------------------------------------------- filters

def test_filters_are_validated(db):
    subject = db.query(models.Subject).filter_by(name="History").one()
    topic = models.Topic(name="Filter topic A", subject_id=subject.id)
    other = db.query(models.Subject).filter_by(name="Geography").one()
    db.add(topic)
    db.commit()

    f = pool.parse_filters(db, source_type="official_pyq", year="2020", subject_id=str(subject.id),
                           topic_id=str(topic.id), difficulty="Hard", unattempted="1")
    assert (f.source_type, f.year, f.subject_id, f.topic_id, f.difficulty, f.unattempted) == (
        "official_pyq", 2020, subject.id, topic.id, "hard", True)
    assert pool.parse_filters(db) == pool.Filters()                                  # everything blank = no filter

    for kwargs, fragment in [
        ({"source_type": "blog"}, "source"), ({"year": "twenty"}, "Year"), ({"subject_id": "99999"}, "subject"),
        ({"topic_id": "99999"}, "topic"), ({"difficulty": "nightmare"}, "difficulty"),
        ({"subject_id": str(other.id), "topic_id": str(topic.id)}, "isn't in the chosen subject"),
    ]:
        with pytest.raises(ValueError, match=fragment):
            pool.parse_filters(db, **kwargs)


def test_every_filter_narrows_the_live_pool_correctly(db, make_paper, make_user):
    student = make_user("filterstudent")
    uid = user_id(db, "filterstudent")
    history = db.query(models.Subject).filter_by(name="History").one()
    polity = db.query(models.Subject).filter_by(name="Polity").one()
    t_hist = models.Topic(name="Filter topic hist", subject_id=history.id)
    t_pol = models.Topic(name="Filter topic pol", subject_id=polity.id)
    db.add_all([t_hist, t_pol])
    db.commit()

    year = next(_year)
    official = live_paper(db, make_paper, "Filter official", n=4, year=year, source_type="official_pyq")
    live_paper(db, make_paper, "Filter coaching", n=2, year=year, source_type="coaching_test")
    q = questions_of(db, official)
    q[1].subject_id, q[1].topic_id, q[1].difficulty = history.id, t_hist.id, "easy"
    q[2].subject_id, q[2].topic_id, q[2].difficulty = history.id, t_hist.id, "hard"
    q[3].subject_id, q[3].topic_id, q[3].difficulty = polity.id, t_pol.id, "easy"
    q[4].status = QStatus.NEEDS_REVIEW                                                # never in any pool
    db.commit()

    def numbers(**kw):
        f = pool.parse_filters(db, year=str(year), **kw)
        db.rollback()
        return sorted((db.get(models.Paper, x.paper_id).title[7:], x.question_number)
                      for x in pool.filtered_questions(db, uid, f).all())

    assert len(numbers()) == 5                                                        # 3 official live + 2 coaching
    assert [t for t, _ in numbers(source_type="coaching_test")] == ["coaching", "coaching"]
    assert numbers(subject_id=str(history.id)) == [("official", 1), ("official", 2)]
    assert numbers(topic_id=str(t_pol.id)) == [("official", 3)]
    assert numbers(difficulty="easy") == [("official", 1), ("official", 3)]
    assert numbers(subject_id=str(history.id), difficulty="hard") == [("official", 2)]

    # "Only questions I haven't answered": answering one removes it from that student's pool, not from others'.
    attempt = attempt_of(db, start(student, year, "10"))
    first_question = attempt.responses[0].question_id
    answer(student, attempt.id, 1, "A", "sure")
    unattempted = pool.parse_filters(db, year=str(year), unattempted="1")
    db.rollback()
    assert pool.filtered_questions(db, uid, unattempted).count() == 4
    assert first_question not in [x.id for x in pool.filtered_questions(db, uid, unattempted).all()]
    assert pool.filtered_questions(db, uid + 999, unattempted).count() == 5


def test_filter_options_only_offer_what_has_live_questions(db, make_paper, make_user):
    subject = db.query(models.Subject).filter_by(name="Economy").one()
    topic_live = models.Topic(name="Options live topic", subject_id=subject.id)
    topic_draft = models.Topic(name="Options draft topic", subject_id=subject.id)
    db.add_all([topic_live, topic_draft])
    db.commit()
    live = live_paper(db, make_paper, "Options live", n=1, source_type="coaching_test")
    draft = make_paper("Options draft", n=1, year=1899, source_type="official_pyq")   # not published
    live_q, draft_q = questions_of(db, live)[1], questions_of(db, draft)[1]     # (questions_of rolls back: fetch both first)
    live_q.subject_id, live_q.topic_id, live_q.difficulty = subject.id, topic_live.id, "tricky"
    draft_q.subject_id, draft_q.topic_id, draft_q.difficulty = subject.id, topic_draft.id, "medium"
    db.commit()

    options = pool.filter_options(db)
    topic_names = {t["name"] for t in options["topics"]}
    assert "Options live topic" in topic_names and "Options draft topic" not in topic_names
    assert 1899 not in options["years"] and live.year in options["years"]
    assert "tricky" in options["difficulties"]

    page = make_user("optionsstudent").get("/practice")
    assert page.status_code == 200 and "Options live topic" in page.text and "Options draft topic" not in page.text


def test_the_match_count_endpoint(db, make_paper, make_user):
    year = next(_year)
    live_paper(db, make_paper, "Count paper", n=5, year=year)
    student = make_user("countstudent")
    assert student.get(f"/practice/count?year={year}").json() == {"count": 5, "error": None}
    assert student.get(f"/practice/count?year={year}&difficulty=hard").json()["count"] == 0
    bad = student.get("/practice/count?difficulty=nightmare")
    assert bad.status_code == 400 and "difficulty" in bad.json()["error"]


# --------------------------------------------------------------------------- starting a session

def test_starting_a_session_creates_a_saved_attempt_with_a_marks_snapshot(db, make_paper, make_user):
    year = next(_year)
    paper = live_paper(db, make_paper, "Start paper", n=6, year=year, marks=2.5, negative=0.25)
    student = make_user("startstudent")

    attempt = attempt_of(db, start(student, year, "4"))
    assert attempt.user_id == user_id(db, "startstudent")
    assert (attempt.kind, attempt.status, attempt.counts_for_rank) == ("topic", "in_progress", False)
    assert attempt.paper_id == paper.id and attempt.total_questions == 4
    assert json.loads(attempt.filters_json)["year"] == year
    responses = attempt.responses
    assert [r.position for r in responses] == [1, 2, 3, 4]
    assert len({r.question_id for r in responses}) == 4                               # no question twice
    assert all(r.marks_if_correct == 2.5 and r.penalty_if_wrong == pytest.approx(0.625) for r in responses)
    assert all(r.answered_at is None and r.confidence == models.Confidence.skipped for r in responses)


def test_papers_without_a_marking_scheme_fall_back_to_the_defaults(db, make_paper, make_user):
    year = next(_year)
    live_paper(db, make_paper, "No scheme paper", n=2, year=year, marks=None, negative=None)
    attempt = attempt_of(db, start(make_user("noschemestudent"), year, "2"))
    r = attempt.responses[0]
    assert r.marks_if_correct == 2.0 and r.penalty_if_wrong == pytest.approx(2.0 / 3)


def test_a_zero_negative_marking_scheme_is_respected_not_replaced_by_the_default(db, make_paper, make_user):
    year = next(_year)
    live_paper(db, make_paper, "Zero negative paper", n=1, year=year, marks=1.0, negative=0.0)
    attempt = attempt_of(db, start(make_user("zeronegstudent"), year, "1"))
    assert attempt.responses[0].penalty_if_wrong == 0.0


def test_a_session_can_mix_papers_and_then_has_no_single_paper(db, make_paper, make_user):
    year = next(_year)
    live_paper(db, make_paper, "Mix one", n=2, year=year)
    live_paper(db, make_paper, "Mix two", n=2, year=year)
    attempt = attempt_of(db, start(make_user("mixstudent"), year, "4"))
    assert attempt.paper_id is None and len({r.question_id for r in attempt.responses}) == 4


def test_the_question_count_is_capped_by_what_matches_and_by_100(db, make_paper, make_user):
    student = make_user("capstudent")
    year = next(_year)
    live_paper(db, make_paper, "Small pool", n=3, year=year)
    assert attempt_of(db, start(student, year, "50")).total_questions == 3            # only 3 exist

    big = next(_year)
    live_paper(db, make_paper, "Big pool", n=105, year=big)
    assert attempt_of(db, start(student, big, "500")).total_questions == engine.MAX_SESSION_QUESTIONS


@pytest.mark.parametrize("form,fragment", [
    ({"count": "abc"}, "how many"), ({"count": "0"}, "how many"), ({"count": "-3"}, "how many"),
    ({"year": "20x0"}, "Year"), ({"difficulty": "nightmare"}, "difficulty"),
])
def test_bad_start_requests_bounce_back_with_a_message_and_create_nothing(db, make_user, form, fragment):
    student = make_user("badstartstudent")
    before = db.query(models.Attempt).filter_by(user_id=user_id(db, "badstartstudent")).count()
    r = student.post("/practice/start", data={"count": "5", **form})
    assert r.status_code == 303 and r.headers["location"] == "/practice"
    assert fragment in student.get("/practice").text
    db.rollback()
    assert db.query(models.Attempt).filter_by(user_id=user_id(db, "badstartstudent")).count() == before


def test_no_matching_questions_gives_a_friendly_message(db, make_user):
    student = make_user("nomatchstudent")
    r = student.post("/practice/start", data={"year": "1801", "count": "5"})
    assert r.headers["location"] == "/practice"
    assert "No questions match" in student.get("/practice").text


def test_only_live_questions_are_ever_selected(db, make_paper, make_user):
    year = next(_year)
    paper = live_paper(db, make_paper, "Selection paper", n=6, year=year)
    q = questions_of(db, paper)
    q[2].status = QStatus.NEEDS_REVIEW
    q[3].status = QStatus.QUARANTINED
    q[4].status = QStatus.VERIFIED                                                     # verified but not published
    q[5].correct_answer = None
    db.commit()
    student = make_user("selectionstudent")
    for _ in range(5):                                                                 # random order: try a few times
        attempt = attempt_of(db, start(student, year, "10"))
        chosen = {db.get(models.Question, r.question_id).question_number for r in attempt.responses}
        assert chosen == {1, 6}


# --------------------------------------------------------------------------- resuming

def test_resume_lands_on_the_first_unanswered_question_even_from_another_browser(db, make_paper, make_user):
    year = next(_year)
    live_paper(db, make_paper, "Resume paper", n=5, year=year)
    student = make_user("resumestudent")
    attempt = attempt_of(db, start(student, year, "5"))

    assert student.get(f"/attempts/{attempt.id}").headers["location"] == f"/attempts/{attempt.id}/q/1"
    answer(student, attempt.id, 1)
    answer(student, attempt.id, 2)
    assert student.get(f"/attempts/{attempt.id}").headers["location"] == f"/attempts/{attempt.id}/q/3"

    other_browser = _login(_client(), "resumestudent", "studentpass1")                # closed the tab, came back later
    assert other_browser.get(f"/attempts/{attempt.id}").headers["location"] == f"/attempts/{attempt.id}/q/3"
    home = other_browser.get("/")
    assert "Continue where you left off" in home.text and f"/attempts/{attempt.id}" in home.text
    assert "2 of 5 answered" in home.text
    assert f"/attempts/{attempt.id}" in other_browser.get("/practice").text


# --------------------------------------------------------------------------- answering: feedback, locking, validation

def test_nothing_about_the_answer_is_sent_before_the_student_answers(db, make_paper, make_user):
    year = next(_year)
    live_paper(db, make_paper, "Secrecy paper", n=1, year=year)
    student = make_user("secretstudent")
    attempt = attempt_of(db, start(student, year, "1"))
    page = student.get(f"/attempts/{attempt.id}/q/1").text
    assert 'name="answer"' in page and 'name="confidence"' in page                      # the form is there
    for leak in ("Correct answer", "Explanation for question", "verdict", "Your answer", "opt correct", "Correct."):
        assert leak not in page, f"answer information leaked before answering: {leak!r}"


def test_a_wrong_answer_is_graded_shown_and_locked(db, make_paper, make_user):
    year = next(_year)
    live_paper(db, make_paper, "Wrong answer paper", n=2, year=year, marks=2.0, negative=0.5)
    student = make_user("wrongstudent")
    attempt = attempt_of(db, start(student, year, "2"))
    right, wrong = correct_letter(db, attempt, 1), wrong_letter(db, attempt, 1)

    r = answer(student, attempt.id, 1, wrong, "guessed")
    assert r.status_code == 303 and r.headers["location"] == f"/attempts/{attempt.id}/q/1"
    page = student.get(f"/attempts/{attempt.id}/q/1").text
    assert "Not this time" in page and f"the answer is {right}" in page
    assert "Correct answer" in page and "Your answer" in page
    assert "Explanation for question" in page and "Unverified (as printed in the answer PDF)" in page
    assert "guessed" in page and 'name="answer"' not in page                             # the form is gone

    db.rollback()
    resp = db.get(models.Attempt, attempt.id).responses[0]
    assert resp.selected_answer == wrong and resp.is_correct is False
    assert resp.confidence == models.Confidence.guessed
    assert resp.marks_awarded == pytest.approx(-1.0)                                     # 2 marks x 0.5 negative
    assert resp.answered_at is not None

    # Locked: a second answer to the same question is refused and changes nothing.
    answer(student, attempt.id, 1, right, "sure")
    assert "already answered" in student.get(f"/attempts/{attempt.id}/q/1").text
    db.rollback()
    resp = db.get(models.Attempt, attempt.id).responses[0]
    assert resp.selected_answer == wrong and resp.confidence == models.Confidence.guessed


def test_a_right_answer_earns_the_marks(db, make_paper, make_user):
    year = next(_year)
    live_paper(db, make_paper, "Right answer paper", n=1, year=year, marks=2.0, negative=0.5)
    student = make_user("rightstudent")
    attempt = attempt_of(db, start(student, year, "1"))
    answer(student, attempt.id, 1, correct_letter(db, attempt, 1), "sure")
    assert "Correct." in student.get(f"/attempts/{attempt.id}/q/1").text
    db.rollback()
    resp = db.get(models.Attempt, attempt.id).responses[0]
    assert resp.is_correct is True and resp.marks_awarded == 2.0


@pytest.mark.parametrize("letter,confidence,fragment", [
    ("A", "", "how sure"), ("A", "certain", "how sure"), ("", "sure", "Choose one of the options"),
    ("E", "sure", "Choose one of the options"), ("AB", "sure", "Choose one of the options"),
])
def test_invalid_answers_are_rejected_and_leave_the_question_unanswered(db, make_paper, make_user, letter, confidence, fragment):
    year = next(_year)
    live_paper(db, make_paper, f"Invalid answer paper {letter}{confidence}", n=1, year=year)
    student = make_user("invalidstudent")
    attempt = attempt_of(db, start(student, year, "1"))
    r = answer(student, attempt.id, 1, letter, confidence)
    assert r.status_code == 303
    assert fragment in student.get(f"/attempts/{attempt.id}/q/1").text
    db.rollback()
    assert db.get(models.Attempt, attempt.id).responses[0].answered_at is None


def test_lowercase_answers_are_accepted(db, make_paper, make_user):
    year = next(_year)
    live_paper(db, make_paper, "Lowercase paper", n=1, year=year)
    student = make_user("lowercasestudent")
    attempt = attempt_of(db, start(student, year, "1"))
    answer(student, attempt.id, 1, correct_letter(db, attempt, 1).lower(), "no_idea")
    db.rollback()
    resp = db.get(models.Attempt, attempt.id).responses[0]
    assert resp.is_correct is True and resp.confidence == models.Confidence.no_idea


def test_out_of_range_question_positions_are_a_404(db, make_paper, make_user):
    year = next(_year)
    live_paper(db, make_paper, "Range paper", n=2, year=year)
    student = make_user("rangestudent")
    attempt = attempt_of(db, start(student, year, "2"))
    for position in (0, 3, 99, -1):
        assert student.get(f"/attempts/{attempt.id}/q/{position}").status_code == 404
        assert answer(student, attempt.id, position).status_code == 404


# --------------------------------------------------------------------------- time is measured on the server

def test_time_spent_is_measured_by_the_server_and_capped(db, make_paper, make_user):
    year = next(_year)
    live_paper(db, make_paper, "Timing paper", n=3, year=year)
    student = make_user("timingstudent")
    attempt = attempt_of(db, start(student, year, "3"))

    def rewind(seconds):
        db.rollback()
        db.get(models.Attempt, attempt.id).last_event_at = datetime.utcnow() - timedelta(seconds=seconds)
        db.commit()

    def spent(position):
        db.rollback()
        return db.get(models.Attempt, attempt.id).responses[position - 1].time_spent_seconds or 0

    student.get(f"/attempts/{attempt.id}/q/1")
    rewind(30)
    answer(student, attempt.id, 1, "A", "sure")
    assert 29 <= spent(1) <= 32                                                          # ~30 s on question 1

    student.get(f"/attempts/{attempt.id}/q/2")
    rewind(20)
    student.get(f"/attempts/{attempt.id}/q/3")                                           # skipped past question 2
    assert 19 <= spent(2) <= 22                                                          # its time still counted

    rewind(3 * 3600)                                                                     # walked away for hours
    answer(student, attempt.id, 3, "A", "sure")
    assert spent(3) == engine.TIME_CAP_SECONDS

    student.get(f"/attempts/{attempt.id}/q/1")                                           # looking at feedback isn't "time spent"
    rewind(60)
    student.get(f"/attempts/{attempt.id}/q/2")
    assert 29 <= spent(1) <= 32


# --------------------------------------------------------------------------- finishing and results

def test_finishing_totals_the_attempt_and_turns_the_pages_into_review_pages(db, make_paper, make_user):
    year = next(_year)
    live_paper(db, make_paper, "Finish paper", n=4, year=year, marks=2.0, negative=0.5)
    student = make_user("finishstudent")
    attempt = attempt_of(db, start(student, year, "4"))
    answer(student, attempt.id, 1, correct_letter(db, attempt, 1), "sure")               # +2
    answer(student, attempt.id, 2, correct_letter(db, attempt, 2), "guessed")            # +2
    answer(student, attempt.id, 3, wrong_letter(db, attempt, 3), "no_idea")              # -1
    # question 4 is left unanswered

    assert student.get(f"/attempts/{attempt.id}/result").headers["location"] == f"/attempts/{attempt.id}"   # not done yet
    r = student.post(f"/attempts/{attempt.id}/finish")
    assert r.status_code == 303 and r.headers["location"] == f"/attempts/{attempt.id}/result"

    db.rollback()
    done = db.get(models.Attempt, attempt.id)
    assert done.status == AttemptStatus.SUBMITTED and done.completed_at is not None
    assert (done.correct_count, done.wrong_count, done.skipped_count, done.total_questions) == (2, 1, 1, 4)
    assert done.score == pytest.approx(3.0) and done.max_marks == pytest.approx(8.0)

    page = student.get(f"/attempts/{attempt.id}/result").text
    assert "67%" in page                                                                 # 2 of 3 answered were right
    assert student.get(f"/attempts/{attempt.id}").headers["location"] == f"/attempts/{attempt.id}/result"

    review = student.get(f"/attempts/{attempt.id}/q/4").text                              # the skipped one now shows its answer
    assert "You didn't answer this one" in review and 'name="answer"' not in review
    assert "finished" in student.get(f"/attempts/{attempt.id}/q/1").text
    assert answer(student, attempt.id, 4, "A", "sure").status_code == 303
    assert "session has finished" in student.get(f"/attempts/{attempt.id}/q/4").text
    db.rollback()
    assert db.get(models.Attempt, attempt.id).responses[3].answered_at is None

    student.post(f"/attempts/{attempt.id}/finish")                                        # twice is harmless
    db.rollback()
    assert db.get(models.Attempt, attempt.id).score == pytest.approx(3.0)


def test_practising_again_starts_a_new_attempt_and_never_overwrites_the_old_one(db, make_paper, make_user):
    year = next(_year)
    live_paper(db, make_paper, "Retake paper", n=3, year=year)
    student = make_user("retakestudent")
    first = attempt_of(db, start(student, year, "3"))
    answer(student, first.id, 1, correct_letter(db, first, 1), "sure")
    student.post(f"/attempts/{first.id}/finish")

    second = attempt_of(db, start(student, year, "3"))
    assert second.id != first.id
    db.rollback()
    old = db.get(models.Attempt, first.id)
    assert old.status == AttemptStatus.SUBMITTED and old.correct_count == 1 and old.total_questions == 3
    assert db.query(models.Attempt).filter_by(user_id=user_id(db, "retakestudent")).count() == 2


def test_a_finished_session_lists_under_recent_sessions(db, make_paper, make_user):
    year = next(_year)
    live_paper(db, make_paper, "Recent paper", n=2, year=year)
    student = make_user("recentstudent")
    attempt = attempt_of(db, start(student, year, "2"))
    answer(student, attempt.id, 1, correct_letter(db, attempt, 1), "sure")
    student.post(f"/attempts/{attempt.id}/finish")
    page = student.get("/practice").text
    assert "Recent sessions" in page and f"/attempts/{attempt.id}/result" in page
    assert "1 correct of 1 answered" in page


# --------------------------------------------------------------------------- privacy: attempts belong to their owner

def test_nobody_can_reach_someone_elses_attempt_by_changing_the_id(db, make_paper, make_user, admin, anon):
    year = next(_year)
    live_paper(db, make_paper, "Ownership paper", n=2, year=year)
    owner = make_user("ownerstudent")
    attempt = attempt_of(db, start(owner, year, "2"))
    answer(owner, attempt.id, 1, "A", "sure")
    a = attempt.id

    intruders = {"another student": make_user("intruderstudent"), "the admin": admin}
    for who, client in intruders.items():
        for method, url in [("get", f"/attempts/{a}"), ("get", f"/attempts/{a}/q/1"), ("get", f"/attempts/{a}/result"),
                            ("post", f"/attempts/{a}/finish")]:
            r = getattr(client, method)(url)
            assert r.status_code == 404, f"{who} got {r.status_code} from {method.upper()} {url}"
        assert answer(client, a, 2, "A", "sure").status_code == 404, who

    # A missing attempt looks exactly the same as someone else's, so the id space can't be probed.
    missing = intruders["another student"].get("/attempts/999999/q/1")
    theirs = intruders["another student"].get(f"/attempts/{a}/q/1")
    assert (missing.status_code, missing.text) == (theirs.status_code, theirs.text)

    for url in (f"/attempts/{a}", f"/attempts/{a}/q/1", f"/attempts/{a}/result"):
        response = anon.get(url)
        assert response.status_code == 303 and response.headers["location"].startswith("/login")

    db.rollback()
    untouched = db.get(models.Attempt, a)
    assert untouched.status == AttemptStatus.IN_PROGRESS and untouched.responses[1].answered_at is None   # nothing changed

    # ...and the intruder's own lists never show it.
    assert f"/attempts/{a}" not in intruders["another student"].get("/practice").text
    assert f"/attempts/{a}" not in intruders["another student"].get("/").text


# --------------------------------------------------------------------------- students only ever see live questions

def test_unpublishing_mid_session_removes_the_questions_at_once(db, make_paper, make_user, admin):
    year = next(_year)
    paper = live_paper(db, make_paper, "Vanishing paper", n=2, year=year)
    student = make_user("vanishstudent")
    attempt = attempt_of(db, start(student, year, "2"))
    text_of_q = db.get(models.Question, attempt.responses[0].question_id).text
    assert text_of_q in student.get(f"/attempts/{attempt.id}/q/1").text

    admin.post(f"/papers/{paper.id}/unpublish")
    page = student.get(f"/attempts/{attempt.id}/q/1").text
    assert "no longer available" in page and text_of_q not in page and 'name="answer"' not in page
    answer(student, attempt.id, 1, "A", "sure")
    assert "no longer available" in student.get(f"/attempts/{attempt.id}/q/1").text
    db.rollback()
    assert db.get(models.Attempt, attempt.id).responses[0].answered_at is None            # nothing was recorded

    student.post(f"/attempts/{attempt.id}/finish")
    result = student.get(f"/attempts/{attempt.id}/result").text
    assert "no longer available" in result and text_of_q not in result

    admin.post(f"/papers/{paper.id}/publish")
    assert text_of_q in student.get(f"/attempts/{attempt.id}/q/1").text                   # back when republished


def test_a_quarantined_question_disappears_from_a_running_session(db, make_paper, make_user, admin):
    year = next(_year)
    paper = live_paper(db, make_paper, "Quarantine mid paper", n=1, year=year)
    student = make_user("quarantinestudent")
    attempt = attempt_of(db, start(student, year, "1"))
    q = questions_of(db, paper)[1]
    admin.post(f"/review/{paper.id}/question/{q.id}/quarantine", data={"reason": "Bad key"})
    assert "no longer available" in student.get(f"/attempts/{attempt.id}/q/1").text


# --------------------------------------------------------------------------- image-dependent questions and explanations

def test_image_dependent_questions_show_their_page_snapshot(db, make_paper, make_user):
    year = next(_year)
    paper = live_paper(db, make_paper, "Snapshot practice paper", n=1, year=year)
    folder = ingest.images_dir_for(paper.id)
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "q1.jpg"), "wb") as f:
        f.write(b"\xff\xd8\xff fake")
    q = questions_of(db, paper)[1]
    q.has_image, q.source_image_path = True, "q1.jpg"
    db.commit()

    student = make_user("snapshotstudent")
    attempt = attempt_of(db, start(student, year, "1"))
    page = student.get(f"/attempts/{attempt.id}/q/1").text
    assert f"/media/{paper.id}/q1.jpg" in page
    assert student.get(f"/media/{paper.id}/q1.jpg").status_code == 200


def test_explanations_carry_an_honest_label(db, make_paper, make_user):
    year = next(_year)
    paper = live_paper(db, make_paper, "Label paper", n=3, year=year)
    q = questions_of(db, paper)
    q[1].explanation_status = "verified"
    q[2].source = "ai_json"
    db.commit()
    student = make_user("labelstudent")
    attempt = attempt_of(db, start(student, year, "3"))
    labels = {}
    for pos, resp in enumerate(attempt.responses, start=1):
        answer(student, attempt.id, pos, "A", "sure")
        number = db.get(models.Question, resp.question_id).question_number
        page = student.get(f"/attempts/{attempt.id}/q/{pos}").text
        labels[number] = re.search(r"Explanation · ([^<]+)<", page).group(1).strip()
    assert labels == {1: "Verified", 2: "AI-supplied, unverified", 3: "Unverified (as printed in the answer PDF)"}


def test_the_admin_can_mark_an_explanation_verified_from_the_review_form(admin, db, make_paper):
    paper = make_paper("Verify explanation paper", n=1)
    q = questions_of(db, paper)[1]
    q.explanation = "Some explanation."
    db.commit()
    url = f"/review/{paper.id}/question/{q.id}"

    admin.post(url, data=question_form(q))                                                # untouched: not counted as an edit
    db.rollback()
    assert not db.query(models.QuestionVersion).filter_by(question_id=q.id).count()
    assert questions_of(db, paper)[1].explanation_status is None

    admin.post(url, data=question_form(q, explanation_verified="true"))
    assert questions_of(db, paper)[1].explanation_status == "verified"
    assert db.query(models.QuestionVersion).filter_by(question_id=q.id).count() == 1

    admin.post(url, data=question_form(q))                                                # box left unticked
    assert questions_of(db, paper)[1].explanation_status == "unverified"


# --------------------------------------------------------------------------- pages

def test_practice_pages_are_mobile_friendly(db, make_paper, make_user):
    year = next(_year)
    live_paper(db, make_paper, "Mobile paper", n=2, year=year)
    student = make_user("mobilestudent")
    assert 'name="viewport"' in student.get("/practice").text
    attempt = attempt_of(db, start(student, year, "2"))
    page = student.get(f"/attempts/{attempt.id}/q/1").text
    assert 'name="viewport"' in page and '<details class="palette">' in page             # collapsible question palette
    css_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app", "static", "style.css")
    css = open(css_path, encoding="utf-8").read()
    assert "min-height: 52px" in css and "@media (max-width: 600px)" in css               # big tap targets, phone layout


def test_the_practice_page_works_for_a_student_with_no_history(make_user):
    page = make_user("freshstudent").get("/practice")
    assert page.status_code == 200 and "Topic practice" in page.text
    assert "Pick up where you left off" not in page.text and "Recent sessions" not in page.text
