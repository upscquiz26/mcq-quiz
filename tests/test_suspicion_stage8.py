"""Import stage 8: suspicious-answer detection (synthetic responses) and the cross-source answer-conflict flag."""
import itertools
import json

import pytest

from app import auth, duplicates, models, sample_audit
from app.models import AttemptStatus, QStatus
from app.practice import pool, suspicion
from conftest import make_student_client, question_form
from test_duplicates_stage7 import OPTIONS, paper_of, pairs, question, scan, sentence

_run = itertools.count(1)


# --------------------------------------------------------------------------- the rule itself, on synthetic data

def crowd(n, chooser, key="A"):
    """n students with rising ability (student i has accuracy i/100). chooser(i) says what student i chose."""
    chosen = {i: chooser(i) for i in range(1, n + 1)}
    return key, chosen, {i: i / 100 for i in range(1, n + 1)}


def test_most_high_scorers_choosing_another_option_is_suspicious():
    key, chosen, ability = crowd(20, lambda i: "C" if i > 15 else "A")           # top 5 all chose C; everyone else chose the key
    verdict = suspicion.analyse_question(key, chosen, ability)
    assert verdict["popular"] == "C" and verdict["high_n"] == 5 and verdict["high_counts"] == {"C": 5}
    assert verdict["all_n"] == 20 and verdict["all_counts"] == {"C": 5, "A": 15}


def test_weak_students_getting_it_wrong_is_not_suspicious():
    key, chosen, ability = crowd(20, lambda i: "A" if i > 10 else "C")            # the strong choose the key; the weak choose C
    assert suspicion.analyse_question(key, chosen, ability) is None


def test_a_split_among_high_scorers_needs_a_clear_margin():
    # top 5: three chose C, two chose the key -> C is the majority but only by one student
    key, chosen, ability = crowd(20, lambda i: "C" if i in (16, 17, 18) else "A")
    assert suspicion.analyse_question(key, chosen, ability) is None
    # top 5: four chose C, one the key -> margin 3
    key, chosen, ability = crowd(20, lambda i: "C" if i in (17, 18, 19, 20) else "A")
    assert suspicion.analyse_question(key, chosen, ability)["popular"] == "C"


def test_the_key_winning_a_tie_is_never_suspicious():
    key, chosen, ability = crowd(24, lambda i: "C" if i in (19, 20, 21) else ("A" if i in (22, 23, 24) else "B"))   # top 6: 3 C, 3 A
    assert suspicion.analyse_question(key, chosen, ability) is None


def test_more_than_half_of_the_high_scorers_is_required():
    # top 5: two chose C, two D, one A: C is the popular option but not a majority
    key, chosen, ability = crowd(20, lambda i: {16: "C", 17: "C", 18: "D", 19: "D", 20: "A"}.get(i, "A"))
    assert suspicion.analyse_question(key, chosen, ability) is None


def test_too_few_students_gives_no_verdict():
    key, chosen, ability = crowd(suspicion.MIN_RESPONDERS - 1, lambda i: "C")
    assert suspicion.analyse_question(key, chosen, ability) is None
    key, chosen, ability = crowd(suspicion.MIN_RESPONDERS, lambda i: "C")
    assert suspicion.analyse_question(key, chosen, ability) is not None


def test_unqualified_students_are_ignored():
    key, chosen, ability = crowd(20, lambda i: "C" if i > 15 else "A")
    for i in range(16, 21):
        del ability[i]                                     # the strongest five have too few answers overall to be trusted
    verdict = suspicion.analyse_question(key, chosen, ability)
    assert verdict is None                                 # the remaining strong students chose the key


def test_ties_at_the_boundary_are_all_high_scorers():
    key = "A"
    chosen = {i: ("C" if i <= 9 else "A") for i in range(1, 21)}
    ability = {i: 0.5 for i in range(1, 21)}               # everyone equally able: everyone is a high scorer
    verdict = suspicion.analyse_question(key, chosen, ability)
    assert verdict is None and suspicion.analyse_question("A", {i: "C" for i in range(1, 21)}, ability)["high_n"] == 20


def test_the_high_group_is_a_quarter_but_at_least_five():
    key, chosen, ability = crowd(40, lambda i: "C" if i > 30 else "A")
    assert suspicion.analyse_question(key, chosen, ability)["high_n"] == 10
    key, chosen, ability = crowd(13, lambda i: "C" if i > 8 else "A")
    assert suspicion.analyse_question(key, chosen, ability)["high_n"] == 5


# --------------------------------------------------------------------------- the same thing on stored attempts

def make_class(db, questions, students=16, tag=None, wrong_on_first_for=range(12, 17)):
    """`students` students who each finished one attempt over all questions. Student i answers the first question in the list with the option
    the test says, and gets 10+i of the other questions right — so ability rises with i. Returns the students."""
    tag = tag or f"s8{next(_run)}"
    out = []
    for i in range(1, students + 1):
        user = models.User(username=f"{tag}_{i}", password_hash=auth.hash_password("pw123456"), is_admin=False, status=models.UserStatus.approved)
        db.add(user)
        db.flush()
        attempt = models.Attempt(user_id=user.id, kind="topic", status=AttemptStatus.SUBMITTED, total_questions=len(questions))
        db.add(attempt)
        db.flush()
        for position, q in enumerate(questions, start=1):
            if position == 1:
                letter = "B" if i in wrong_on_first_for else q.correct_answer
            else:
                letter = q.correct_answer if position - 2 < 10 + i else "D"
            db.add(models.Response(attempt_id=attempt.id, question_id=q.id, position=position, selected_answer=letter,
                                   is_correct=(letter == q.correct_answer), confidence=models.Confidence.sure))
        out.append(user)
    db.commit()
    return out


def live_paper(db, n=24):
    paper = paper_of(db, [sentence() for _ in range(n)], status=QStatus.LIVE)
    paper.publish_status = "published"
    for q in db.query(models.Question).filter_by(paper_id=paper.id):
        q.correct_answer = "A"
    db.commit()
    return paper, sorted(db.query(models.Question).filter_by(paper_id=paper.id), key=lambda q: q.question_number)


def row_for(db, q):
    db.rollback()
    db.expire_all()
    return db.query(models.AnswerSuspicion).filter_by(question_id=q.id).first()


def test_the_analysis_finds_the_question_the_best_students_disagree_on(db):
    paper, qs = live_paper(db)
    make_class(db, qs)
    result = suspicion.analyse(db)
    db.commit()
    assert result["checked"] >= 24 and result["new"] >= 1
    row = row_for(db, qs[0])
    assert (row.status, row.key_answer, row.popular_answer, row.high_n) == ("open", "A", "B", 5)
    assert json.loads(row.high_counts_json) == {"B": 5} and row.all_n == 16
    assert all(row_for(db, q) is None for q in qs[1:])                                    # the other 23 are ordinary


def test_only_finished_attempts_of_students_and_first_answers_count(db):
    paper, qs = live_paper(db)
    students = make_class(db, qs)
    top = students[-1]
    # an unfinished attempt and a later, different answer from the strongest student change nothing
    extra = models.Attempt(user_id=top.id, kind="topic", status=AttemptStatus.IN_PROGRESS)
    db.add(extra)
    db.flush()
    db.add(models.Response(attempt_id=extra.id, question_id=qs[0].id, position=1, selected_answer="A", confidence=models.Confidence.sure))
    later = models.Attempt(user_id=top.id, kind="topic", status=AttemptStatus.SUBMITTED)
    db.add(later)
    db.flush()
    db.add(models.Response(attempt_id=later.id, question_id=qs[0].id, position=1, selected_answer="A", confidence=models.Confidence.sure))
    admin_user = db.query(models.User).filter_by(is_admin=True).first()
    admin_attempt = models.Attempt(user_id=admin_user.id, kind="topic", status=AttemptStatus.SUBMITTED)
    db.add(admin_attempt)
    db.flush()
    db.add(models.Response(attempt_id=admin_attempt.id, question_id=qs[0].id, position=1, selected_answer="A", confidence=models.Confidence.sure))
    db.commit()
    answers = suspicion.first_answers(db)
    assert answers[top.id][qs[0].id] == "B" and admin_user.id not in answers
    suspicion.analyse(db)
    db.commit()
    assert row_for(db, qs[0]).high_n == 5


def test_a_question_nobody_disagrees_on_is_not_raised(db):
    paper, qs = live_paper(db)
    make_class(db, qs, wrong_on_first_for=[])
    suspicion.analyse(db)
    db.commit()
    assert row_for(db, qs[0]) is None


def test_only_live_questions_are_analysed(db):
    paper, qs = live_paper(db)
    make_class(db, qs)
    for q in qs:
        q.status = QStatus.VERIFIED
    db.commit()
    suspicion.analyse(db)
    db.commit()
    assert row_for(db, qs[0]) is None


def test_the_row_clears_itself_when_the_key_is_fixed_and_returns_when_it_is_changed_to_something_else(db):
    paper, qs = live_paper(db)
    make_class(db, qs)
    suspicion.analyse(db)
    db.commit()
    assert row_for(db, qs[0]).status == "open"
    qs[0].correct_answer = "B"                            # the key was wrong and has been fixed: the strong students are now right
    db.commit()
    result = suspicion.analyse(db)
    db.commit()
    assert row_for(db, qs[0]) is None and result["cleared"] >= 1


def test_dismissing_is_remembered_against_the_key_it_was_judged_on(db):
    paper, qs = live_paper(db)
    make_class(db, qs)
    suspicion.analyse(db)
    db.commit()
    row = row_for(db, qs[0])
    suspicion.dismiss(db, None, row)
    db.commit()
    suspicion.analyse(db)
    db.commit()
    assert row_for(db, qs[0]).status == "dismissed"                       # the same key, the same disagreement: stays dismissed
    qs[0].correct_answer = "C"                                            # the key changes (and students still disagree)
    db.commit()
    suspicion.analyse(db)
    db.commit()
    row = row_for(db, qs[0])
    assert row.status == "open" and row.key_answer == "C" and row.popular_answer == "B"


# --------------------------------------------------------------------------- the queue and its buttons

@pytest.fixture()
def raised(db):
    paper, qs = live_paper(db)
    make_class(db, qs)
    suspicion.analyse(db)
    db.commit()
    return paper, qs


def test_the_queue_shows_the_evidence(admin, db, raised):
    paper, qs = raised
    page = admin.get("/admin/suspicious").text
    assert f"{paper.title} · Q1" in page and "5 high scorers" in page and "All 16 students counted" in page
    assert "Most high scorers chose this" in page and "100%" in page
    assert "The key is right" in page and "send back to review" in page
    db.add(models.QuestionReport(user_id=db.query(models.User).filter_by(is_admin=False).first().id, question_id=qs[0].id, kind="wrong_answer"))
    db.commit()
    assert "1 open student report" in admin.get("/admin/suspicious").text


def test_the_queue_appears_in_the_menu_and_on_the_dashboard(admin, db, raised):
    assert 'href="/admin/suspicious"' in admin.get("/admin").text
    assert "where strong students disagree with the key" in admin.get("/admin").text


def test_dismissing_from_the_page(admin, db, raised):
    paper, qs = raised
    row = row_for(db, qs[0])
    assert admin.post(f"/admin/suspicious/{row.id}/dismiss").status_code == 303
    assert row_for(db, qs[0]).status == "dismissed"
    assert f"{paper.title} · Q1" not in admin.get("/admin/suspicious").text
    assert f"{paper.title} · Q1" in admin.get("/admin/suspicious?show=handled").text
    assert db.query(models.AuditLog).filter_by(action="suspicion.dismiss", entity_id=qs[0].id).count() == 1
    assert question(db, paper, 1).status == QStatus.LIVE                  # dismissing changes nothing about the question


def test_sending_a_question_back_takes_it_away_from_students_and_is_remembered(admin, db, raised):
    paper, qs = raised
    paper.audit_state = "passed"
    db.commit()
    row = row_for(db, qs[0])
    r = admin.post(f"/admin/suspicious/{row.id}/send-back")
    assert r.status_code == 303 and r.headers["location"].startswith(f"/review/{paper.id}")
    q = question(db, paper, 1)
    assert q.status == QStatus.NEEDS_REVIEW and q.id not in {x.id for x in pool.live_questions(db)}
    assert row_for(db, qs[0]).status == "sent_back" and db.get(models.Paper, paper.id).audit_state == "none"
    log = db.query(models.AuditLog).filter_by(action="question.reopen", entity_id=q.id).order_by(models.AuditLog.id.desc()).first()
    assert "suspicious answer" in log.detail_json
    suspicion.analyse(db)                                                # not live: the row is left as it was
    db.commit()
    assert row_for(db, qs[0]).status == "sent_back"
    q = question(db, paper, 1)
    q.status = QStatus.LIVE                                              # re-confirmed and published with the same key
    db.commit()
    suspicion.analyse(db)
    db.commit()
    assert row_for(db, qs[0]).status == "dismissed"


def test_analyse_now_reports_what_it_did_and_says_when_there_is_nothing_to_analyse(admin, db, raised, monkeypatch):
    admin.post("/admin/suspicious/analyse")
    assert "Checked" in admin.get("/admin/suspicious").text
    assert db.query(models.AuditLog).filter_by(action="suspicion.analyse").count() >= 1
    monkeypatch.setattr(suspicion, "analyse", lambda db: {"open": 0, "new": 0, "cleared": 0, "checked": 0})
    admin.post("/admin/suspicious/analyse")
    assert "Nothing to analyse yet" in admin.get("/admin/suspicious").text


def test_a_key_changed_after_dismissal_is_pointed_out(admin, db, raised):
    paper, qs = raised
    row = row_for(db, qs[0])
    admin.post(f"/admin/suspicious/{row.id}/dismiss")
    q = question(db, paper, 1)
    q.correct_answer = "D"
    db.commit()
    assert "The key was changed to D" in admin.get("/admin/suspicious?show=handled").text


def test_the_suspicion_pages_are_admin_only(admin, db, raised, anon):
    paper, qs = raised
    row = row_for(db, qs[0])
    student = make_student_client(db, "suspicionstudent")
    calls = [("get", "/admin/suspicious"), ("post", "/admin/suspicious/analyse"),
             ("post", f"/admin/suspicious/{row.id}/dismiss"), ("post", f"/admin/suspicious/{row.id}/send-back")]
    for client, expected in ((student, 403), (anon, 303)):
        for method, url in calls:
            assert getattr(client, method)(url).status_code == expected, url
    assert row_for(db, qs[0]).status == "open" and question(db, paper, 1).status == QStatus.LIVE
    assert "Suspicious" not in student.get("/").text


# --------------------------------------------------------------------------- cross-source answer conflicts

def put(db, paper, **fields):
    """Set fields on question 1 of a paper and commit (question() rolls back, so never chain two of them in one statement)."""
    q = question(db, paper, 1)
    for name, value in fields.items():
        setattr(q, name, value)
    db.commit()


def flagged(db, paper, number=1):
    return "source_conflict" in (question(db, paper, number).ocr_flags or "")


def conflicting(db, status=QStatus.NEEDS_REVIEW):
    text = sentence()
    one, two = paper_of(db, [text], status=status), paper_of(db, [text], status=status)
    put(db, one, correct_answer="A")
    put(db, two, correct_answer="C")
    scan(db, two)
    return one, two


def test_two_copies_with_different_answers_are_both_flagged(admin, db):
    one, two = conflicting(db)
    assert flagged(db, one) and flagged(db, two)
    page = admin.get(f"/review/{two.id}").text
    assert "Another copy of this question (in another paper) has a different answer" in page


def test_copies_that_agree_are_not_flagged(db):
    text = sentence()
    one, two = paper_of(db, [text]), paper_of(db, [text])
    scan(db, two)
    assert not flagged(db, one) and not flagged(db, two)


def test_the_comparison_is_by_the_answers_text_not_its_letter(db):
    text = sentence()
    same = ("Delhi", "Mumbai", "Chennai", "Kolkata")
    shuffled = ("Kolkata", "Chennai", "Delhi", "Mumbai")                       # the same four options in another order
    one = paper_of(db, [text], options=same)
    two = paper_of(db, [text], options=shuffled)
    put(db, one, correct_answer="A")                                            # Delhi
    put(db, two, correct_answer="C")                                            # Delhi again
    scan(db, two)
    assert not flagged(db, one) and not flagged(db, two)
    put(db, two, correct_answer="A")                                            # now Kolkata: a real disagreement
    duplicates.refresh_around(db, [question(db, two, 1).id])
    db.commit()
    assert flagged(db, one) and flagged(db, two)


def test_a_newly_flagged_confirmed_question_must_be_looked_at_again(admin, db):
    one, two = paper_of(db, [sentence()], status=QStatus.VERIFIED), paper_of(db, [sentence()], status=QStatus.VERIFIED)
    text = sentence()
    put(db, one, text=text, flags_acknowledged=True, correct_answer="A")
    put(db, two, text=text, flags_acknowledged=True, correct_answer="B")
    scan(db, two)
    q = question(db, two, 1)
    assert "source_conflict" in q.ocr_flags and q.flags_acknowledged is False
    from app.practice import pool as pool_module
    assert "flagged" in pool_module.publish_blockers(db, db.get(models.Paper, two.id))


def test_fixing_the_answer_clears_the_flag_on_both_sides(admin, db):
    one, two = conflicting(db)
    q = question(db, two, 1)
    assert admin.post(f"/review/{two.id}/question/{q.id}", data=question_form(q, correct_answer="A")).status_code == 303
    assert not flagged(db, one) and not flagged(db, two)


def test_merging_resolves_the_conflict(admin, db):
    one, two = conflicting(db)
    (row,) = pairs(db, one, two)
    assert admin.post(f"/admin/duplicates/{row.id}/merge", data={"keep": str(question(db, one, 1).id)}).status_code == 303
    assert not flagged(db, one) and not flagged(db, two)


def test_keeping_both_does_not_hide_a_real_conflict(admin, db):
    one, two = conflicting(db)
    (row,) = pairs(db, one, two)
    admin.post(f"/admin/duplicates/{row.id}/keep-both")
    assert flagged(db, one) and flagged(db, two)


def test_a_quarantined_copy_stops_counting(admin, db):
    one, two = conflicting(db)
    q = question(db, two, 1)
    q.status = QStatus.QUARANTINED
    q.quarantine_reason = "bad"
    db.commit()
    duplicates.refresh_around(db, [q.id, question(db, one, 1).id])
    db.commit()
    assert not flagged(db, one)


def test_restoring_a_merged_copy_brings_the_conflict_back(admin, db):
    one, two = conflicting(db)
    (row,) = pairs(db, one, two)
    admin.post(f"/admin/duplicates/{row.id}/merge", data={"keep": str(question(db, one, 1).id)})
    admin.post(f"/quarantine/{question(db, two, 1).id}/restore")
    assert flagged(db, one) and flagged(db, two)


def test_the_duplicates_page_compares_answers_by_text_too(admin, db):
    text = sentence()
    one = paper_of(db, [text], options=("Delhi", "Mumbai", "Chennai", "Kolkata"))
    two = paper_of(db, [text], options=("Kolkata", "Chennai", "Delhi", "Mumbai"))
    put(db, one, correct_answer="A")                                                           # Delhi
    put(db, two, correct_answer="C")                                                           # Delhi
    scan(db, two)
    assert "disagree on the answer" not in admin.get(f"/admin/duplicates?paper_id={two.id}").text
    put(db, two, correct_answer="A")                                                           # Kolkata
    shown = admin.get(f"/admin/duplicates?paper_id={two.id}").text
    assert "disagree on the answer (older: A: Delhi — newer: A: Kolkata)" in shown
