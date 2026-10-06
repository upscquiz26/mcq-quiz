"""
Student-side Stage 5: revision — the spaced-repetition schedule, the mistake notebook, mistake practice,
bookmarks and private notes, and who is allowed to look up what.

The schedule tests are table-driven: each row is (day, right?, how sure) -> what should happen, with the due date
worked out by hand. Days are counted from day 0, the day of the first mistake.  The gaps are 1, 3, 7, then 15 days.
"""
import html
import re
from datetime import date, datetime, timedelta

import pytest

from app import auth, models
from app.models import AttemptKind, AttemptStatus, Confidence
from app.practice import pool, revision
from conftest import _client, _login
from test_practice_stage2 import _year, answer, attempt_of, correct_letter, live_paper, questions_of, start, user_id, wrong_letter
from test_timed_stage3 import get_attempt, letter_for, save, start_full, timed_paper, wrong_for

D = date(2030, 1, 1)                       # "day 0" for the schedule tables
S, G, N, U = Confidence.sure, Confidence.guessed, Confidence.no_idea, Confidence.skipped   # skipped = "not rated"


def page(client, url):
    """The page as plain text: tags removed, entities decoded, whitespace collapsed."""
    text = re.sub(r"<[^>]+>", " ", client.get(url).text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def new_student(db, name):
    if not db.query(models.User).filter_by(username=name).first():
        db.add(models.User(username=name, password_hash=auth.hash_password("studentpass1"),
                           is_admin=False, status=models.UserStatus.approved))
        db.commit()
    return user_id(db, name)


def one_question(db, make_paper, title):
    paper = live_paper(db, make_paper, title, n=1)
    return questions_of(db, paper)[1].id


def item_of(db, uid, qid):
    """The schedule item. NOTE: this rolls back first (to see other sessions' commits), so to change several items,
    fetch them all BEFORE modifying any — see items_of()."""
    db.rollback()
    return db.query(models.RevisionItem).filter_by(user_id=uid, question_id=qid).one_or_none()


def items_of(db, uid, qids):
    """Several schedule items at once, safe to modify together and then commit."""
    db.rollback()
    found = {i.question_id: i for i in db.query(models.RevisionItem)
             .filter(models.RevisionItem.user_id == uid, models.RevisionItem.question_id.in_(list(qids))).all()}
    return [found[q] for q in qids]


def by_number(db, attempt, number):
    """The position of question `number` in the attempt."""
    db.rollback()
    for r in db.get(models.Attempt, attempt.id).responses:
        if db.get(models.Question, r.question_id).question_number == number:
            return r.position
    raise AssertionError(f"question {number} is not in the attempt")


def practise(db, client, attempt, number, right, confidence="sure"):
    pos = by_number(db, attempt, number)
    letter = correct_letter(db, attempt, pos) if right else wrong_letter(db, attempt, pos)
    return answer(client, attempt.id, pos, letter, confidence)


# --------------------------------------------------------------------------- the schedule (hand-worked tables)
#  row: (day, correct, confidence, result, status, stage, streak, due_day)

SEQUENCES = {
    "gets it right twice and leaves": [
        (0, False, S, "created", "active", 0, 0, 1),
        (0, True, S, "ignored", "active", 0, 0, 1),            # the same day: not due, so it changes nothing
        (1, True, S, "reviewed", "active", 1, 1, 4),           # due on day 1; next gap is 3 days -> day 4
        (4, True, S, "mastered", "done", 1, 2, 4),             # two clean rights in a row: gone
    ],
    "struggles and climbs the whole ladder 1, 3, 7, 15": [
        (0, False, S, "created", "active", 0, 0, 1),
        (1, False, S, "reviewed", "active", 1, 0, 4),          # a wrong review breaks the streak but the ladder moves: +3
        (4, True, S, "reviewed", "active", 2, 1, 11),          # +7
        (11, False, S, "reviewed", "active", 3, 0, 26),        # +15
        (26, True, S, "reviewed", "active", 4, 1, 41),         # the gap stays at 15 from here on
        (41, True, S, "mastered", "done", 4, 2, 41),
    ],
    "extra practice never speeds it up, but a wrong answer breaks the streak": [
        (0, False, S, "created", "active", 0, 0, 1),
        (1, True, S, "reviewed", "active", 1, 1, 4),
        (2, False, S, "streak_broken", "active", 1, 0, 4),     # wrong before it was due: streak reset, schedule untouched
        (3, True, S, "ignored", "active", 1, 0, 4),            # right before it was due: nothing
        (4, True, S, "reviewed", "active", 2, 1, 11),          # the streak is 1 again, not 2
    ],
    "a lucky guess does not count as knowing it": [
        (0, False, S, "created", "active", 0, 0, 1),
        (1, True, G, "reviewed", "active", 1, 0, 4),           # right, but guessed: treated like a miss (streak 0)
        (4, True, N, "reviewed", "active", 2, 0, 11),          # right, "no idea": same
        (11, True, U, "reviewed", "active", 3, 1, 26),         # right and NOT RATED counts as clean
        (26, True, S, "mastered", "done", 3, 2, 26),
    ],
    "a late review rolls over and counts the next gap from the day it happened": [
        (0, False, S, "created", "active", 0, 0, 1),
        (10, True, S, "reviewed", "active", 1, 1, 13),         # due on day 1, done on day 10: next gap 3 from day 10
    ],
    "forgetting a mastered question puts it back on the schedule": [
        (0, False, S, "created", "active", 0, 0, 1),
        (1, True, S, "reviewed", "active", 1, 1, 4),
        (4, True, S, "mastered", "done", 1, 2, 4),
        (10, True, S, "ignored", "done", 1, 2, 4),             # right answers don't touch a mastered question
        (10, False, S, "reset", "active", 0, 0, 11),           # wrong again: back to the start, due tomorrow
    ],
}


@pytest.mark.parametrize("name", list(SEQUENCES))
def test_the_review_schedule(db, make_paper, name):
    uid = new_student(db, f"schedule{abs(hash(name)) % 10**6}")
    qid = one_question(db, make_paper, f"Schedule paper {name}")
    for day, correct, confidence, result, status, stage, streak, due_day in SEQUENCES[name]:
        got = revision.record_answer(db, uid, qid, correct=correct, confidence=confidence, on_date=D + timedelta(days=day))
        db.commit()
        item = item_of(db, uid, qid)
        where = f"day {day}, {'right' if correct else 'wrong'} ({confidence.name})"
        assert got == result, where
        assert (item.status, item.stage, item.correct_streak, item.due_date) == (status, stage, streak, D + timedelta(days=due_day)), where
    if status == "done":
        assert item_of(db, uid, qid).done_at is not None
    else:
        assert item_of(db, uid, qid).done_at is None


def test_right_answers_never_start_a_schedule(db, make_paper):
    uid = new_student(db, "neverschedulestudent")
    qid = one_question(db, make_paper, "Never schedule paper")
    assert revision.record_answer(db, uid, qid, correct=True, confidence=S, on_date=D) == "ignored"
    assert revision.record_answer(db, uid, qid, correct=True, confidence=G, on_date=D) == "ignored"
    db.commit()
    assert item_of(db, uid, qid) is None


def test_schedules_belong_to_one_student_each(db, make_paper):
    a, b = new_student(db, "schedulea"), new_student(db, "scheduleb")
    qid = one_question(db, make_paper, "Two students paper")
    revision.record_answer(db, a, qid, correct=False, confidence=S, on_date=D)
    db.commit()
    assert item_of(db, a, qid) is not None and item_of(db, b, qid) is None
    revision.record_answer(db, b, qid, correct=True, confidence=S, on_date=D + timedelta(days=1))
    db.commit()
    assert item_of(db, b, qid) is None and item_of(db, a, qid).correct_streak == 0     # b's right answer didn't touch a's item


def test_due_now_means_due_today_or_earlier(db, make_paper):
    uid = new_student(db, "duecountstudent")
    paper = live_paper(db, make_paper, "Due count paper", n=3)
    q = questions_of(db, paper)
    today = revision.today()
    for number, offset in ((1, -3), (2, 0), (3, 1)):                                     # overdue, due today, due tomorrow
        db.add(models.RevisionItem(user_id=uid, question_id=q[number].id, stage=0, correct_streak=0,
                                   status="active", due_date=today + timedelta(days=offset)))
    db.commit()
    assert revision.due_count(db, uid, today) == 2
    assert revision.due_count(db, uid, today + timedelta(days=1)) == 3
    assert revision.due_count(db, uid + 999, today) == 0


# --------------------------------------------------------------------------- answers feed the schedule from every mode

def test_a_wrong_answer_in_practice_is_scheduled_for_tomorrow_and_a_right_one_is_not(db, make_paper, make_user):
    year = next(_year)
    paper = live_paper(db, make_paper, "Practice feeds schedule paper", n=3, year=year)
    student = make_user("practicefeedstudent")
    uid = user_id(db, "practicefeedstudent")
    attempt = attempt_of(db, start(student, year, "3"))
    practise(db, student, attempt, 1, right=True)
    practise(db, student, attempt, 2, right=False, confidence="guessed")
    q = {n: questions_of(db, paper)[n].id for n in (1, 2, 3)}

    assert item_of(db, uid, q[1]) is None and item_of(db, uid, q[3]) is None
    wrong = item_of(db, uid, q[2])
    assert (wrong.status, wrong.stage, wrong.correct_streak) == ("active", 0, 0)
    assert wrong.due_date == revision.today() + timedelta(days=1)


def test_a_tests_answers_reach_the_schedule_when_it_is_finished_not_before(db, make_paper, make_user):
    paper = timed_paper(db, make_paper, "Test feeds schedule paper", n=3)
    student = make_user("testfeedstudent")
    uid = user_id(db, "testfeedstudent")
    attempt = attempt_of(db, start_full(student, paper))
    save(student, attempt.id, 1, answer=letter_for(1), confidence="sure")
    save(student, attempt.id, 2, answer=wrong_for(2), confidence="sure")
    save(student, attempt.id, 3, answer=wrong_for(3), confidence="guessed")
    q = {n: questions_of(db, paper)[n].id for n in (1, 2, 3)}

    assert all(item_of(db, uid, q[n]) is None for n in (1, 2, 3))                       # nothing until the test is over
    student.post(f"/attempts/{attempt.id}/finish")
    assert item_of(db, uid, q[1]) is None
    for n in (2, 3):
        assert item_of(db, uid, q[n]).due_date == revision.today() + timedelta(days=1)


def test_a_test_that_ran_out_of_time_also_feeds_the_schedule(db, make_paper, make_user):
    from test_timed_stage3 import force_deadline
    paper = timed_paper(db, make_paper, "Expired feeds schedule paper", n=2)
    student = make_user("expiredfeedstudent")
    uid = user_id(db, "expiredfeedstudent")
    attempt = attempt_of(db, start_full(student, paper))
    save(student, attempt.id, 1, answer=wrong_for(1), confidence="sure")
    force_deadline(db, attempt.id, seconds_ago=60)
    student.get(f"/attempts/{attempt.id}/result")
    assert item_of(db, uid, questions_of(db, paper)[1].id) is not None


# --------------------------------------------------------------------------- the notebook

def test_the_notebook_collects_wrong_and_guessed_questions_with_their_history(db, make_paper, make_user):
    year = next(_year)
    paper = live_paper(db, make_paper, "Notebook paper", n=5, year=year)
    student = make_user("notebookstudent")
    uid = user_id(db, "notebookstudent")
    q = {n: questions_of(db, paper)[n].id for n in range(1, 6)}

    first = attempt_of(db, start(student, year, "5"))
    practise(db, student, first, 1, right=False, confidence="sure")
    practise(db, student, first, 2, right=True, confidence="guessed")       # right, but a guess
    practise(db, student, first, 3, right=True, confidence="sure")          # solid: stays out
    practise(db, student, first, 4, right=True, confidence="no_idea")       # right, but no idea
    student.post(f"/attempts/{first.id}/finish")
    second = attempt_of(db, start(student, year, "5"))
    practise(db, student, second, 1, right=False, confidence="guessed")     # wrong again
    student.post(f"/attempts/{second.id}/finish")

    entries = {e["question"].id: e for e in revision.notebook(db, uid)}
    assert set(entries) == {q[1], q[2], q[4]}                                # Q3 (sure and right) and Q5 (unseen) are absent
    assert (entries[q[1]]["wrong"], entries[q[1]]["guessed_right"], entries[q[1]]["state"]) == (2, 0, "scheduled")
    assert (entries[q[2]]["wrong"], entries[q[2]]["guessed_right"], entries[q[2]]["state"]) == (0, 1, "guessed")
    assert (entries[q[4]]["wrong"], entries[q[4]]["guessed_right"], entries[q[4]]["state"]) == (0, 1, "guessed")
    assert entries[q[1]]["reason"] == models.MistakeReason.guess_miss       # the LATEST wrong answer was a guess

    text = page(student, "/revision")
    assert "wrong ×2" in text and "guessed right ×1" in text and "Revision" in text and "Guessed only (2)" in text


def test_the_notebook_shows_the_students_own_chosen_reason(db, make_paper, make_user):
    year = next(_year)
    live_paper(db, make_paper, "Reason notebook paper", n=1, year=year)
    student = make_user("reasonnotebookstudent")
    attempt = attempt_of(db, start(student, year, "1"))
    practise(db, student, attempt, 1, right=False, confidence="sure")
    student.post(f"/attempts/{attempt.id}/finish")
    assert "Misconception / overconfidence" in page(student, "/revision")
    student.post(f"/attempts/{attempt.id}/q/1/reason", data={"reason": "careless"})
    assert "Careless mistake" in page(student, "/revision")


def test_notebook_order_is_due_then_scheduled_then_guessed_then_mastered(db, make_paper, make_user):
    year = next(_year)
    paper = live_paper(db, make_paper, "Order paper", n=4, year=year)
    student = make_user("orderstudent")
    uid = user_id(db, "orderstudent")
    attempt = attempt_of(db, start(student, year, "4"))
    practise(db, student, attempt, 1, right=False)      # will be mastered
    practise(db, student, attempt, 2, right=False)      # will be scheduled
    practise(db, student, attempt, 3, right=False)      # will be due
    practise(db, student, attempt, 4, right=True, confidence="guessed")
    q = {n: questions_of(db, paper)[n].id for n in range(1, 5)}
    today = revision.today()
    mastered, scheduled, due = items_of(db, uid, [q[1], q[2], q[3]])
    mastered.status = "done"
    scheduled.due_date = today + timedelta(days=5)
    due.due_date = today - timedelta(days=2)
    db.commit()

    states = [(e["question"].question_number, e["state"]) for e in revision.notebook(db, uid)]
    assert states == [(3, "due"), (2, "scheduled"), (4, "guessed"), (1, "mastered")]
    entry = {e["question"].question_number: e for e in revision.notebook(db, uid)}
    assert entry[3]["days_until"] == -2 and entry[2]["days_until"] == 5
    text = page(student, "/revision")
    assert "Overdue 2d" in text and "In 5 days" in text and "Mastered" in text


def test_notebook_filters_by_subject_topic_reason_and_state(db, make_paper, make_user):
    year = next(_year)
    paper = live_paper(db, make_paper, "Filter notebook paper", n=4, year=year)
    history = db.query(models.Subject).filter_by(name="History").one()
    polity = db.query(models.Subject).filter_by(name="Polity").one()
    topic = models.Topic(name="Notebook topic", subject_id=polity.id)
    db.add(topic)
    db.commit()
    q = questions_of(db, paper)
    q[1].subject_id = q[2].subject_id = history.id
    q[3].subject_id, q[3].topic_id = polity.id, topic.id
    q[4].subject_id = polity.id
    db.commit()

    student = make_user("filternotebookstudent")
    uid = user_id(db, "filternotebookstudent")
    attempt = attempt_of(db, start(student, year, "4"))
    practise(db, student, attempt, 1, right=False, confidence="no_idea")     # History, concept gap
    practise(db, student, attempt, 2, right=False, confidence="guessed")     # History, guess
    practise(db, student, attempt, 3, right=False, confidence="no_idea")     # Polity + topic, concept gap
    practise(db, student, attempt, 4, right=True, confidence="guessed")      # Polity, guessed right
    student.post(f"/attempts/{attempt.id}/finish")

    def numbers(**kw):
        return sorted(e["question"].question_number for e in revision.notebook(db, uid, **kw))

    assert numbers() == [1, 2, 3, 4]
    assert numbers(subject_id=history.id) == [1, 2]
    assert numbers(subject_id=polity.id) == [3, 4]
    assert numbers(topic_id=topic.id) == [3]
    assert numbers(reason="knowledge_gap") == [1, 3] and numbers(reason="guess_miss") == [2]
    assert numbers(state="guessed") == [4] and numbers(state="revise") == [1, 2, 3]
    assert numbers(subject_id=history.id, reason="knowledge_gap") == [1]

    listed = lambda url: sorted(int(n) for n in re.findall(r"/questions/(\d+)\"", student.get(url).text))
    ids = {n: q[n].id for n in q}
    assert listed(f"/revision?subject_id={history.id}") == sorted([ids[1], ids[2]])
    assert listed("/revision?reason=knowledge_gap") == sorted([ids[1], ids[3]])
    assert listed("/revision?state=nonsense&subject_id=abc") == sorted(ids.values())        # bad filters are ignored, not fatal


def test_the_notebook_is_private_and_only_shows_live_questions(db, make_paper, make_user, admin):
    year = next(_year)
    paper = live_paper(db, make_paper, "Private notebook paper", n=1, year=year)
    owner, other = make_user("notebookowner"), make_user("notebookother")
    attempt = attempt_of(db, start(owner, year, "1"))
    practise(db, owner, attempt, 1, right=False)
    owner.post(f"/attempts/{attempt.id}/finish")
    qid = questions_of(db, paper)[1].id

    assert f"/questions/{qid}" in owner.get("/revision").text
    assert f"/questions/{qid}" not in other.get("/revision").text                          # someone else's mistakes
    assert "Nothing here yet" in page(other, "/revision")

    admin.post(f"/papers/{paper.id}/unpublish")
    assert f"/questions/{qid}" not in owner.get("/revision").text                          # no longer live: gone
    assert revision.due_count(db, user_id(db, "notebookowner")) == 0


# --------------------------------------------------------------------------- mistake practice

def _mistakes(db, make_paper, make_user, username, n=4, wrong=(1, 2, 3)):
    """A finished practice session with the given questions wrong. Returns (student, uid, {number: question_id})."""
    year = next(_year)
    paper = live_paper(db, make_paper, f"Mistakes paper {username}", n=n, year=year)
    student = make_user(username)
    attempt = attempt_of(db, start(student, year, str(n)))
    for number in range(1, n + 1):
        practise(db, student, attempt, number, right=number not in wrong)
    student.post(f"/attempts/{attempt.id}/finish")
    return student, user_id(db, username), {i: questions_of(db, paper)[i].id for i in range(1, n + 1)}


def test_due_revision_includes_all_due_questions_without_a_count_parameter(db, make_paper, make_user):
    student, uid, qids = _mistakes(db, make_paper, make_user, "all_duerevision", n=12, wrong=tuple(range(1, 13)))
    for item in items_of(db, uid, list(qids.values())):
        item.due_date = revision.today()
    db.commit()

    response = student.post("/revision/start", data={"mode": "due"})
    attempt = attempt_of(db, response)
    assert len(attempt.responses) == 12


def test_revising_what_is_due_takes_the_most_overdue_first_and_updates_the_schedule(db, make_paper, make_user):
    student, uid, q = _mistakes(db, make_paper, make_user, "revisedue")
    today = revision.today()
    for item, offset in zip(items_of(db, uid, [q[1], q[2], q[3]]), (-1, -5, 0)):     # Q2 is the most overdue
        item.due_date = today + timedelta(days=offset)
    db.commit()
    assert "3 due right now" in page(student, "/revision")

    r = student.post("/revision/start", data={"mode": "due"})
    attempt = attempt_of(db, r)
    assert attempt.kind == AttemptKind.MISTAKE and attempt.status == AttemptStatus.IN_PROGRESS
    order = [db.get(models.Question, x.question_id).question_number for x in attempt.responses]
    assert order == [2, 1, 3]                                                # most overdue first
    assert attempt.deadline_at is None and "Mistake practice" in page(student, f"/attempts/{attempt.id}/q/1")

    practise(db, student, attempt, 2, right=True, confidence="sure")         # a clean right at a review: streak 1, +3 days
    practise(db, student, attempt, 1, right=False, confidence="sure")        # a wrong review: streak stays 0, ladder moves
    two, one = item_of(db, uid, q[2]), item_of(db, uid, q[1])
    assert (two.stage, two.correct_streak, two.due_date) == (1, 1, today + timedelta(days=3))
    assert (one.stage, one.correct_streak, one.due_date) == (1, 0, today + timedelta(days=3))
    student.post(f"/attempts/{attempt.id}/finish")
    assert revision.due_count(db, uid) == 1                                   # only Q3, which wasn't answered, is still due
    assert "Mistake practice" in page(student, "/practice")                   # it shows up in the session lists too


def test_two_clean_reviews_in_a_row_master_a_question(db, make_paper, make_user):
    student, uid, q = _mistakes(db, make_paper, make_user, "masterstudent", n=2, wrong=(1,))
    today = revision.today()

    def review(right):
        item = item_of(db, uid, q[1])
        item.due_date = today                                                # bring it due again
        db.commit()
        attempt = attempt_of(db, student.post("/revision/start", data={"mode": "due", "count": "5"}))
        practise(db, student, attempt, 1, right=right, confidence="sure")
        student.post(f"/attempts/{attempt.id}/finish")

    review(True)
    assert item_of(db, uid, q[1]).correct_streak == 1 and item_of(db, uid, q[1]).status == "active"
    review(True)
    assert item_of(db, uid, q[1]).status == "done"
    assert [e["state"] for e in revision.notebook(db, uid)] == ["mastered"]
    assert revision.due_count(db, uid) == 0

    # Mastered questions are left out of mistake practice unless asked for, and are never "due".
    assert student.post("/revision/start", data={"mode": "due"}).headers["location"] == "/revision"
    assert "Nothing is due" in page(student, "/revision")
    assert student.post("/revision/start", data={"mode": "all"}).headers["location"] == "/revision"
    assert "No mistakes match" in page(student, "/revision")
    included = student.post("/revision/start", data={"mode": "all", "include_mastered": "1", "count": "5"})
    assert attempt_of(db, included).total_questions == 1


def test_mistake_practice_uses_only_the_students_own_live_mistakes_and_filters(db, make_paper, make_user, admin):
    student, uid, q = _mistakes(db, make_paper, make_user, "allmistakesstudent", n=5, wrong=(1, 2, 3))
    other, _, _ = _mistakes(db, make_paper, make_user, "someoneelsestudent", n=3, wrong=(1, 2))
    history = db.query(models.Subject).filter_by(name="History").one()
    polity = db.query(models.Subject).filter_by(name="Polity").one()
    qs = {n: db.get(models.Question, q[n]) for n in q}
    qs[1].subject_id = qs[2].subject_id = history.id
    qs[3].subject_id = polity.id
    db.commit()

    attempt = attempt_of(db, student.post("/revision/start", data={"mode": "all", "count": "10"}))
    assert {db.get(models.Question, x.question_id).id for x in attempt.responses} == {q[1], q[2], q[3]}   # not Q4/Q5, not theirs
    assert attempt.kind == AttemptKind.MISTAKE and attempt.paper_id is not None

    only_history = attempt_of(db, student.post("/revision/start", data={"mode": "all", "subject_id": str(history.id), "count": "10"}))
    assert {x.question_id for x in only_history.responses} == {q[1], q[2]}
    capped = attempt_of(db, student.post("/revision/start", data={"mode": "all", "count": "2"}))
    assert capped.total_questions == 2

    paper_id = db.get(models.Question, q[1]).paper_id                                # unpublishing removes them from the pool
    admin.post(f"/papers/{paper_id}/unpublish")
    assert student.post("/revision/start", data={"mode": "all"}).headers["location"] == "/revision"
    assert "No mistakes match" in page(student, "/revision")


@pytest.mark.parametrize("data,fragment", [
    ({"mode": "all", "count": "abc"}, "valid options"), ({"mode": "all", "count": "0"}, "at least 1"),
    ({"mode": "all", "subject_id": "x"}, "valid options"), ({"mode": "all", "reason": "unset"}, "valid options"),
    ({"mode": "all", "reason": "bogus"}, "valid options"), ({"mode": "all", "topic_id": "1.5"}, "valid options"),
])
def test_bad_mistake_practice_requests_are_refused(db, make_user, data, fragment):
    student = make_user("badrevisionstudent")
    before = db.query(models.Attempt).filter_by(user_id=user_id(db, "badrevisionstudent")).count()
    r = student.post("/revision/start", data=data)
    assert r.status_code == 303 and r.headers["location"] == "/revision"
    assert fragment in page(student, "/revision")
    db.rollback()
    assert db.query(models.Attempt).filter_by(user_id=user_id(db, "badrevisionstudent")).count() == before


def test_the_home_page_and_revision_page_show_what_is_due(db, make_paper, make_user):
    student, uid, q = _mistakes(db, make_paper, make_user, "homeduestudent", n=2, wrong=(1,))
    home_before_due = student.get("/").text
    assert "Fix your mistakes" in home_before_due
    assert "Nothing due right now" in home_before_due                          # due tomorrow, not today
    item_of(db, uid, q[1]).due_date = revision.today()
    db.commit()
    home = page(student, "/")
    assert "Fix your mistakes" in home and "1 due right now" in home and "Start revision" in home
    revision_page = page(student, "/revision")
    assert "1 due right now" in revision_page and "Start revision" in revision_page
    assert "Misconception / overconfidence (1)" in revision_page
    revision_html = student.get("/revision").text
    due_form = re.search(r'<form method="post" action="/revision/start">.*?</form>', revision_html, re.S)
    assert due_form and 'name="count"' not in due_form.group(0)


def test_a_student_with_no_mistakes_gets_a_friendly_page(make_user):
    text = page(make_user("fresherstudent"), "/revision")
    assert "Nothing here yet" in text and "after 1, 3, 7 and then 15 days" in text


# --------------------------------------------------------------------------- bookmarks and notes

def _answered_question(db, make_paper, make_user, username):
    year = next(_year)
    paper = live_paper(db, make_paper, f"Keep paper {username}", n=1, year=year)
    student = make_user(username)
    attempt = attempt_of(db, start(student, year, "1"))
    practise(db, student, attempt, 1, right=False)
    return student, user_id(db, username), questions_of(db, paper)[1].id, attempt


def test_bookmarking_a_question_and_removing_the_bookmark(db, make_paper, make_user):
    student, uid, qid, attempt = _answered_question(db, make_paper, make_user, "bookmarkstudent")
    review = f"/attempts/{attempt.id}/q/1"
    assert "Bookmark this question" in page(student, review)

    r = student.post(f"/questions/{qid}/bookmark", data={"on": "1", "next": review})
    assert r.status_code == 303 and r.headers["location"] == review
    db.rollback()
    assert db.query(models.QuestionBookmark).filter_by(user_id=uid, question_id=qid).count() == 1
    assert "Bookmarked — remove" in page(student, review)
    student.post(f"/questions/{qid}/bookmark", data={"on": "1"})                        # doing it twice is harmless
    db.rollback()
    assert db.query(models.QuestionBookmark).filter_by(user_id=uid, question_id=qid).count() == 1

    assert f"/questions/{qid}" in student.get("/bookmarks").text
    student.post(f"/questions/{qid}/bookmark", data={"on": "0"})
    db.rollback()
    assert db.query(models.QuestionBookmark).filter_by(user_id=uid, question_id=qid).count() == 0
    assert "No bookmarks yet" in page(student, "/bookmarks")


def test_private_notes_are_saved_edited_and_removed(db, make_paper, make_user):
    student, uid, qid, attempt = _answered_question(db, make_paper, make_user, "notestudent")
    review = f"/attempts/{attempt.id}/q/1"
    student.post(f"/questions/{qid}/note", data={"text": "Article 32 vs 226:\r\nremember the writs", "next": review})
    db.rollback()
    saved = db.query(models.QuestionNote).filter_by(user_id=uid, question_id=qid).one()
    assert saved.text == "Article 32 vs 226:\nremember the writs"                       # Windows line breaks are tidied
    text = page(student, review)
    assert "Note saved." in text and "remember the writs" in text

    student.post(f"/questions/{qid}/note", data={"text": "  second version  "})
    db.rollback()
    assert db.query(models.QuestionNote).filter_by(user_id=uid, question_id=qid).one().text == "second version"

    student.post(f"/questions/{qid}/bookmark", data={"on": "1"})
    assert "second version" in page(student, "/bookmarks")                                # a snippet appears in the list
    student.post(f"/questions/{qid}/note", data={"text": "   "})                          # empty removes the note
    db.rollback()
    assert db.query(models.QuestionNote).filter_by(user_id=uid, question_id=qid).count() == 0


def test_a_note_cannot_be_longer_than_the_limit(db, make_paper, make_user):
    student, uid, qid, _ = _answered_question(db, make_paper, make_user, "longnotestudent")
    student.post(f"/questions/{qid}/note", data={"text": "x" * (revision.MAX_NOTE_LENGTH + 1)})
    assert "under 2000 characters" in page(student, f"/questions/{qid}")
    db.rollback()
    assert db.query(models.QuestionNote).filter_by(user_id=uid, question_id=qid).count() == 0
    student.post(f"/questions/{qid}/note", data={"text": "y" * revision.MAX_NOTE_LENGTH})
    db.rollback()
    assert db.query(models.QuestionNote).filter_by(user_id=uid, question_id=qid).count() == 1


def test_bookmark_and_note_redirects_stay_on_this_site(db, make_paper, make_user):
    student, _, qid, _ = _answered_question(db, make_paper, make_user, "safenextstudent")
    for unsafe in ("//evil.example/x", "https://evil.example", "\\\\evil"):
        r = student.post(f"/questions/{qid}/bookmark", data={"on": "1", "next": unsafe})
        assert r.headers["location"] == "/"
        r = student.post(f"/questions/{qid}/note", data={"text": "hi", "next": unsafe})
        assert r.headers["location"] == "/"


def test_you_can_only_keep_questions_you_have_met_and_that_are_live(db, make_paper, make_user, admin):
    year = next(_year)
    paper = live_paper(db, make_paper, "Unmet paper", n=2, year=year)
    student, stranger = make_user("unmetstudent"), make_user("unmetstranger")
    attempt = attempt_of(db, start(student, year, "1"))                                # only ONE of the two questions is drawn
    seen = db.get(models.Question, db.get(models.Attempt, attempt.id).responses[0].question_id)
    student.get(f"/attempts/{attempt.id}/q/1")
    unseen = next(q for q in questions_of(db, paper).values() if q.id != seen.id)

    assert student.post(f"/questions/{unseen.id}/bookmark", data={"on": "1"}).status_code == 404
    assert student.post(f"/questions/{unseen.id}/note", data={"text": "peek"}).status_code == 404
    assert student.get(f"/questions/{unseen.id}").status_code == 404
    assert student.post("/questions/999999/bookmark", data={"on": "1"}).status_code == 404
    assert stranger.post(f"/questions/{seen.id}/bookmark", data={"on": "1"}).status_code == 404   # never met it

    assert student.post(f"/questions/{seen.id}/bookmark", data={"on": "1"}).status_code == 303
    admin.post(f"/papers/{paper.id}/unpublish")                                              # not live any more
    assert student.post(f"/questions/{seen.id}/bookmark", data={"on": "0"}).status_code == 404
    assert student.get(f"/questions/{seen.id}").status_code == 404
    assert f'href="/questions/{seen.id}"' not in student.get("/bookmarks").text


def test_bookmarks_and_notes_are_private(db, make_paper, make_user):
    student, uid, qid, attempt = _answered_question(db, make_paper, make_user, "privatekeepstudent")
    student.post(f"/questions/{qid}/bookmark", data={"on": "1"})
    student.post(f"/questions/{qid}/note", data={"text": "my secret note"})

    # A second student who has ALSO met the question sees an empty bookmark list and no note.
    year = db.get(models.Question, qid).paper.year
    other = make_user("privatekeepother")
    attempt2 = attempt_of(db, start(other, year, "1"))
    practise(db, other, attempt2, 1, right=False)
    assert "my secret note" not in page(other, f"/questions/{qid}")
    assert "my secret note" not in page(other, f"/attempts/{attempt2.id}/q/1")
    assert "No bookmarks yet" in page(other, "/bookmarks") and f"/questions/{qid}" not in other.get("/bookmarks").text
    assert "☆ Bookmark this question" in page(other, f"/questions/{qid}")                   # not shown as bookmarked to them


# --------------------------------------------------------------------------- what a question's own page reveals

def test_a_questions_own_page_shows_the_answer_history_and_schedule(db, make_paper, make_user):
    student, uid, qid, attempt = _answered_question(db, make_paper, make_user, "questionpagestudent")
    student.post(f"/attempts/{attempt.id}/finish")
    text = page(student, f"/questions/{qid}")
    assert "Correct answer" in text and "Explanation · Unverified (as printed in the answer PDF)" in text
    assert "Revision schedule" in text and "Next review in 1 day" in text and "0 of 2 clean right answers" in text
    assert "Your answers" in text and "Topic practice" in text and "you chose" in text

    item_of(db, uid, qid).due_date = revision.today()
    db.commit()
    assert "Due now" in page(student, f"/questions/{qid}")
    item_of(db, uid, qid).status = "done"
    db.commit()
    assert "Mastered" in page(student, f"/questions/{qid}") and "left your schedule" in page(student, f"/questions/{qid}")


def test_the_answer_is_hidden_until_you_have_answered_or_finished(db, make_paper, make_user):
    year = next(_year)
    live_paper(db, make_paper, "Hidden until paper", n=1, year=year)
    student = make_user("hiddenuntilstudent")
    attempt = attempt_of(db, start(student, year, "1"))
    student.get(f"/attempts/{attempt.id}/q/1")                                          # seen, not answered
    qid = db.get(models.Attempt, attempt.id).responses[0].question_id
    text = page(student, f"/questions/{qid}")
    assert "Correct answer" not in text and "The answer will show here" in text

    practise(db, student, attempt, 1, right=True)
    assert "Correct answer" in page(student, f"/questions/{qid}")                        # answered in practice: revealed


def test_a_question_in_a_test_you_are_still_sitting_cannot_be_used_to_peek(db, make_paper, make_user):
    """The student got this question wrong in practice (so has met it), then starts a timed test containing it.
       While that test runs, its own page must not reveal the answer."""
    paper = timed_paper(db, make_paper, "Peek paper", n=2)
    student = make_user("peekstudent")
    year = paper.year
    practice = attempt_of(db, start(student, year, "2"))
    practise(db, student, practice, 1, right=False)
    qid = questions_of(db, paper)[1].id
    assert "Correct answer" in page(student, f"/questions/{qid}")                        # fine before the test

    test = attempt_of(db, start_full(student, paper))
    text = page(student, f"/questions/{qid}")
    assert "Correct answer" not in text and "still sitting" in text
    assert "Explanation ·" not in text and "Your answers" not in text                    # nothing that could hint at it

    student.post(f"/attempts/{test.id}/finish")
    assert "Correct answer" in page(student, f"/questions/{qid}")                        # fine again afterwards


def test_a_test_question_you_only_looked_at_is_locked_but_can_be_bookmarked(db, make_paper, make_user):
    paper = timed_paper(db, make_paper, "Locked look paper", n=2)
    student = make_user("lockedlookstudent")
    test = attempt_of(db, start_full(student, paper))
    student.get(f"/attempts/{test.id}/q/1")
    qid = questions_of(db, paper)[1].id
    text = page(student, f"/questions/{qid}")
    assert "still sitting" in text and "Correct answer" not in text
    assert student.post(f"/questions/{qid}/bookmark", data={"on": "1"}).status_code == 303   # bookmarking leaks nothing


def test_the_test_screen_itself_has_no_bookmark_or_note_controls(db, make_paper, make_user):
    paper = timed_paper(db, make_paper, "No keep in test paper", n=1)
    student = make_user("nokeepstudent")
    test = attempt_of(db, start_full(student, paper))
    text = page(student, f"/attempts/{test.id}/q/1")
    assert "Bookmark this question" not in text and "private note" not in text


def test_a_finished_tests_review_page_offers_bookmarks_and_notes(db, make_paper, make_user):
    paper = timed_paper(db, make_paper, "Review keep paper", n=2)
    student = make_user("reviewkeepstudent")
    test = attempt_of(db, start_full(student, paper))
    save(student, test.id, 1, answer=wrong_for(1), confidence="sure")
    student.post(f"/attempts/{test.id}/finish")
    text = page(student, f"/attempts/{test.id}/q/1")
    assert "Bookmark this question" in text and "Your private note" in text
    assert "Bookmark this question" in page(student, f"/attempts/{test.id}/q/2")           # even the skipped one
