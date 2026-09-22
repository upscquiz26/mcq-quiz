"""
Student-side Stage 4: results — mistake reasons (suggested, then editable), the guessing report, the subject
breakdown, and the "should you have attempted more?" estimate. Numbers in the scenarios are worked out by hand
in the comments so they can be checked with a calculator.
"""
import html
import re
from types import SimpleNamespace

import pytest

from app import models
from app.models import AttemptKind, AttemptStatus, Confidence, MistakeReason
from app.practice import grading
from conftest import _client, _login
from test_practice_stage2 import _year, attempt_of, live_paper, questions_of, user_id
from test_timed_stage3 import (JSON, force_deadline, get_attempt, letter_for, save, start_full, timed_paper,
                               wrong_for)


def resp(answer="A", correct=False, confidence=Confidence.sure, seconds=None):
    """An unsaved Response with just what the classifier looks at."""
    return models.Response(selected_answer=answer, is_correct=correct, confidence=confidence, time_spent_seconds=seconds)


def set_times(db, attempt_id, times):
    """Pretend the student spent these seconds on each question (position -> seconds)."""
    a = get_attempt(db, attempt_id)
    for r in a.responses:
        if r.position in times:
            r.time_spent_seconds = times[r.position]
    db.commit()


def page_text(client, url):
    """The page with HTML entities decoded and every run of whitespace collapsed to one space, so a sentence the
    template wraps over several lines can still be matched as one string."""
    return re.sub(r"\s+", " ", html.unescape(client.get(url).text))


# --------------------------------------------------------------------------- suggesting a mistake reason

@pytest.mark.parametrize("confidence,seconds,typical,expected", [
    (Confidence.no_idea, 2, 60, MistakeReason.knowledge_gap),          # confidence beats speed: a fast "no idea" is still a gap
    (Confidence.no_idea, 500, 60, MistakeReason.knowledge_gap),
    (Confidence.guessed, 2, 60, MistakeReason.guess_miss),
    (Confidence.guessed, 500, 60, MistakeReason.guess_miss),
    (Confidence.sure, 14, 60, MistakeReason.careless),                  # quick = under max(8, 25% of 60) = 15 s
    (Confidence.sure, 15, 60, MistakeReason.conceptual_confusion),      # exactly 15 s is not "under"
    (Confidence.sure, 40, 60, MistakeReason.conceptual_confusion),
    (Confidence.sure, 150, 60, MistakeReason.conceptual_confusion),     # exactly 2.5 x typical is not "over"
    (Confidence.sure, 151, 60, MistakeReason.time_pressure),
    (Confidence.sure, 7, 20, MistakeReason.careless),                   # 25% of 20 is 5, so the 8 s floor applies
    (Confidence.sure, 8, 20, MistakeReason.conceptual_confusion),
    (Confidence.sure, None, 60, MistakeReason.conceptual_confusion),    # no timing recorded: don't guess "careless"
    (Confidence.sure, 0, 60, MistakeReason.conceptual_confusion),
    (Confidence.skipped, 30, 60, MistakeReason.unset),                  # answered but never rated
])
def test_the_suggested_reason_for_a_wrong_answer(confidence, seconds, typical, expected):
    assert grading.suggest_reason(resp(confidence=confidence, seconds=seconds), typical) == expected


def test_only_wrong_answers_get_a_reason():
    assert grading.suggest_reason(resp(correct=True), 60) is None
    assert grading.suggest_reason(resp(answer=None, correct=None, confidence=Confidence.skipped), 60) is None


def test_typical_time_is_the_median_of_answered_questions():
    attempt = SimpleNamespace(responses=[resp(seconds=s) for s in (10, 20, 30)])
    assert grading.typical_seconds(attempt) == 20
    attempt = SimpleNamespace(responses=[resp(seconds=s) for s in (5, 30, 40, 40, 45, 50, 60, 200)])
    assert grading.typical_seconds(attempt) == 42.5                                         # (40 + 45) / 2
    skipped = resp(answer=None, correct=None, seconds=999)                                  # unanswered questions don't count
    untimed = resp(seconds=None)
    assert grading.typical_seconds(SimpleNamespace(responses=[resp(seconds=10), resp(seconds=20), skipped, untimed])) == 72
    assert grading.typical_seconds(SimpleNamespace(responses=[])) == 72                     # too few timings: the default pace


def test_a_chosen_reason_survives_reanalysis():
    r = resp(confidence=Confidence.sure, seconds=5)
    attempt = SimpleNamespace(responses=[r, resp(seconds=40), resp(seconds=45)])
    grading.classify_attempt(attempt)
    assert r.mistake_reason == MistakeReason.careless
    grading.set_reason(r, "knowledge_gap", "never learnt this")
    grading.classify_attempt(attempt)                                                       # run the analysis again
    assert r.mistake_reason == MistakeReason.knowledge_gap and r.reason_overridden is True
    grading.reset_reason(attempt, r)
    assert r.mistake_reason == MistakeReason.careless and r.reason_overridden is False


@pytest.mark.parametrize("reason", ["unset", "bogus", "", "Careless", "careless; drop table"])
def test_only_listed_reasons_can_be_chosen(reason):
    r = resp()
    with pytest.raises(grading.ReasonRejected, match="listed reasons"):
        grading.set_reason(r, reason, None)
    assert r.reason_overridden is not True


def test_reasons_only_apply_to_wrong_answers_and_notes_are_tidied_and_capped():
    with pytest.raises(grading.ReasonRejected, match="wrong answers"):
        grading.set_reason(resp(correct=True), "careless", None)
    r = resp()
    grading.set_reason(r, "careless", "  too   fast \n once ")
    assert r.note == "too fast once"
    with pytest.raises(grading.ReasonRejected, match="under 500"):
        grading.set_reason(r, "careless", "x" * 501)
    grading.set_reason(r, "careless", "")
    assert r.note is None


# --------------------------------------------------------------------------- the worked example

@pytest.fixture()
def worked(db, make_paper, make_user):
    """10 questions (all History), 2 marks each, a wrong answer costs 1 mark. What the student does:

        Q  answer   sure?      seconds   result   marks   suggested reason
        1  right    sure        40       right     +2
        2  right    guessed     30       right     +2
        3  wrong    sure         5       wrong     -1     careless        (5 s is under 25% of the 42.5 s median)
        4  wrong    sure       200       wrong     -1     time pressure   (200 s is over 2.5 x 42.5)
        5  wrong    sure        40       wrong     -1     misconception
        6  wrong    guessed     60       wrong     -1     guess
        7  wrong    no idea     50       wrong     -1     concept gap
        8  right    no idea     45       right     +2     (lucky)
        9  skipped
       10  skipped

       Median of the answered times [5,30,40,40,45,50,60,200] = 42.5  ->  quick < 10.6 s, long > 106.25 s.
       Score  = 3 x 2 - 5 x 1 = 1     out of 20.   3 right, 5 wrong, 2 skipped.
       Accuracy 3/8 = 37.5% -> 38%.  Attempt rate 8/10 = 80%.
       Guessing: "guessed" nets +2 -1 = +1; "no idea" nets -1 +2 = +1; so the shaky answers added +2 and skipping
       them would have left 1 - 2 = -1.
    """
    paper = timed_paper(db, make_paper, "Worked example paper", n=10, marks=2.0, negative=0.5, duration_minutes=60)
    student = make_user("workedstudent")
    attempt = attempt_of(db, start_full(student, paper))
    plan = {1: (letter_for(1), "sure", 40), 2: (letter_for(2), "guessed", 30),
            3: (wrong_for(3), "sure", 5), 4: (wrong_for(4), "sure", 200), 5: (wrong_for(5), "sure", 40),
            6: (wrong_for(6), "guessed", 60), 7: (wrong_for(7), "no_idea", 50), 8: (letter_for(8), "no_idea", 45)}
    for pos, (letter, confidence, _) in plan.items():
        save(student, attempt.id, pos, answer=letter, confidence=confidence)
    set_times(db, attempt.id, {pos: seconds for pos, (_, _, seconds) in plan.items()})
    student.post(f"/attempts/{attempt.id}/finish")
    return SimpleNamespace(student=student, attempt_id=attempt.id, paper=paper)


def test_the_worked_example_totals(db, worked):
    a = get_attempt(db, worked.attempt_id)
    assert (a.correct_count, a.wrong_count, a.skipped_count) == (3, 5, 2)
    assert a.score == pytest.approx(1.0) and a.max_marks == pytest.approx(20.0)


def test_the_worked_example_mistake_reasons(db, worked):
    a = get_attempt(db, worked.attempt_id)
    reasons = {r.position: r.mistake_reason for r in a.responses}
    assert reasons[3] == MistakeReason.careless
    assert reasons[4] == MistakeReason.time_pressure
    assert reasons[5] == MistakeReason.conceptual_confusion
    assert reasons[6] == MistakeReason.guess_miss
    assert reasons[7] == MistakeReason.knowledge_gap
    for pos in (1, 2, 8, 9, 10):                                                            # right or skipped: no reason
        assert reasons[pos] == MistakeReason.unset
    assert grading.typical_seconds(a) == 42.5
    assert not any(r.reason_overridden for r in a.responses)


def test_the_worked_example_guessing_report(db, worked):
    report = grading.guessing_report(get_attempt(db, worked.attempt_id))
    levels = report["levels"]
    assert (levels["sure"]["answered"], levels["sure"]["right"], levels["sure"]["marks"]) == (4, 1, pytest.approx(-1.0))
    assert (levels["guessed"]["answered"], levels["guessed"]["right"], levels["guessed"]["marks"]) == (2, 1, pytest.approx(1.0))
    assert (levels["no_idea"]["answered"], levels["no_idea"]["right"], levels["no_idea"]["marks"]) == (2, 1, pytest.approx(1.0))
    assert levels["unrated"]["answered"] == 0
    assert report["shaky_answers"] == 4 and report["shaky_marks"] == pytest.approx(2.0)
    assert report["actual_score"] == pytest.approx(1.0) and report["score_if_skipped"] == pytest.approx(-1.0)
    assert report["helped"] is True and report["cost"] is False


def test_the_worked_example_results_page(db, worked):
    page = page_text(worked.student, f"/attempts/{worked.attempt_id}/result")
    assert "Your score" in page and '1 <span class="score-of">/ 20</span>' in page          # 6 - 5 = 1, out of 20
    assert "+6 for right answers" in page and "−5 for wrong answers" in page and "the 2 you skipped" in page
    assert "38%" in page                                                                       # 3 of 8 answered right
    assert "You attempted 8 of 10 questions (80% attempt rate)" in page

    guessing = page.split('id="guessing"')[1].split("</section>")[0]
    assert "added <strong>2</strong> marks overall" in guessing
    assert "would have been <strong>-1</strong> instead of <strong>1</strong>" in guessing
    for label in ("Sure", "Guessed", "No idea"):
        assert label in guessing

    reasons = page.split('id="reasons"')[1].split("</section>")[0]
    for label in ("Careless mistake", "Time pressure / confusion", "Misconception / overconfidence", "Guess", "Concept gap"):
        assert label in reasons
    assert len(re.findall(r"<strong>1</strong>", reasons)) == 5                              # one of each


def test_the_worked_example_attempt_analysis(db, worked):
    """Two skipped History questions. The student's History record is 3 right of 8 answered = 37.5%.
       Expected marks per skipped question = 0.375 x 2 - 0.625 x 1 = +0.125, so both are worth attempting,
       for an estimated +0.25 marks. With 2 marks and a 1-mark penalty a blind attempt breaks even at 1/3 = 33%."""
    analysis = grading.worth_attempting(db, get_attempt(db, worked.attempt_id))
    assert analysis["skipped"] == 2 and analysis["worth"] == 2
    assert analysis["expected_gain"] == pytest.approx(0.25)
    assert analysis["break_even_percent"] == 33 and analysis["your_accuracy_percent"] == 38
    assert [i["position"] for i in analysis["items"]] == [9, 10]

    page = page_text(worked.student, f"/attempts/{worked.attempt_id}/result")
    section = page.split('id="attempt-analysis"')[1].split("</section>")[0]
    assert "Should you have attempted more?" in section and "33%" in section and "+0.25" in section
    assert "about <strong>2</strong> of the 2 questions you skipped" in section
    assert "This is an estimate" in section


def test_the_review_page_shows_the_reason_time_marks_and_an_honest_explanation_label(db, worked):
    page = page_text(worked.student, f"/attempts/{worked.attempt_id}/q/3")
    assert "Why did this go wrong?" in page and "Careless mistake" in page and "suggested" in page
    assert "Time on this question: 0m 05s" in page and "Marks: -1" in page
    assert "Explanation · Unverified (as printed in the answer PDF)" in page
    right = page_text(worked.student, f"/attempts/{worked.attempt_id}/q/1")
    assert "Why did this go wrong?" not in right and "Marks: 2" in right                     # nothing to explain
    skipped = page_text(worked.student, f"/attempts/{worked.attempt_id}/q/9")
    assert "You didn't answer this one" in skipped and "Explanation · " in skipped and "Why did this go wrong?" not in skipped


# --------------------------------------------------------------------------- editing a reason

def test_a_student_can_change_a_reason_and_it_sticks(db, worked):
    s, a = worked.student, worked.attempt_id
    r = s.post(f"/attempts/{a}/q/3/reason", data={"reason": "knowledge_gap", "note": "never learnt Article 32"})
    assert r.status_code == 303 and r.headers["location"] == f"/attempts/{a}/q/3"
    row = get_attempt(db, a).responses[2]
    assert row.mistake_reason == MistakeReason.knowledge_gap and row.reason_overridden is True
    assert row.note == "never learnt Article 32"

    page = page_text(s, f"/attempts/{a}/q/3")
    assert "Saved." in page and "chosen by you" in page and "never learnt Article 32" in page
    assert "Back to suggested" in page
    result = page_text(s, f"/attempts/{a}/result")
    reasons = result.split('id="reasons"')[1].split("</section>")[0]
    assert "Careless mistake" not in reasons                                                # Q3 was the only careless one
    assert re.search(r'Concept gap</span> <strong>(\d+)</strong>', reasons).group(1) == "2"   # Q7 (suggested) + Q3 (chosen)

    s.post(f"/attempts/{a}/q/3/reason", data={"reset": "1"})                                # back to the suggestion
    row = get_attempt(db, a).responses[2]
    assert row.mistake_reason == MistakeReason.careless and row.reason_overridden is False
    assert row.note == "never learnt Article 32"                                            # the note is theirs; keep it


@pytest.mark.parametrize("data,fragment", [
    ({"reason": "unset"}, "listed reasons"), ({"reason": "bogus"}, "listed reasons"), ({}, "listed reasons"),
    ({"reason": "careless", "note": "x" * 501}, "under 500"),
])
def test_bad_reason_edits_are_refused_and_change_nothing(db, worked, data, fragment):
    s, a = worked.student, worked.attempt_id
    r = s.post(f"/attempts/{a}/q/3/reason", data=data)
    assert r.status_code == 303
    assert fragment in page_text(s, f"/attempts/{a}/q/3")
    row = get_attempt(db, a).responses[2]
    assert row.mistake_reason == MistakeReason.careless and not row.reason_overridden


def test_only_wrong_answers_can_be_given_a_reason(db, worked):
    s, a = worked.student, worked.attempt_id
    for pos in (1, 9):                                                                       # a right answer, a skipped one
        s.post(f"/attempts/{a}/q/{pos}/reason", data={"reason": "careless"})
        assert "Only wrong answers" in page_text(s, f"/attempts/{a}/q/{pos}")
    row = get_attempt(db, a).responses[0]
    assert not row.reason_overridden and row.note is None


def test_a_reason_cannot_be_set_while_the_session_is_still_running(db, make_paper, make_user):
    paper = timed_paper(db, make_paper, "Running reason paper", n=2)
    s = make_user("runningreasonstudent")
    attempt = attempt_of(db, start_full(s, paper))
    save(s, attempt.id, 1, answer=wrong_for(1), confidence="sure")
    s.post(f"/attempts/{attempt.id}/q/1/reason", data={"reason": "careless"})
    assert "once the session is finished" in page_text(s, f"/attempts/{attempt.id}/q/1")
    assert get_attempt(db, attempt.id).responses[0].reason_overridden is not True
    assert "Why did this go wrong?" not in page_text(s, f"/attempts/{attempt.id}/q/1")


def test_reasons_are_private_to_the_attempts_owner(db, worked, make_user, admin, anon):
    a = worked.attempt_id
    for who, client in {"another student": make_user("reasonintruder"), "the admin": admin}.items():
        r = client.post(f"/attempts/{a}/q/3/reason", data={"reason": "knowledge_gap"})
        assert r.status_code == 404, who
    r = anon.post(f"/attempts/{a}/q/3/reason", data={"reason": "knowledge_gap"})
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    assert get_attempt(db, a).responses[2].mistake_reason == MistakeReason.careless
    assert make_user("reasonintruder").get(f"/attempts/{a}/result").status_code == 404


# --------------------------------------------------------------------------- guessing report edge cases

def _run_test(db, make_paper, make_user, title, username, plan, marks=2.0, negative=0.5, n=None):
    """plan: {position: (letter_kind, confidence)} where letter_kind is 'right' / 'wrong'."""
    n = n or max(plan)
    paper = timed_paper(db, make_paper, title, n=n, marks=marks, negative=negative)
    student = make_user(username)
    attempt = attempt_of(db, start_full(student, paper))
    for pos, (kind, confidence) in plan.items():
        letter = letter_for(pos) if kind == "right" else wrong_for(pos)
        data = {"answer": letter}
        if confidence:
            data["confidence"] = confidence
        save(student, attempt.id, pos, **data)
    student.post(f"/attempts/{attempt.id}/finish")
    return student, attempt.id


def test_guessing_that_cost_marks_says_so(db, make_paper, make_user):
    """Q1 sure right +2; Q2 guessed wrong -1; Q3 no idea wrong -1; Q4 skipped.  Score 0.
       The two shaky answers cost 2, so skipping them would have scored 2."""
    student, a = _run_test(db, make_paper, make_user, "Costly guessing paper", "costlyguessstudent",
                           {1: ("right", "sure"), 2: ("wrong", "guessed"), 3: ("wrong", "no_idea")}, n=4)
    guessing = page_text(student, f"/attempts/{a}/result").split('id="guessing"')[1].split("</section>")[0]
    assert "cost you <strong>2</strong> marks overall" in guessing
    assert "would have been <strong>2</strong> instead of <strong>0</strong>" in guessing


def test_no_guessing_says_so(db, make_paper, make_user):
    student, a = _run_test(db, make_paper, make_user, "No guessing paper", "noguessstudent",
                           {1: ("right", "sure"), 2: ("wrong", "sure")}, n=3)
    guessing = page_text(student, f"/attempts/{a}/result").split('id="guessing"')[1].split("</section>")[0]
    assert "You didn't mark any answer as a guess" in guessing and "Sure" in guessing
    assert "would have been" not in guessing


def test_answers_without_confidence_are_reported_honestly(db, make_paper, make_user):
    student, a = _run_test(db, make_paper, make_user, "Unrated paper", "unratedstudent",
                           {1: ("right", None), 2: ("wrong", None)}, n=3)
    page = page_text(student, f"/attempts/{a}/result")
    guessing = page.split('id="guessing"')[1].split("</section>")[0]
    assert "No answers here were rated" in guessing
    row = get_attempt(db, a).responses[1]
    assert row.mistake_reason == MistakeReason.unset                                         # can't be classified...
    assert "Not classified" in page_text(student, f"/attempts/{a}/q/2")                      # ...but the student can choose


def test_a_break_even_of_25_percent_for_the_usual_one_third_scheme(db, make_paper, make_user):
    student, a = _run_test(db, make_paper, make_user, "One third paper", "onethirdstudent",
                           {1: ("right", "sure"), 2: ("wrong", "sure")}, marks=2.0, negative=1 / 3, n=4)
    assert grading.worth_attempting(db, get_attempt(db, a))["break_even_percent"] == 25


def test_topic_practice_gets_reasons_and_the_guessing_report_but_no_score_card(db, make_paper, make_user):
    year = next(_year)
    live_paper(db, make_paper, "Practice reasons paper", n=3, year=year)
    student = make_user("practicereasonstudent")
    attempt = attempt_of(db, student.post("/practice/start", data={"year": str(year), "count": "3"}))
    for pos, (kind, confidence) in {1: ("right", "sure"), 2: ("wrong", "guessed"), 3: ("wrong", "no_idea")}.items():
        db.rollback()
        q = db.get(models.Question, db.get(models.Attempt, attempt.id).responses[pos - 1].question_id)
        letter = q.correct_answer if kind == "right" else next(l for l in "ABCD" if l != q.correct_answer)
        student.post(f"/attempts/{attempt.id}/q/{pos}/answer", data={"answer": letter, "confidence": confidence})
    student.post(f"/attempts/{attempt.id}/finish")

    rows = get_attempt(db, attempt.id).responses
    assert [r.mistake_reason for r in rows] == [MistakeReason.unset, MistakeReason.guess_miss, MistakeReason.knowledge_gap]
    page = page_text(student, f"/attempts/{attempt.id}/result")
    assert "Guessing report" in page and "Why the wrong answers went wrong" in page
    assert "Your score" not in page and "By subject" not in page and "Should you have attempted more?" not in page


def test_a_practice_answer_with_no_measurable_time_is_not_called_careless(db, make_paper, make_user):
    year = next(_year)
    live_paper(db, make_paper, "Instant answer paper", n=1, year=year)
    student = make_user("instantanswerstudent")
    attempt = attempt_of(db, student.post("/practice/start", data={"year": str(year), "count": "1"}))
    db.rollback()
    q = db.get(models.Question, db.get(models.Attempt, attempt.id).responses[0].question_id)
    wrong = next(l for l in "ABCD" if l != q.correct_answer)
    student.post(f"/attempts/{attempt.id}/q/1/answer", data={"answer": wrong, "confidence": "sure"})
    student.post(f"/attempts/{attempt.id}/finish")
    assert get_attempt(db, attempt.id).responses[0].mistake_reason == MistakeReason.conceptual_confusion


def test_a_test_that_timed_out_is_analysed_too(db, make_paper, make_user):
    paper = timed_paper(db, make_paper, "Expired analysis paper", n=3, marks=2.0, negative=0.5)
    student = make_user("expiredanalysisstudent")
    attempt = attempt_of(db, start_full(student, paper))
    save(student, attempt.id, 1, answer=wrong_for(1), confidence="guessed")
    force_deadline(db, attempt.id, seconds_ago=60)
    student.get(f"/attempts/{attempt.id}/result")                                             # the clock ran out; this settles it
    done = get_attempt(db, attempt.id)
    assert done.status == AttemptStatus.EXPIRED and done.responses[0].mistake_reason == MistakeReason.guess_miss
    assert "Guessing report" in page_text(student, f"/attempts/{attempt.id}/result")


# --------------------------------------------------------------------------- should you have attempted more?

def _two_tests(db, make_paper, make_user, username, first_right):
    """A first test on one paper (5 History answers, all right or all wrong), then a second test on another paper:
       2 right, 2 wrong, 2 skipped. Returns the second attempt.  Marks 2, penalty 1 => break-even 1/3."""
    student = make_user(username)
    p1 = timed_paper(db, make_paper, f"History record {username}", n=5, marks=2.0, negative=0.5)
    first = attempt_of(db, start_full(student, p1))
    for pos in range(1, 6):
        save(student, first.id, pos, answer=letter_for(pos) if first_right else wrong_for(pos), confidence="sure")
    student.post(f"/attempts/{first.id}/finish")

    p2 = timed_paper(db, make_paper, f"History second {username}", n=6, marks=2.0, negative=0.5)
    second = attempt_of(db, start_full(student, p2))
    for pos in (1, 2):
        save(student, second.id, pos, answer=letter_for(pos), confidence="sure")
    for pos in (3, 4):
        save(student, second.id, pos, answer=wrong_for(pos), confidence="sure")
    student.post(f"/attempts/{second.id}/finish")
    return get_attempt(db, second.id)


def test_a_strong_record_makes_skipped_questions_worth_attempting(db, make_paper, make_user):
    """History overall: 5 right (first test) + 2 right of 4 (second) = 7 of 9 = 77.8%.
       Expected marks each = 0.7778 x 2 - 0.2222 x 1 = +1.3333, for 2 skipped = +2.6667."""
    second = _two_tests(db, make_paper, make_user, "strongrecordstudent", first_right=True)
    analysis = grading.worth_attempting(db, second)
    assert analysis["worth"] == 2
    assert analysis["expected_gain"] == pytest.approx(2 * (7 / 9 * 2 - 2 / 9 * 1))


def test_a_weak_record_means_skipping_was_sensible_even_if_this_test_looked_fine(db, make_paper, make_user):
    """History overall: 0 right (first test) + 2 right of 4 (second) = 2 of 9 = 22.2%, below the 33% break-even.
       Expected marks each = 0.2222 x 2 - 0.7778 x 1 = -0.3333: not worth it, though this test alone was 50%."""
    second = _two_tests(db, make_paper, make_user, "weakrecordstudent", first_right=False)
    analysis = grading.worth_attempting(db, second)
    assert analysis["worth"] == 0 and analysis["your_accuracy_percent"] == 50
    page = page_text(_login(_client(), "weakrecordstudent", "studentpass1"), f"/attempts/{second.id}/result")
    assert "weren't worth the risk" in page


def test_there_is_nothing_to_estimate_without_skips_or_answers(db, make_paper, make_user):
    student, a = _run_test(db, make_paper, make_user, "All answered paper", "allansweredstudent",
                           {1: ("right", "sure"), 2: ("wrong", "sure")}, n=2)
    assert grading.worth_attempting(db, get_attempt(db, a)) is None
    assert "Should you have attempted more?" not in page_text(student, f"/attempts/{a}/result")

    paper = timed_paper(db, make_paper, "None answered paper", n=2)
    other = make_user("noneansweredstudent")
    attempt = attempt_of(db, start_full(other, paper))
    other.post(f"/attempts/{attempt.id}/finish")
    assert grading.worth_attempting(db, get_attempt(db, attempt.id)) is None


# --------------------------------------------------------------------------- subject breakdown

def test_the_subject_breakdown_adds_up(db, make_paper, make_user):
    paper = timed_paper(db, make_paper, "Breakdown paper", n=6, marks=2.0, negative=0.5)
    geography = db.query(models.Subject).filter_by(name="Geography").one()
    q = questions_of(db, paper)
    q[4].subject_id = q[5].subject_id = geography.id
    q[6].subject_id = None
    db.commit()
    student = make_user("breakdownstudent")
    attempt = attempt_of(db, start_full(student, paper))
    save(student, attempt.id, 1, answer=letter_for(1), confidence="sure")     # History: right +2
    save(student, attempt.id, 2, answer=wrong_for(2), confidence="sure")      # History: wrong -1
    save(student, attempt.id, 4, answer=letter_for(4), confidence="sure")     # Geography: right +2
    save(student, attempt.id, 6, answer=wrong_for(6), confidence="sure")      # no subject: wrong -1
    student.post(f"/attempts/{attempt.id}/finish")

    rows = {r["subject"]: r for r in grading.subject_breakdown(db, get_attempt(db, attempt.id))}
    assert (rows["History"]["total"], rows["History"]["answered"], rows["History"]["right"], rows["History"]["marks"]) \
        == (3, 2, 1, pytest.approx(1.0)) and rows["History"]["skipped"] == 1
    assert (rows["Geography"]["total"], rows["Geography"]["right"], rows["Geography"]["marks"]) == (2, 1, pytest.approx(2.0))
    assert (rows["No subject set"]["total"], rows["No subject set"]["wrong"], rows["No subject set"]["marks"]) \
        == (1, 1, pytest.approx(-1.0))
    assert sum(r["marks"] for r in rows.values()) == pytest.approx(get_attempt(db, attempt.id).score)
    assert sum(r["total"] for r in rows.values()) == 6
    order = [r["subject"] for r in grading.subject_breakdown(db, get_attempt(db, attempt.id))]
    assert order[-1] == "No subject set"                                                      # unassigned always last

    page = page_text(student, f"/attempts/{attempt.id}/result")
    assert "By subject" in page and "History" in page and "Geography" in page


# --------------------------------------------------------------------------- the question list on the results page

def test_the_results_list_can_be_filtered(db, worked):
    s, a = worked.student, worked.attempt_id

    def listed(show):
        page = s.get(f"/attempts/{a}/result?show={show}").text
        return sorted(int(n) for n in re.findall(r'/attempts/%d/q/(\d+)" class="result-row"' % a, page))

    assert listed("all") == list(range(1, 11))
    assert listed("wrong") == [3, 4, 5, 6, 7]
    assert listed("skipped") == [9, 10]
    assert listed("shaky") == [2, 6, 7, 8]                                                    # guessed or no idea, answered
    assert listed("nonsense") == list(range(1, 11))                                            # unknown filter: show everything


def test_a_filter_with_no_matches_says_so(db, make_paper, make_user):
    student, a = _run_test(db, make_paper, make_user, "Empty filter paper", "emptyfilterstudent",
                           {1: ("right", "sure"), 2: ("wrong", "sure")}, n=2)                # nothing guessed, nothing skipped
    assert "Nothing to show for this filter" in student.get(f"/attempts/{a}/result?show=shaky").text
    assert "Nothing to show for this filter" in student.get(f"/attempts/{a}/result?show=skipped").text


def test_each_wrong_answer_in_the_list_shows_its_reason(db, worked):
    page = page_text(worked.student, f"/attempts/{worked.attempt_id}/result?show=wrong")
    assert page.count('class="chip small"') == 5
    assert "wrong · -1" in page


# --------------------------------------------------------------------------- old sessions and pages still work

def test_results_still_open_for_attempts_finished_before_reasons_existed(db, make_paper, make_user):
    """An attempt whose wrong answers have no reason recorded (mistake_reason 'unset') must not break the page."""
    student, a = _run_test(db, make_paper, make_user, "Legacy result paper", "legacyresultstudent",
                           {1: ("wrong", "sure"), 2: ("right", "sure")}, n=3)
    row = get_attempt(db, a).responses[0]
    row.mistake_reason = MistakeReason.unset
    db.commit()
    page = student.get(f"/attempts/{a}/result")
    assert page.status_code == 200 and "Not classified" in html.unescape(page.text)
    assert student.get(f"/attempts/{a}/q/1").status_code == 200
