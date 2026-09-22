"""
Student-side Stage 6: the home screen and the progress page — key numbers, score trend, accuracy by subject and
topic (weak areas highlighted), time per answer, "Practise these", and topic frequency across official PYQs.

The numbers come from attempts built directly in the database, so each expected value can be checked by hand.
"""
import html
import re
from datetime import datetime, timedelta

import pytest

from app import models
from app.models import AttemptKind, AttemptStatus, Confidence, QStatus
from app.practice import analytics, pool
from test_practice_stage2 import _year, live_paper, questions_of, user_id
from test_revision_stage5 import page

import itertools

NOW = datetime.utcnow()
_world_ids = itertools.count(1)          # each use of the `world` fixture gets its own student, so nothing accumulates


def text_of(client, url):
    return page(client, url)


def add_attempt(db, uid, kind, answers=(), *, score=None, max_marks=None, started=None, completed=None, paper_id=None,
                status=AttemptStatus.SUBMITTED):
    """A finished attempt. answers: [(question, correct, seconds)]. Marks aren't needed for the analytics."""
    started = started or NOW - timedelta(hours=1)
    attempt = models.Attempt(
        user_id=uid, kind=kind, status=status, started_at=started, completed_at=completed or started + timedelta(minutes=30),
        score=score, max_marks=max_marks, paper_id=paper_id, total_questions=len(answers), counts_for_rank=False)
    db.add(attempt)
    db.flush()
    for position, (q, correct, seconds) in enumerate(answers, start=1):
        wrong = next(l for l in "ABCD" if l != q.correct_answer)
        db.add(models.Response(
            attempt_id=attempt.id, question_id=q.id, position=position, visited=True,
            selected_answer=q.correct_answer if correct else wrong, is_correct=correct, confidence=Confidence.sure,
            time_spent_seconds=seconds, marks_if_correct=2.0, penalty_if_wrong=0.5, marks_awarded=2.0 if correct else -0.5))
    db.commit()
    return attempt


def bank(db, make_paper, title, n, *, topics=None, subjects=None, source_type="official_pyq"):
    """A live paper of n questions. subjects: {number: subject name}; topics: {number: topic id}."""
    paper = live_paper(db, make_paper, title, n=n, source_type=source_type)
    qs = questions_of(db, paper)
    for number, name in (subjects or {}).items():
        qs[number].subject_id = db.query(models.Subject).filter_by(name=name).one().id
    for number, topic_id in (topics or {}).items():
        qs[number].topic_id = topic_id
    db.commit()
    return paper, {n: qs[n] for n in qs}


def topic(db, name, subject_name="History"):
    t = models.Topic(name=name, subject_id=db.query(models.Subject).filter_by(name=subject_name).one().id)
    db.add(t)
    db.commit()
    return t.id


@pytest.fixture()
def world(db, make_paper, make_user):
    """One student, worked out by hand.

       Paper P: Q1-8 History, Q9-12 Polity.
       Practice A (History), seconds in brackets:  Q1 right(10) Q2 wrong(20) Q3 right(30) Q4 wrong(40)
                                                   Q5 wrong(none) Q6 wrong(60) Q7 right(50) Q8 wrong(70)
            -> 3 right of 8 = 37.5% -> 38%, below 60% with 8 answers: WEAK.  Mean of the 7 recorded times = 280/7 = 40 s.
       Practice B (Polity):  right(5) right(5) right(5) wrong(5)  -> 3 of 4 = 75%, but only 4 answers: not enough to judge.
       Tests:  T1 12/20 = 60%,  T2 15/20 = 75%,  T3 6/20 = 30%   (oldest to newest)
       Totals: 12 answers, 6 right = 50%.  Tests 3, average score (60+75+30)/3 = 55%.
               Average time = (280 + 20) / 11 recorded = 27.27 -> 27 s.
    """
    name = f"worldstudent{next(_world_ids)}"
    student = make_user(name)
    uid = user_id(db, name)
    paper, q = bank(db, make_paper, "World paper P", 12, subjects={**{i: "History" for i in range(1, 9)},
                                                                    **{i: "Polity" for i in range(9, 13)}})
    add_attempt(db, uid, AttemptKind.TOPIC, [(q[1], True, 10), (q[2], False, 20), (q[3], True, 30), (q[4], False, 40),
                                             (q[5], False, None), (q[6], False, 60), (q[7], True, 50), (q[8], False, 70)],
                started=NOW - timedelta(days=5))
    add_attempt(db, uid, AttemptKind.TOPIC, [(q[9], True, 5), (q[10], True, 5), (q[11], True, 5), (q[12], False, 5)],
                started=NOW - timedelta(days=4))
    for days, kind, score in ((3, AttemptKind.FULL, 12), (2, AttemptKind.SECTIONAL, 15), (1, AttemptKind.FULL, 6)):
        add_attempt(db, uid, kind, score=score, max_marks=20, paper_id=paper.id,
                    started=NOW - timedelta(days=days, hours=1), completed=NOW - timedelta(days=days))
    return student, uid, paper, q


# --------------------------------------------------------------------------- the numbers

def test_the_key_numbers_worked_by_hand(db, world):
    _, uid, _, _ = world
    o = analytics.overview(db, uid)
    assert (o["answered"], o["right"], o["accuracy"]) == (12, 6, 50)
    assert o["tests_taken"] == 3 and o["average_score"] == 55.0
    assert o["average_seconds"] == 27                                          # 300 s over 11 timed answers


def test_the_score_trend_lists_finished_timed_tests_oldest_first(db, world):
    _, uid, paper, _ = world
    points = analytics.trend(db, uid)
    assert [p["percent"] for p in points] == [60.0, 75.0, 30.0]
    assert [p["kind"] for p in points] == ["Full-length test", "Sectional test", "Full-length test"]
    assert all(p["label"] == "World paper P" for p in points) and points[0]["when"] < points[-1]["when"]


def test_only_finished_timed_tests_with_a_maximum_count_towards_the_trend(db, world):
    _, uid, paper, q = world
    add_attempt(db, uid, AttemptKind.FULL, score=5, max_marks=20, status=AttemptStatus.IN_PROGRESS)        # still running
    add_attempt(db, uid, AttemptKind.TOPIC, score=9, max_marks=10)                                          # untimed practice
    add_attempt(db, uid, AttemptKind.FULL, score=3, max_marks=0)                                            # nothing to compare to
    add_attempt(db, uid, AttemptKind.SECTIONAL, score=-4, max_marks=20, status=AttemptStatus.EXPIRED)       # timed out: counts
    assert [p["percent"] for p in analytics.trend(db, uid)][-1] == -20.0
    assert len(analytics.trend(db, uid)) == 4


def test_the_trend_keeps_only_the_most_recent_tests(db, make_paper, make_user):
    make_user("longtrendstudent")
    uid = user_id(db, "longtrendstudent")
    paper, _ = bank(db, make_paper, "Long trend paper", 1)
    for i in range(analytics.MAX_TREND_POINTS + 5):
        add_attempt(db, uid, AttemptKind.FULL, score=i, max_marks=100, paper_id=paper.id,
                    started=NOW - timedelta(days=100 - i, hours=1), completed=NOW - timedelta(days=100 - i))
    points = analytics.trend(db, uid)
    assert len(points) == analytics.MAX_TREND_POINTS
    assert points[-1]["percent"] == analytics.MAX_TREND_POINTS + 4                              # the newest is kept


def test_subject_accuracy_flags_only_the_weak_ones_with_enough_answers(db, world):
    _, uid, _, _ = world
    rows = {r["name"]: r for r in analytics.by_subject(db, uid)}
    history, polity = rows["History"], rows["Polity"]
    assert (history["answered"], history["right"], history["accuracy"]) == (8, 3, 38)
    assert history["weak"] is True and history["enough"] is True
    assert history["avg_seconds"] == pytest.approx(40.0)
    assert (polity["answered"], polity["accuracy"]) == (4, 75)
    assert polity["weak"] is False and polity["enough"] is False                              # 4 answers isn't enough to judge
    assert [r["name"] for r in analytics.by_subject(db, uid)] == ["History", "Polity"]           # weakest / judgeable first


def test_a_weak_subject_needs_at_least_five_answers(db, make_paper, make_user):
    make_user("fewanswersstudent")
    uid = user_id(db, "fewanswersstudent")
    _, q = bank(db, make_paper, "Few answers paper", 5, subjects={i: "Economy" for i in range(1, 6)})
    add_attempt(db, uid, AttemptKind.TOPIC, [(q[i], False, 10) for i in range(1, 5)])           # 0 of 4: bad, but only 4 answers
    economy = next(r for r in analytics.by_subject(db, uid) if r["name"] == "Economy")
    assert economy["accuracy"] == 0 and economy["weak"] is False
    add_attempt(db, uid, AttemptKind.TOPIC, [(q[5], False, 10)])                                # the fifth wrong answer
    economy = next(r for r in analytics.by_subject(db, uid) if r["name"] == "Economy")
    assert economy["answered"] == 5 and economy["weak"] is True


def test_60_percent_is_not_weak_but_59_is(db, make_paper, make_user):
    make_user("boundarystudent")
    uid = user_id(db, "boundarystudent")
    _, q = bank(db, make_paper, "Boundary paper", 10, subjects={i: "Geography" for i in range(1, 11)})
    add_attempt(db, uid, AttemptKind.TOPIC, [(q[i], i <= 6, 10) for i in range(1, 11)])          # 6 of 10 = 60%
    assert next(r for r in analytics.by_subject(db, uid) if r["name"] == "Geography")["weak"] is False
    add_attempt(db, uid, AttemptKind.TOPIC, [(q[i], False, 10) for i in range(1, 3)])            # 6 of 12 = 50%
    assert next(r for r in analytics.by_subject(db, uid) if r["name"] == "Geography")["weak"] is True


def test_topic_accuracy_ignores_questions_with_no_topic(db, make_paper, make_user):
    make_user("topicgroupstudent")
    uid = user_id(db, "topicgroupstudent")
    tid = topic(db, "Grouping topic")
    _, q = bank(db, make_paper, "Topic group paper", 8, subjects={i: "History" for i in range(1, 9)},
                topics={i: tid for i in range(1, 6)})                                             # Q6-8 have no topic
    add_attempt(db, uid, AttemptKind.TOPIC, [(q[1], True, 10), (q[2], False, 10), (q[3], False, 10), (q[4], False, 10),
                                             (q[5], True, 10), (q[6], False, 10), (q[7], False, 10), (q[8], False, 10)])
    topics = analytics.by_topic(db, uid)
    assert [(t["name"], t["answered"], t["right"], t["accuracy"], t["weak"]) for t in topics] == [("Grouping topic", 5, 2, 40, True)]
    assert topics[0]["subject"] == "History"


# --------------------------------------------------------------------------- where to focus

def test_weak_areas_prefer_topics_and_fall_back_to_subjects(db, make_paper, make_user):
    make_user("weakareasstudent")
    uid = user_id(db, "weakareasstudent")
    _, q = bank(db, make_paper, "Weak areas paper", 8, subjects={i: "History" for i in range(1, 9)})
    add_attempt(db, uid, AttemptKind.TOPIC, [(q[i], False, 10) for i in range(1, 7)])            # History: 0 of 6, no topics set yet
    weak = analytics.weak_areas(db, uid)
    assert weak["kind"] == "subjects" and [i["name"] for i in weak["items"]] == ["History"]

    tid = topic(db, "Weak areas topic")
    for number in range(1, 7):
        q[number].topic_id = tid
    db.commit()
    weak = analytics.weak_areas(db, uid)
    assert weak["kind"] == "topics" and weak["ids"] == [tid]                                     # now a specific topic is named


def test_nothing_is_weak_when_the_student_is_doing_well(db, make_paper, make_user):
    make_user("doingwellstudent")
    uid = user_id(db, "doingwellstudent")
    _, q = bank(db, make_paper, "Doing well paper", 6, subjects={i: "Polity" for i in range(1, 7)})
    add_attempt(db, uid, AttemptKind.TOPIC, [(q[i], True, 10) for i in range(1, 7)])
    assert analytics.weak_areas(db, uid) == {"kind": None, "items": [], "ids": []}


def test_only_the_three_weakest_areas_are_named(db, make_paper, make_user):
    make_user("threeweakstudent")
    uid = user_id(db, "threeweakstudent")
    tids = [topic(db, f"Weakest {i}") for i in range(5)]
    _, q = bank(db, make_paper, "Three weak paper", 25, topics={n: tids[(n - 1) // 5] for n in range(1, 26)})
    answers = []
    for group, right_count in enumerate((0, 1, 2, 2, 3)):                                       # accuracy 0, 20, 40, 40, 60 %
        for j in range(5):
            answers.append((q[group * 5 + j + 1], j < right_count, 10))
    add_attempt(db, uid, AttemptKind.TOPIC, answers)
    weak = analytics.weak_areas(db, uid)
    assert weak["ids"] == [tids[0], tids[1], tids[2]]                       # two topics tie at 40%; the name order breaks it
    assert len(weak["items"]) == 3 and [i["accuracy"] for i in weak["items"]] == [0, 20, 40]


def test_practise_these_draws_only_from_the_weak_topic(db, make_paper, make_user):
    student = make_user("practisethesestudent")
    uid = user_id(db, "practisethesestudent")
    weak_topic, fine_topic = topic(db, "Practise weak"), topic(db, "Practise fine")
    _, q = bank(db, make_paper, "Practise these paper", 12, topics={**{i: weak_topic for i in range(1, 7)},
                                                                     **{i: fine_topic for i in range(7, 13)}})
    add_attempt(db, uid, AttemptKind.TOPIC, [(q[i], False, 10) for i in range(1, 6)] + [(q[i], True, 10) for i in range(7, 12)])
    r = student.post("/analytics/practise-weak", data={})
    assert r.status_code == 303 and re.fullmatch(r"/attempts/\d+", r.headers["location"])
    db.rollback()
    attempt = db.get(models.Attempt, int(r.headers["location"].rsplit("/", 1)[1]))
    assert attempt.kind == AttemptKind.TOPIC and attempt.user_id == uid
    assert {x.question_id for x in attempt.responses} == {q[i].id for i in range(1, 7)}          # only the weak topic's questions
    assert '"weak_areas": "topics"' in attempt.filters_json


def test_practise_these_says_so_when_nothing_is_weak_or_nothing_is_live(db, make_paper, make_user, admin, only):
    fresh = make_user("nothingweakstudent")
    r = fresh.post("/analytics/practise-weak", data={})
    assert r.headers["location"] == "/analytics"
    assert "Nothing is flagged as weak" in text_of(fresh, "/analytics")

    student = make_user("noliveweakstudent")
    uid = user_id(db, "noliveweakstudent")
    paper, q = bank(db, make_paper, "Not live weak paper", 6, subjects={i: "Economy" for i in range(1, 7)})
    add_attempt(db, uid, AttemptKind.TOPIC, [(q[i], False, 10) for i in range(1, 7)])
    admin.post(f"/papers/{paper.id}/unpublish")
    only(paper)                       # other tests leave live Economy questions in the shared database; look at this paper alone
    before = db.query(models.Attempt).filter_by(user_id=uid).count()
    student.post("/analytics/practise-weak", data={})
    assert "no questions available to practise" in text_of(student, "/analytics")
    db.rollback()
    assert db.query(models.Attempt).filter_by(user_id=uid).count() == before


# --------------------------------------------------------------------------- the range filter

def test_the_range_filter_scopes_every_number(db, world):
    student, uid, _, _ = world
    old = NOW - timedelta(days=100)
    for a in db.query(models.Attempt).filter_by(user_id=uid, kind=AttemptKind.TOPIC).all():
        a.started_at = old
    db.commit()

    everything = analytics.overview(db, uid)
    recent = analytics.overview(db, uid, analytics.since_for("30"))
    assert everything["answered"] == 12 and recent["answered"] == 0 and recent["tests_taken"] == 3
    assert analytics.overview(db, uid, analytics.since_for("90"))["answered"] == 0
    assert analytics.overview(db, uid, analytics.since_for("nonsense"))["answered"] == 12       # unknown range = all time
    assert analytics.by_subject(db, uid, analytics.since_for("30")) == []

    assert "6 of 12 answers right" in text_of(student, "/analytics")
    in_range = text_of(student, "/analytics?range=30")
    assert "Nothing to show" not in in_range                                                       # the 3 tests are in range
    assert "Timed tests taken 3" in in_range and "no answers in this period" in in_range and "0 of 0" not in in_range
    assert text_of(student, "/analytics?range=bogus") == text_of(student, "/analytics")


# --------------------------------------------------------------------------- the progress page

def test_the_progress_page_shows_the_numbers_charts_and_table_twins(db, world):
    student, _, _, _ = world
    text = text_of(student, "/analytics")
    for expected in ("Your progress", "Timed tests taken", "Average test score", "55%", "Accuracy", "50%",
                     "6 of 12 answers right", "Average time per answer", "0m 27s", "Score trend", "Accuracy by subject"):
        assert expected in text, expected

    raw = student.get("/analytics").text
    assert raw.count("<svg") == 3                                    # score trend, subject accuracy, and the official-PYQ chart
    assert raw.count("View as a table") == 3                         # every chart has a table twin
    assert 'role="img"' in raw and 'src="/static/charts.js"' in raw
    assert raw.count('class="viz-legend"') == 1                      # the emphasis chart has its legend; the single-series line has none
    assert "data-tip=\"38% right | History · 3 of 8 answered · weak\"" in raw
    assert ">30%<" in raw                                            # the line's final value is labelled directly


def test_the_subject_chart_highlights_the_weak_subject_and_keeps_the_rest_grey(db, world, make_paper):
    student, uid, _, _ = world
    _, q = bank(db, make_paper, "Grey bar paper", 5, subjects={i: "Geography" for i in range(1, 6)})
    add_attempt(db, uid, AttemptKind.TOPIC, [(q[i], i <= 4, 10) for i in range(1, 6)])          # Geography 4 of 5 = 80%
    raw = student.get("/analytics").text
    svg = raw.split('id="subjects"')[1].split("</svg>")[0]
    assert re.findall(r'<path class="(viz-bar[^"]*)"', svg) == ["viz-bar accent", "viz-bar"]      # weak History, then Geography
    assert "38% · weak" in svg and ">80%<" in svg
    assert "Polity" not in svg                                                                   # too few answers: table only
    twin = raw.split('id="subjects"')[1].split("View as a table")[1]
    assert "Polity" in twin and "Not enough answers yet" in twin and "<strong>Weak</strong>" in twin


def test_a_single_test_is_a_sentence_not_a_line(db, make_paper, make_user):
    student = make_user("singletestpage")
    uid = user_id(db, "singletestpage")
    paper, q = bank(db, make_paper, "Single test paper", 2)
    add_attempt(db, uid, AttemptKind.FULL, [(q[1], True, 10)], score=13, max_marks=20, paper_id=paper.id)
    text = text_of(student, "/analytics")
    assert "You've taken one timed test so far (65% in Single test paper)" in text
    assert "<svg" not in student.get("/analytics").text.split("What official PYQs ask about")[0]


def test_a_new_student_gets_friendly_empty_states(make_user):
    student = make_user("emptyprogressstudent")
    text = text_of(student, "/analytics")
    assert "Nothing to show" in text and "Answer some questions" in text
    assert "Timed tests taken" not in text
    assert student.get("/analytics").status_code == 200


def test_where_to_focus_lists_the_weak_area_and_offers_practice(db, world):
    student, _, _, _ = world
    text = text_of(student, "/analytics")
    assert "Where to focus" in text and "Subjects where you're below 60% (with at least 5 answers)" in text
    assert "History 38% right · 3 of 8" in text and "Practise these (10 questions)" in text


def test_names_from_data_cannot_inject_markup_into_the_page(db, make_paper, make_user):
    student = make_user("escapestudent")
    uid = user_id(db, "escapestudent")
    evil = topic(db, '<script>alert(1)</script> "quoted"')
    _, q = bank(db, make_paper, "Escape paper", 6, subjects={i: "History" for i in range(1, 7)}, topics={i: evil for i in range(1, 7)})
    add_attempt(db, uid, AttemptKind.TOPIC, [(q[i], False, 10) for i in range(1, 7)])
    raw = student.get("/analytics").text
    assert "<script>alert(1)" not in raw
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in raw
    assert "&quot;quoted&quot;" in raw and '"quoted"' not in raw.split("<svg")[1].split("</svg>")[0].replace("&quot;quoted&quot;", "")


# --------------------------------------------------------------------------- privacy

def test_each_student_sees_only_their_own_numbers(db, world, make_user, make_paper):
    mine, my_id, _, _ = world
    other = make_user("otherprogressstudent")
    other_id = user_id(db, "otherprogressstudent")
    paper, q = bank(db, make_paper, "Someone else's paper", 3, subjects={i: "Economy" for i in range(1, 4)})
    add_attempt(db, other_id, AttemptKind.FULL, [(q[1], True, 100)], score=20, max_marks=20, paper_id=paper.id)

    mine_text, other_text = text_of(mine, "/analytics"), text_of(other, "/analytics")
    assert "World paper P" in mine_text and "Someone else's paper" not in mine_text
    assert "Someone else's paper" in other_text and "World paper P" not in other_text
    assert "Timed tests taken 3" in mine_text and "Timed tests taken 1" in other_text
    assert analytics.overview(db, other_id)["tests_taken"] == 1 and analytics.overview(db, my_id)["tests_taken"] == 3


def test_there_is_no_way_to_ask_for_another_students_progress(db, world, make_user):
    _, my_id, _, _ = world
    snoop = make_user("snoopstudent")
    for url in (f"/analytics?user_id={my_id}", f"/analytics?uid={my_id}", f"/analytics/{my_id}"):
        response = snoop.get(url)
        assert response.status_code in (200, 404)
        if response.status_code == 200:
            assert "World paper P" not in response.text and "Nothing to show" in html.unescape(response.text)


# --------------------------------------------------------------------------- what official PYQs ask about

@pytest.fixture()
def only(monkeypatch):
    """Restrict the pool to the papers a test creates, so counts are exact whatever other tests left behind."""
    def restrict(*papers):
        ids = [p.id for p in papers]
        original = pool.live_questions
        monkeypatch.setattr(pool, "live_questions", lambda db: original(db).filter(models.Paper.id.in_(ids)))
    return restrict


def test_topic_frequency_counts_live_official_questions_only(db, make_paper, only):
    t_a, t_b, t_c = topic(db, "Freq A"), topic(db, "Freq B"), topic(db, "Freq C")
    official, q = bank(db, make_paper, "Official freq paper", 8, topics={1: t_a, 2: t_a, 3: t_a, 4: t_b, 5: t_b, 6: t_c,
                                                                       7: t_c, 8: t_c})
    coaching, _ = bank(db, make_paper, "Coaching freq paper", 4, topics={i: t_b for i in range(1, 5)}, source_type="coaching_test")
    q[8].status = QStatus.NEEDS_REVIEW                                                            # not live: not counted
    db.commit()
    only(official, coaching)

    freq = analytics.topic_frequency(db)
    assert freq["kind"] == "topics" and freq["total"] == 7
    assert [(r["name"], r["count"]) for r in freq["rows"]] == [("Freq A", 3), ("Freq B", 2), ("Freq C", 2)]   # coaching's 4 not counted


def test_frequency_falls_back_to_subjects_when_no_topics_are_assigned(db, make_paper, only):
    paper, _ = bank(db, make_paper, "Subject freq paper", 6, subjects={1: "History", 2: "History", 3: "History", 4: "Polity",
                                                                     5: "Polity", 6: "Economy"})
    only(paper)
    freq = analytics.topic_frequency(db)
    assert freq["kind"] == "subjects"
    assert [(r["name"], r["count"]) for r in freq["rows"]] == [("History", 3), ("Polity", 2), ("Economy", 1)]


def test_frequency_is_empty_without_official_questions(db, make_paper, only):
    coaching, _ = bank(db, make_paper, "Only coaching paper", 3, subjects={1: "History"}, source_type="coaching_test")
    only(coaching)
    assert analytics.topic_frequency(db) == {"kind": None, "total": 0, "rows": []}


def test_the_frequency_chart_is_one_colour_with_counts_at_the_bar_tips(db, make_paper, make_user, only):
    t_a, t_b = topic(db, "Chart freq A"), topic(db, "Chart freq B")
    paper, _ = bank(db, make_paper, "Chart freq paper", 5, topics={1: t_a, 2: t_a, 3: t_a, 4: t_b, 5: t_b})
    only(paper)
    raw = make_user("freqchartstudent").get("/analytics").text
    svg = raw.split('id="frequency"')[1].split("</svg>")[0]
    assert re.findall(r'<path class="(viz-bar[^"]*)"', svg) == ["viz-bar accent", "viz-bar accent"]   # one series, one colour
    assert ">3<" in svg and ">2<" in svg and "Chart freq A" in svg
    assert 'data-tip="3 questions |' in svg
    plain = re.sub(r"\s+", " ", html.unescape(raw))
    assert "across 5 official PYQ questions" in plain
    assert "isn't affected by the date range" in plain


def test_frequency_is_the_same_for_every_student_and_needs_no_history(db, make_paper, make_user, only):
    paper, _ = bank(db, make_paper, "Shared freq paper", 3, subjects={1: "Science & Tech", 2: "Science & Tech", 3: "Polity"})
    only(paper)
    a, b = make_user("freqstudenta"), make_user("freqstudentb")
    # (the heading's words also appear in the filter note above it, so take the LAST occurrence: the section itself)
    part = lambda c: html.unescape(c.get("/analytics").text).split("What official PYQs ask about")[-1]
    assert part(a) == part(b)                                        # identical for everyone: it describes the bank, not a student
    assert "Science & Tech" in part(a) and "Nothing to show" not in part(a)


# --------------------------------------------------------------------------- the home page

def test_home_shows_recent_tests_newest_first_and_only_five(db, make_paper, make_user):
    student = make_user("homerecentstudent")
    uid = user_id(db, "homerecentstudent")
    paper, _ = bank(db, make_paper, "Home recent paper", 1)
    for i in range(7):
        add_attempt(db, uid, AttemptKind.FULL, score=i * 2, max_marks=20, paper_id=paper.id,
                    started=NOW - timedelta(days=10 - i, hours=1), completed=NOW - timedelta(days=10 - i))
    text = text_of(student, "/")
    assert "Recent tests" in text
    section = text.split("Recent tests")[1].split("See your full progress")[0]
    percents = re.findall(r"(\d+)% \(", section)
    assert percents == ["60", "50", "40", "30", "20"]                                              # 12/20 down to 4/20: newest five
    assert "See your full progress" in text


def test_home_offers_practise_these_for_a_student_with_a_weak_area(db, world):
    student, _, _, _ = world
    text = text_of(student, "/")
    assert "Where to focus" in text and "History 38% right · 8 answers" in text and "Practise these" in text
    assert "Recent tests" in text and "World paper P" in text


def test_a_new_student_sees_no_progress_cards_on_home(make_user):
    text = text_of(make_user("noprogresshome"), "/")
    assert "Recent tests" not in text and "Where to focus" not in text and "Welcome, noprogresshome" in text


def test_practise_these_on_home_starts_a_session(db, world):
    student, uid, _, _ = world
    r = student.post("/analytics/practise-weak", data={"range": "all"})
    assert r.status_code == 303 and r.headers["location"].startswith("/attempts/")
    attempt = db.get(models.Attempt, int(r.headers["location"].rsplit("/", 1)[1]))
    assert all(db.get(models.Question, x.question_id).subject_id == db.query(models.Subject).filter_by(name="History").one().id
               for x in attempt.responses)
