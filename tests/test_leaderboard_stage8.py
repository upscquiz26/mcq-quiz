"""
Student stage 8: leaderboards.

Ranking is tested on attempts written straight into the database (so scores, times and dates are exact); the
"only the first attempt counts" and "a lapsed test is still ranked" rules are tested through the real test engine.
The weekly board is global, so its ranking tests sit in a fixed week years ago that no other test writes to.
"""
import re
from datetime import datetime, timedelta

import pytest

from app import auth, models, settings
from app.models import AttemptKind, AttemptStatus
from app.practice import leaderboard as lb
from app.routes.leaderboard import rank_key_for
from conftest import _client, _login
from test_practice_stage2 import attempt_of, user_id
from test_revision_stage5 import page
from test_timed_stage3 import force_deadline, get_attempt, letter_for, save, start_full, timed_paper

PASSWORD = "studentpass1"
WEEK = datetime(2001, 3, 7, 12, 0)                 # a Wednesday; its week is Mon 5 Mar - Sun 11 Mar 2001
_names = iter(range(1, 10_000))


@pytest.fixture(autouse=True)
def leaderboard_on(db):
    def reset():
        db.rollback()
        settings.set_bool(db, "leaderboard_enabled", True)
        db.commit()
    reset()
    yield
    reset()


def person(db, label, show=True, status=models.UserStatus.approved, is_admin=False, display=None):
    """A user with a unique username; returns (username, user id). The display name defaults to the label."""
    username = f"lb{next(_names)}_{label.lower()}"          # usernames are case-insensitive at login
    db.add(models.User(username=username, display_name=display or f"{label}-{username}", is_admin=is_admin, status=status,
                       password_hash=auth.hash_password(PASSWORD), show_on_leaderboard=show))
    db.commit()
    return username, user_id(db, username)


def name_of(db, uid):
    db.rollback()
    return db.get(models.User, uid).display_name


def client_for(username):
    return _login(_client(), username, PASSWORD)


def ranked(db, uid, key, score, seconds, *, max_marks=20.0, counts=True, status=AttemptStatus.SUBMITTED, kind=AttemptKind.FULL,
           completed=None, paper_id=None):
    a = models.Attempt(user_id=uid, kind=kind, status=status, rank_key=key, counts_for_rank=counts, score=score,
                       max_marks=max_marks, time_taken_seconds=seconds, paper_id=paper_id, total_questions=10,
                       started_at=(completed or datetime.utcnow()) - timedelta(seconds=seconds or 0),
                       completed_at=completed or datetime.utcnow())
    db.add(a)
    db.commit()
    return a


@pytest.fixture()
def paper(db, make_paper):
    return timed_paper(db, make_paper, f"Board paper {next(_names)}", n=10)


def fresh_board(db, key, viewer_id):
    """The board, read with a session that hasn't cached attempts the test client has since changed."""
    db.expire_all()
    return lb.test_board(db, key, viewer_id)


def key_of(db, paper, subject_id=None):
    db.rollback()
    return rank_key_for(db, paper.id, subject_id)


# --------------------------------------------------------------------------- ranking

def test_higher_score_first_then_less_time_and_equal_means_shared_rank(db, paper):
    key = key_of(db, paper)
    people = {label: person(db, label)[1] for label in "ABCDE"}
    ranked(db, people["A"], key, 18, 900)
    ranked(db, people["B"], key, 20, 1500)          # best score wins even though slowest
    ranked(db, people["C"], key, 18, 800)           # same score as A, faster: ahead of A
    ranked(db, people["D"], key, 12, 600)
    ranked(db, people["E"], key, 12, 600)           # identical to D: shares rank 4
    board = lb.test_board(db, key, people["A"])
    order = [(e["rank"], e["name"].split("-")[0]) for e in board["top"]]
    assert order == [(1, "B"), (2, "C"), (3, "A"), (4, "D"), (4, "E")]
    assert board["participants"] == 5 and board["you"]["rank"] == 3


def test_the_board_lists_only_the_top_ten_plus_your_own_row(db, paper):
    key = key_of(db, paper)
    people = [person(db, f"P{i:02d}") for i in range(12)]
    for i, (_, uid) in enumerate(people):
        ranked(db, uid, key, 20 - i, 600)                   # P00 best ... P11 worst
    viewer_name, viewer_id = people[10]                     # rank 11
    board = lb.test_board(db, key, viewer_id)
    assert len(board["top"]) == 10 and board["you"]["rank"] == 11 and not board["you_in_top"]
    text = page(client_for(viewer_name), f"/leaderboard/paper/{paper.id}")
    assert name_of(db, people[0][1]) in text and name_of(db, people[9][1]) in text
    assert name_of(db, viewer_id) in text and "(you)" in text
    assert name_of(db, people[11][1]) not in text           # nobody below you (or below the cut-off) is ever listed
    assert "12 participants" in text and "you are #11" in text


def test_a_viewer_inside_the_top_ten_is_shown_once(db, paper):
    key = key_of(db, paper)
    people = [person(db, f"Q{i}") for i in range(3)]
    for i, (_, uid) in enumerate(people):
        ranked(db, uid, key, 20 - i, 600)
    text = page(client_for(people[1][0]), f"/leaderboard/paper/{paper.id}")
    table = text.split("← Leaderboard")[1]                  # below the header, which also shows the viewer's name
    assert table.count(name_of(db, people[1][1])) == 1 and table.count("(you)") == 1


def test_percentile_needs_five_participants(db, paper):
    key = key_of(db, paper)
    people = [person(db, f"R{i}")[1] for i in range(6)]
    for i, uid in enumerate(people[:4]):
        ranked(db, uid, key, 20 - i, 600)
    assert lb.test_board(db, key, people[1])["percentile"] is None          # 4 participants: too few
    ranked(db, people[4], key, 10, 600)
    ranked(db, people[5], key, 8, 600)
    board = lb.test_board(db, key, people[1])                               # rank 2 of 6: 4 behind, 5 others -> 80%
    assert board["you"]["rank"] == 2 and board["percentile"] == 80
    assert lb.test_board(db, key, people[0])["percentile"] == 100
    assert lb.test_board(db, key, people[5])["percentile"] == 0
    assert "ahead of 80% of the other participants" in page(client_for(name_from(db, people[1])), f"/leaderboard/paper/{paper.id}")


def name_from(db, uid):
    db.rollback()
    return db.get(models.User, uid).username


def test_someone_who_opted_out_is_anonymous_to_others_but_still_ranked_and_sees_themselves(db, paper):
    key = key_of(db, paper)
    hidden_name, hidden_id = person(db, "Hidden", show=False)
    other_name, other_id = person(db, "Visible")
    ranked(db, hidden_id, key, 20, 600)
    ranked(db, other_id, key, 10, 600)

    others_view = page(client_for(other_name), f"/leaderboard/paper/{paper.id}")
    assert "Anonymous" in others_view and name_of(db, hidden_id) not in others_view and hidden_name not in others_view
    assert "2 participants" in others_view and re.search(r"1 Anonymous", others_view)      # still holds rank 1

    own_view = page(client_for(hidden_name), f"/leaderboard/paper/{paper.id}")
    assert name_of(db, hidden_id) in own_view and "you are #1" in own_view
    assert "You've chosen not to be shown" in own_view and "Anonymous" in own_view


def test_only_counted_finished_students_attempts_are_on_the_board(db, paper):
    key = key_of(db, paper)
    good_name, good = person(db, "Good")
    ranked(db, good, key, 10, 600)
    retaker = person(db, "Retaker")[1]
    ranked(db, retaker, key, 20, 600, counts=False)                         # a retake never counts
    running = person(db, "Running")[1]
    ranked(db, running, key, 20, 600, status=AttemptStatus.IN_PROGRESS)     # not finished
    admin_id = person(db, "Adminish", is_admin=True)[1]
    ranked(db, admin_id, key, 20, 600)                                      # admins aren't students
    gone = person(db, "Gone", status=models.UserStatus.deactivated)[1]
    ranked(db, gone, key, 20, 600)                                          # deactivated accounts are removed from boards
    elsewhere = person(db, "Elsewhere")[1]
    ranked(db, elsewhere, "paper:0:full:deadbeef", 20, 600)                 # a different test
    unscored = person(db, "Unscored")[1]
    ranked(db, unscored, key, None, 600)
    board = lb.test_board(db, key, good)
    assert board["participants"] == 1 and board["top"][0]["name"] == name_of(db, good) and board["you"]["rank"] == 1


def test_a_board_with_nobody_says_so_and_a_missing_test_is_404(db, paper, make_paper):
    name, uid = person(db, "Lonely")
    student = client_for(name)
    text = page(student, f"/leaderboard/paper/{paper.id}")
    assert "Nobody has finished this test yet" in text and "You haven't taken this test yet" in text
    assert student.get("/leaderboard/paper/999999").status_code == 404
    assert student.get(f"/leaderboard/paper/{paper.id}/subject/999999").status_code == 404
    unpublished = make_paper("Not live board paper", n=3)
    assert student.get(f"/leaderboard/paper/{unpublished.id}").status_code == 404


def test_names_from_data_cannot_inject_markup(db, paper):
    key = key_of(db, paper)
    name, uid = person(db, "Evil", display="<script>alert(1)</script>")
    ranked(db, uid, key, 20, 600)
    raw = client_for(name).get(f"/leaderboard/paper/{paper.id}").text
    assert "<script>alert(1)" not in raw and "&lt;script&gt;alert(1)&lt;/script&gt;" in raw


# --------------------------------------------------------------------------- the real engine

def test_only_the_first_attempt_counts_so_retaking_cannot_improve_a_rank(db, paper, make_paper):
    key = key_of(db, paper)
    a_name, a_id = person(db, "First")
    b_name, b_id = person(db, "Rival")
    a, b = client_for(a_name), client_for(b_name)

    first = attempt_of(db, start_full(a, paper))
    for p in range(1, 4):                                                   # 3 right = 6 marks
        save(a, first.id, p, answer=letter_for(p), confidence="sure")
    assert a.post(f"/attempts/{first.id}/finish").status_code == 303
    second = attempt_of(db, start_full(b, paper))
    for p in range(1, 6):                                                   # 5 right = 10 marks
        save(b, second.id, p, answer=letter_for(p), confidence="sure")
    b.post(f"/attempts/{second.id}/finish")
    assert fresh_board(db, key, a_id)["you"]["rank"] == 2

    retake = attempt_of(db, start_full(a, paper))                           # a perfect retake
    for p in range(1, 11):
        save(a, retake.id, p, answer=letter_for(p), confidence="sure")
    a.post(f"/attempts/{retake.id}/finish")
    db.rollback()
    assert db.get(models.Attempt, first.id).counts_for_rank is True and db.get(models.Attempt, retake.id).counts_for_rank is False
    board = fresh_board(db, key, a_id)
    assert board["you"]["rank"] == 2 and board["you"]["score"] == pytest.approx(6.0)     # still the first attempt's 6 marks
    assert board["participants"] == 2

    result = page(a, f"/attempts/{retake.id}/result")
    assert "This wasn't your first attempt, so it isn't ranked" in result
    assert "you are #2 of 2" in page(a, f"/attempts/{first.id}/result")


def test_a_test_whose_clock_ran_out_is_ranked_on_what_was_saved(db, paper):
    key = key_of(db, paper)
    name, uid = person(db, "Lapsed")
    student = client_for(name)
    attempt = attempt_of(db, start_full(student, paper))
    for p in range(1, 5):
        save(student, attempt.id, p, answer=letter_for(p), confidence="sure")
    force_deadline(db, attempt.id, seconds_ago=600)                         # nobody opens it again
    board = fresh_board(db, key, uid)                                     # reading the board settles it
    assert board["you"]["rank"] == 1 and board["you"]["score"] == pytest.approx(8.0)
    assert get_attempt(db, attempt.id).status == AttemptStatus.EXPIRED


def test_a_sections_board_is_separate_from_the_full_tests(db, paper):
    history = db.query(models.Subject).filter_by(name="History").one().id
    full_key, section_key = key_of(db, paper), key_of(db, paper, history)
    assert full_key != section_key and section_key.startswith(f"paper:{paper.id}:subject:{history}:")
    name, uid = person(db, "Sect")
    ranked(db, uid, section_key, 12, 500, kind=AttemptKind.SECTIONAL, paper_id=paper.id)
    student = client_for(name)
    assert "1 participant" in page(student, f"/leaderboard/paper/{paper.id}/subject/{history}")
    assert "Nobody has finished this test yet" in page(student, f"/leaderboard/paper/{paper.id}")


# --------------------------------------------------------------------------- weekly

def test_week_bounds_start_on_monday():
    start, end = lb.week_bounds("this", WEEK)
    assert (start, end) == (datetime(2001, 3, 5), datetime(2001, 3, 12))
    assert lb.week_bounds("last", WEEK)[0] == datetime(2001, 2, 26)
    sunday_night = datetime(2001, 3, 11, 23, 59)
    assert lb.week_bounds("this", sunday_night)[0] == datetime(2001, 3, 5)
    assert lb.week_bounds("this", datetime(2001, 3, 12, 0, 0))[0] == datetime(2001, 3, 12)


def test_the_weekly_board_averages_percentages_and_breaks_ties_by_time(db, paper):
    key = key_of(db, paper)
    ann, bob, cy, dee = (person(db, n)[1] for n in ("Ann", "Bob", "Cy", "Dee"))
    when = WEEK
    # Ann: 90% and 70% -> 80%.  Bob: one test at 80% (fewer tests, same average) but faster.  Cy: 60%.
    ranked(db, ann, key + "a", 18, 1000, completed=when, kind=AttemptKind.FULL)
    ranked(db, ann, key + "b", 14, 1200, completed=when + timedelta(days=1), kind=AttemptKind.FULL)
    ranked(db, bob, key + "a", 16, 900, completed=when)
    ranked(db, cy, key + "a", 12, 300, completed=when)
    # Dee: all ineligible — a sectional test, a retake, a test outside the week, an unfinished one.
    ranked(db, dee, key + "s", 20, 300, completed=when, kind=AttemptKind.SECTIONAL)
    ranked(db, dee, key + "r", 20, 300, completed=when, counts=False)
    ranked(db, dee, key + "o", 20, 300, completed=when - timedelta(days=7))
    ranked(db, dee, key + "p", 20, 300, completed=when, status=AttemptStatus.IN_PROGRESS)
    board = lb.weekly_board(db, ann, "this", now=WEEK)
    rows = [(e["rank"], e["name"].split("-")[0], e["percent"], e["tests"]) for e in board["top"]]
    # Bob and Ann both average 80%; Bob's average time (900 s) beats Ann's (1100 s), so Bob is first.
    assert rows == [(1, "Bob", 80.0, 1), (2, "Ann", 80.0, 2), (3, "Cy", 60.0, 1)]
    assert board["you"]["rank"] == 2 and board["participants"] == 3 and board["percentile"] is None
    assert lb.weekly_board(db, dee, "this", now=WEEK)["you"] is None
    last = lb.weekly_board(db, dee, "last", now=WEEK)                       # only Dee's out-of-week test is in last week
    assert last["participants"] == 1 and last["top"][0]["percent"] == 100.0


def test_the_weekly_page_shows_this_students_place_for_a_test_finished_now(db, paper):
    name, uid = person(db, "Now")
    ranked(db, uid, key_of(db, paper), 20, 500)
    text = page(client_for(name), "/leaderboard")
    assert "This week" in text and "Last week" in text and "(you)" in text
    assert client_for(name).get("/leaderboard?week=last").status_code == 200
    assert client_for(name).get("/leaderboard?week=bogus").status_code == 200


# --------------------------------------------------------------------------- the overview page

def test_the_overview_lists_tests_with_participants_and_your_rank(db, paper):
    history = db.query(models.Subject).filter_by(name="History").one().id
    name, uid = person(db, "Lister")
    other = person(db, "Listed")[1]
    ranked(db, uid, key_of(db, paper), 10, 500, paper_id=paper.id)
    ranked(db, other, key_of(db, paper), 20, 500, paper_id=paper.id)
    text = page(client_for(name), "/leaderboard")
    block = text.split("By test")[1]
    assert paper.title in block and "Full-length test" in block and "2 participants" in block and "you are #2" in block
    assert f"/leaderboard/paper/{paper.id}" in client_for(name).get("/leaderboard").text


# --------------------------------------------------------------------------- the switch

def test_switching_leaderboards_off_hides_them_everywhere(db, paper, admin):
    name, uid = person(db, "Switch")
    ranked(db, uid, key_of(db, paper), 10, 500, paper_id=paper.id)
    student = client_for(name)
    assert 'href="/leaderboard"' in student.get("/").text

    assert admin.post("/admin/settings/leaderboard", data={"enabled": "0"}).status_code == 303
    for url in ("/leaderboard", f"/leaderboard/paper/{paper.id}"):
        r = student.get(url)
        assert r.status_code == 303 and r.headers["location"] == "/"
    assert 'href="/leaderboard"' not in student.get("/").text
    assert 'href="/leaderboard"' not in student.get("/tests").text

    assert admin.post("/admin/settings/leaderboard", data={"enabled": "1"}).status_code == 303
    assert student.get("/leaderboard").status_code == 200 and 'href="/leaderboard"' in student.get("/tests").text


def test_the_result_page_shows_no_rank_while_leaderboards_are_off(db, paper, admin):
    name, uid = person(db, "Quiet")
    student = client_for(name)
    attempt = attempt_of(db, start_full(student, paper))
    save(student, attempt.id, 1, answer=letter_for(1), confidence="sure")
    student.post(f"/attempts/{attempt.id}/finish")
    assert "Leaderboard: you are #" in page(student, f"/attempts/{attempt.id}/result")
    admin.post("/admin/settings/leaderboard", data={"enabled": "0"})
    text = page(student, f"/attempts/{attempt.id}/result")
    assert "Leaderboard" not in text and "leaderboard" not in text


# --------------------------------------------------------------------------- access

def test_visitors_are_sent_to_login(anon, paper):
    for url in ("/leaderboard", f"/leaderboard/paper/{paper.id}", f"/leaderboard/paper/{paper.id}/subject/1"):
        r = anon.get(url)
        assert r.status_code == 303 and r.headers["location"].startswith("/login")


def test_boards_are_read_only(db, paper):
    name, _ = person(db, "Reader")
    student = client_for(name)
    for url in ("/leaderboard", f"/leaderboard/paper/{paper.id}"):
        for verb in ("post", "put", "patch", "delete"):
            assert getattr(student, verb)(url).status_code == 405
