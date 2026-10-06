"""Import stage 4: the review screen and gates — sample audit, publish gate, 'extracted X of Y', flagged first, re-run pages."""
import itertools
import random

import pytest

from app import ingest, models, sample_audit
from app.models import QStatus
from conftest import make_student_client, pass_audit, question_form
from test_text_extract import paper_pdf

_run = itertools.count(1)


def fresh(db, make_paper, n=20, status=QStatus.VERIFIED, **fields):
    """A paper with n questions, all in `status`, none edited."""
    paper = make_paper(title=f"Gate paper {next(_run)}", n=n, **fields)
    for q in db.query(models.Question).filter_by(paper_id=paper.id):
        q.status = status
    db.commit()
    return paper


def reload(db, paper):
    db.rollback()
    db.expire_all()
    return db.get(models.Paper, paper.id)


def numbers(db, paper, **filters):
    db.rollback()
    return sorted(q.question_number for q in db.query(models.Question).filter_by(paper_id=paper.id, **filters))


def start(admin, db, paper):
    assert admin.post(f"/review/{paper.id}/audit/start").status_code == 303
    db.rollback()
    return sorted(db.query(models.Question).filter_by(paper_id=paper.id, audit_pick=True), key=lambda q: q.question_number)


def verdict(admin, paper, q, v):
    return admin.post(f"/review/{paper.id}/audit/{q.id}/check", data={"verdict": v})


# --------------------------------------------------------------------------- sample size

@pytest.mark.parametrize("pool,expected", [(0, 0), (1, 1), (3, 3), (5, 5), (20, 5), (50, 5), (51, 6), (150, 15), (200, 20), (201, 21)])
def test_sample_is_ten_percent_with_a_floor_of_five(pool, expected):
    assert sample_audit.sample_size(pool) == expected


def test_only_questions_confirmed_without_an_edit_are_sampled(admin, db, make_paper):
    paper = fresh(db, make_paper, n=12)
    qs = sorted(db.query(models.Question).filter_by(paper_id=paper.id), key=lambda q: q.question_number)
    from app import versions
    for q in qs[:4]:                                            # these were edited by hand at some point
        versions.snapshot(db, q, None, "edited")
    qs[4].status = QStatus.NEEDS_REVIEW                         # not confirmed at all
    qs[5].status = QStatus.QUARANTINED
    db.commit()
    pool_ids = {q.id for q in sample_audit.unedited_confirmed(db, reload(db, paper))}
    assert pool_ids == {q.id for q in qs[6:]}


def test_a_re_read_snapshot_does_not_count_as_a_human_edit(admin, db, make_paper):
    from app import versions
    paper = fresh(db, make_paper, n=6)
    q = db.query(models.Question).filter_by(paper_id=paper.id).first()
    versions.snapshot(db, q, None, "pages 1-2 read again")
    db.commit()
    assert len(sample_audit.unedited_confirmed(db, reload(db, paper))) == 6


# --------------------------------------------------------------------------- the publish gate

def test_a_small_paper_needs_no_audit(admin, db, make_paper):
    paper = fresh(db, make_paper, n=4)
    assert sample_audit.blocker(db, paper) is None
    admin.post(f"/papers/{paper.id}/publish")
    assert reload(db, paper).publish_status == "published"


def test_publishing_is_blocked_until_the_audit_is_done(admin, db, make_paper):
    paper = fresh(db, make_paper, n=20)
    page = admin.get(f"/review/{paper.id}").text
    assert "an audit is needed before publishing" in page and "Run the sample audit first" in page
    assert "disabled" in page.split("Publish paper")[0].rsplit("<button", 1)[1]
    admin.post(f"/papers/{paper.id}/publish")
    assert reload(db, paper).publish_status == "draft"

    picked = start(admin, db, paper)
    assert len(picked) == 5
    admin.post(f"/papers/{paper.id}/publish")                   # still blocked while the audit is running
    assert reload(db, paper).publish_status == "draft"
    for q in picked:
        assert verdict(admin, paper, q, "ok").status_code == 303
    paper = reload(db, paper)
    assert paper.audit_state == "passed"
    admin.post(f"/papers/{paper.id}/publish")
    assert reload(db, paper).publish_status == "published"


def test_a_confirmed_question_with_unseen_warnings_blocks_publishing(admin, db, make_paper):
    paper = fresh(db, make_paper, n=4)
    q = db.query(models.Question).filter_by(paper_id=paper.id).first()
    q.ocr_flags, q.flags_acknowledged = "few_options", False
    db.commit()
    from app.practice import pool
    assert "flagged" in pool.publish_blockers(db, reload(db, paper))
    admin.post(f"/papers/{paper.id}/publish")
    assert reload(db, paper).publish_status == "draft"
    # Confirming it again, by hand, is what acknowledges the warning.
    admin.post(f"/review/{paper.id}/question/{q.id}/reopen")
    db.rollback()
    assert admin.post(f"/review/{paper.id}/question/{q.id}", data=question_form(db.get(models.Question, q.id))).status_code == 303
    admin.post(f"/papers/{paper.id}/publish")
    assert reload(db, paper).publish_status == "published"


# --------------------------------------------------------------------------- audit outcomes

def test_one_or_two_wrong_passes_but_sends_those_questions_back(admin, db, make_paper):
    paper = fresh(db, make_paper, n=20)
    picked = start(admin, db, paper)
    verdict(admin, paper, picked[0], "wrong")
    verdict(admin, paper, picked[1], "wrong")
    for q in picked[2:]:
        verdict(admin, paper, q, "ok")
    paper = reload(db, paper)
    assert paper.audit_state == "passed"
    assert numbers(db, paper, status=QStatus.NEEDS_REVIEW) == sorted([picked[0].question_number, picked[1].question_number])
    admin.post(f"/papers/{paper.id}/publish")                   # the two sent back still have to be confirmed
    assert reload(db, paper).publish_status == "draft"


def test_three_wrong_fails_the_audit_and_sends_every_unedited_question_back(admin, db, make_paper):
    from app import versions
    paper = fresh(db, make_paper, n=20)
    edited = db.query(models.Question).filter_by(paper_id=paper.id, question_number=1).one()
    versions.snapshot(db, edited, None, "fixed a typo")
    db.commit()
    picked = start(admin, db, paper)
    assert edited.question_number not in [q.question_number for q in picked]
    for q in picked[:3]:
        verdict(admin, paper, q, "wrong")
    for q in picked[3:]:
        verdict(admin, paper, q, "ok")
    paper = reload(db, paper)
    assert paper.audit_state == "failed"
    assert numbers(db, paper, status=QStatus.VERIFIED) == [1]                    # only the hand-edited one stays confirmed
    assert len(numbers(db, paper, status=QStatus.NEEDS_REVIEW)) == 19
    admin.post(f"/papers/{paper.id}/publish")
    assert reload(db, paper).publish_status == "draft"
    assert "failed" in admin.get(f"/review/{paper.id}/audit").text


def test_a_failed_audit_can_be_followed_by_a_second_pass(admin, db, make_paper):
    paper = fresh(db, make_paper, n=20)
    picked = start(admin, db, paper)
    for q in picked[:3]:
        verdict(admin, paper, q, "wrong")
    for q in picked[3:]:                                        # the audit is only decided once all five have verdicts
        verdict(admin, paper, q, "ok")
    paper = reload(db, paper)
    assert paper.audit_state == "failed" and paper.audit_round == 1
    # Second pass: confirm them again (by hand). That ends the failed state, but the paper still needs a NEW audit to publish.
    for q in db.query(models.Question).filter_by(paper_id=paper.id, status=QStatus.NEEDS_REVIEW).all():
        assert admin.post(f"/review/{paper.id}/question/{q.id}", data=question_form(q)).status_code == 303
    paper = reload(db, paper)
    assert paper.audit_state == "none"
    assert "Run the sample audit first" in sample_audit.blocker(db, paper)
    admin.post(f"/papers/{paper.id}/publish")
    assert reload(db, paper).publish_status == "draft"
    assert admin.post(f"/review/{paper.id}/audit/start").status_code == 303
    paper = reload(db, paper)
    assert paper.audit_state == "pending" and paper.audit_round == 2


def test_the_verdict_can_only_be_ok_or_wrong_and_only_for_picked_questions(admin, db, make_paper):
    paper = fresh(db, make_paper, n=20)
    picked = start(admin, db, paper)
    other = db.query(models.Question).filter_by(paper_id=paper.id, audit_pick=False).first()
    assert verdict(admin, paper, picked[0], "maybe").status_code == 400
    assert verdict(admin, paper, other, "ok").status_code == 400
    assert admin.post(f"/review/{paper.id}/audit/999999/check", data={"verdict": "ok"}).status_code == 404


def test_an_audit_cannot_be_started_twice_or_on_nothing(admin, db, make_paper):
    paper = fresh(db, make_paper, n=20)
    start(admin, db, paper)
    admin.post(f"/review/{paper.id}/audit/start")
    assert reload(db, paper).audit_round == 1
    empty = fresh(db, make_paper, n=6, status=QStatus.NEEDS_REVIEW)
    r = admin.post(f"/review/{empty.id}/audit/start")
    assert r.status_code == 303 and reload(db, empty).audit_state in (None, "none")


def test_the_pick_is_random_but_reproducible_with_a_seed(admin, db, make_paper):
    paper = fresh(db, make_paper, n=60)
    a = [q.id for q in sample_audit.start(db, reload(db, paper), random.Random(7))]
    db.rollback()
    b = [q.id for q in sample_audit.start(db, reload(db, paper), random.Random(7))]
    db.rollback()
    assert a == b and len(a) == 6


# --------------------------------------------------------------------------- a passed audit only vouches for what it saw

def _passed(admin, db, paper):
    for q in start(admin, db, paper):
        verdict(admin, paper, q, "ok")
    assert reload(db, paper).audit_state == "passed"


def test_reopening_a_question_makes_the_audit_stale(admin, db, make_paper):
    paper = fresh(db, make_paper, n=20)
    _passed(admin, db, paper)
    q = db.query(models.Question).filter_by(paper_id=paper.id, question_number=3).one()
    admin.post(f"/review/{paper.id}/question/{q.id}/reopen")
    assert reload(db, paper).audit_state == "none"


def test_confirming_another_question_makes_the_audit_stale(admin, db, make_paper):
    paper = fresh(db, make_paper, n=20)
    q = db.query(models.Question).filter_by(paper_id=paper.id, question_number=20).one()
    q.status = QStatus.NEEDS_REVIEW
    db.commit()
    _passed(admin, db, paper)
    assert admin.post(f"/review/{paper.id}/question/{q.id}", data=question_form(db.get(models.Question, q.id))).status_code == 303
    assert reload(db, paper).audit_state == "none"


def test_reopen_all_and_confirm_clean_make_the_audit_stale(admin, db, make_paper):
    paper = fresh(db, make_paper, n=20)
    _passed(admin, db, paper)
    admin.post(f"/review/{paper.id}/reopen-all")
    assert reload(db, paper).audit_state == "none"
    _ = numbers(db, paper)
    admin.post(f"/review/{paper.id}/confirm-clean")
    assert reload(db, paper).audit_state == "none"


# --------------------------------------------------------------------------- pages, permissions, log

def test_the_audit_page_shows_picked_questions_with_both_buttons(admin, db, make_paper):
    paper = fresh(db, make_paper, n=20)
    assert "Start audit (5 questions)" in admin.get(f"/review/{paper.id}/audit").text
    picked = start(admin, db, paper)
    page = admin.get(f"/review/{paper.id}/audit").text
    assert page.count("Matches the original") >= 5 and page.count("Doesn&#39;t match") + page.count("Doesn't match") >= 5
    assert f"Q{picked[0].question_number}" in page
    response = verdict(admin, paper, picked[0], "ok")
    assert response.headers["location"] == f"/review/{paper.id}/audit#q{picked[0].question_number}"
    assert "Matches the original</span>" in admin.get(f"/review/{paper.id}/audit").text


def test_the_audit_is_admin_only(admin, db, make_paper, anon):
    paper = fresh(db, make_paper, n=20)
    student = make_student_client(db, "auditstudent")
    q = db.query(models.Question).filter_by(paper_id=paper.id).first()
    for client, ok in ((student, 403), (anon, 303)):
        assert client.get(f"/review/{paper.id}/audit").status_code == ok
        assert client.post(f"/review/{paper.id}/audit/start").status_code == ok
        assert client.post(f"/review/{paper.id}/audit/{q.id}/check", data={"verdict": "ok"}).status_code == ok
        assert client.post(f"/review/{paper.id}/rerun", data={"first_page": "1"}).status_code == ok
    assert reload(db, paper).audit_state in (None, "none")


def test_every_audit_step_is_written_to_the_audit_log(admin, db, make_paper):
    paper = fresh(db, make_paper, n=20)
    for q in start(admin, db, paper):
        verdict(admin, paper, q, "ok")
    db.rollback()
    actions = [a.action for a in db.query(models.AuditLog).filter_by(paper_id=paper.id)]
    assert actions.count("sample_audit.start") == 1 and actions.count("sample_audit.check") == 5
    assert actions.count("sample_audit.result") == 1


# --------------------------------------------------------------------------- the review screen

def test_the_review_page_says_how_many_were_extracted_and_which_are_missing(admin, db, make_paper):
    paper = fresh(db, make_paper, n=10, status=QStatus.NEEDS_REVIEW, expected_total=10)
    for q in db.query(models.Question).filter(models.Question.paper_id == paper.id, models.Question.question_number.in_([4, 7])):
        db.delete(q)
    db.commit()
    page = admin.get(f"/review/{paper.id}").text
    assert "Extracted 8 of 10" in page and "missing: 4, 7" in page


def test_without_an_expected_total_gaps_below_the_highest_number_still_show(admin, db, make_paper):
    paper = fresh(db, make_paper, n=10, status=QStatus.NEEDS_REVIEW)
    db.query(models.Question).filter_by(paper_id=paper.id, question_number=3).delete()
    db.commit()
    page = admin.get(f"/review/{paper.id}").text
    assert "Extracted 9 of 10" in page and "missing: 3" in page and "highest number seen" in page


def test_a_complete_paper_says_nothing_is_missing(admin, db, make_paper):
    paper = fresh(db, make_paper, n=6, status=QStatus.NEEDS_REVIEW, expected_total=6)
    assert "Extracted 6 of 6" in admin.get(f"/review/{paper.id}").text
    assert "none missing" in admin.get(f"/review/{paper.id}").text


def test_flagged_questions_come_first_when_reviewing_and_the_order_can_be_switched(admin, db, make_paper):
    paper = fresh(db, make_paper, n=6, status=QStatus.NEEDS_REVIEW)
    for q in db.query(models.Question).filter(models.Question.paper_id == paper.id, models.Question.question_number.in_([5, 3])):
        q.ocr_flags = "few_options"
    db.commit()

    def order(url):
        text = admin.get(url).text
        return [n for n in sorted(range(1, 7), key=lambda n: text.find(f'id="q{n}"'))]

    assert order(f"/review/{paper.id}?show=to_confirm") == [3, 5, 1, 2, 4, 6]
    assert order(f"/review/{paper.id}?show=to_confirm&sort=number") == [1, 2, 3, 4, 5, 6]
    assert order(f"/review/{paper.id}?show=all") == [1, 2, 3, 4, 5, 6]


def test_answers_are_prefilled_and_nothing_is_confirmed_by_looking_at_the_page(admin, db, make_paper):
    paper = fresh(db, make_paper, n=4, status=QStatus.NEEDS_REVIEW)
    page = admin.get(f"/review/{paper.id}").text
    assert page.count('<option value="A" selected>') >= 1
    assert numbers(db, paper, status=QStatus.NEEDS_REVIEW) == [1, 2, 3, 4]


def test_the_review_page_shows_the_audit_panel(admin, db, make_paper):
    paper = fresh(db, make_paper, n=20)
    assert "Sample audit:" in admin.get(f"/review/{paper.id}").text
    pass_audit(db, paper)
    assert "passed" in admin.get(f"/review/{paper.id}").text.split("Sample audit:")[1][:80]


# --------------------------------------------------------------------------- re-run pages

@pytest.fixture()
def read_paper(db, tmp_path):
    """A paper genuinely read from a generated text PDF (cover page + question pages)."""
    def factory(count=24):
        path = tmp_path / f"rerun{next(_run)}.pdf"
        path.write_bytes(paper_pdf(count=count))
        paper = models.Paper(title=f"Rerun paper {next(_run)}", exam_type=models.ExamType.full_length, status="processing",
                             source_pdf_path=str(path), expected_total=count)
        db.add(paper)
        db.commit()
        ingest.process_paper(paper.id)
        db.rollback()
        paper = db.get(models.Paper, paper.id)
        assert paper.status == "ready" and len(paper.questions) == count
        return paper
    return factory


def _by_number(db, paper):
    db.rollback()
    db.expire_all()
    return {q.question_number: q for q in db.query(models.Question).filter_by(paper_id=paper.id)}


def test_rerun_replaces_only_unconfirmed_unedited_questions_on_those_pages(admin, db, read_paper):
    paper = read_paper()
    qs = _by_number(db, paper)
    on_page = sorted(n for n, q in qs.items() if q.page_number == 2)
    elsewhere = sorted(n for n, q in qs.items() if q.page_number != 2)
    assert len(on_page) >= 4 and elsewhere
    original = {n: qs[n].text for n in qs}

    edited, confirmed, garbled, missing = on_page[0], on_page[1], on_page[2], on_page[3]
    qs[edited].status = QStatus.VERIFIED
    qs[edited].text = "MY OWN CORRECTION"
    from app import versions
    versions.snapshot(db, qs[edited], None, "typo fixed")
    qs[confirmed].status = QStatus.VERIFIED
    qs[garbled].text = "GARBLED"
    db.delete(qs[missing])
    outside = elsewhere[-1]
    qs[outside].text = "OUTSIDE THE RANGE"
    db.commit()

    ingest.rerun_pages(paper.id, 2, 2)
    qs = _by_number(db, paper)
    assert qs[edited].text == "MY OWN CORRECTION" and qs[edited].status == QStatus.VERIFIED       # protected: edited
    assert qs[confirmed].text == original[confirmed] and qs[confirmed].status == QStatus.VERIFIED  # protected: confirmed
    assert qs[garbled].text == original[garbled] and qs[garbled].status == QStatus.NEEDS_REVIEW    # replaced
    assert qs[missing].text == original[missing] and qs[missing].status == QStatus.NEEDS_REVIEW    # a missing number is added
    assert qs[outside].text == "OUTSIDE THE RANGE"                                                 # other pages untouched
    versions_ = db.query(models.QuestionVersion).filter_by(question_id=qs[garbled].id).all()
    assert any(v.reason == "pages 2-2 read again" and "GARBLED" in v.snapshot_json for v in versions_)

    paper = reload(db, paper)
    assert paper.status == "ready"
    replaced = len(on_page) - 3                                 # every other waiting question on the page is read again too
    assert f"{replaced} question(s) replaced, 1 added, 2 left alone" in paper.status_message
    log = db.query(models.AuditLog).filter_by(paper_id=paper.id, action="paper.rerun_pages").one()
    assert f'"replaced": {replaced}' in log.detail_json and '"protected": 2' in log.detail_json


def test_a_replaced_question_keeps_the_answer_it_already_had(admin, db, read_paper):
    paper = read_paper()
    qs = _by_number(db, paper)
    n = min(n for n, q in qs.items() if q.page_number == 2)
    qs[n].correct_answer, qs[n].answer_source = "D", "key_pdf"
    qs[n].text = "GARBLED"
    db.commit()
    ingest.rerun_pages(paper.id, 2, 3)
    q = _by_number(db, paper)[n]
    assert q.text != "GARBLED" and q.correct_answer == "D" and q.answer_source == "key_pdf"


def test_a_replaced_question_takes_a_printed_answer_when_it_had_none(admin, db, read_paper):
    paper = read_paper()
    qs = _by_number(db, paper)
    n = min(n for n, q in qs.items() if q.page_number == 2)
    qs[n].correct_answer, qs[n].answer_source = None, None
    db.commit()
    ingest.rerun_pages(paper.id, 2, 2)
    q = _by_number(db, paper)[n]
    assert q.correct_answer == "ABCD"[(n - 1) % 4] and q.answer_source == "inline"


def test_the_rerun_route_validates_its_input_and_runs(admin, db, read_paper):
    paper = read_paper()
    total = paper.pages_total
    assert total >= 3
    for bad in ({"first_page": "x"}, {"first_page": "0"}, {"first_page": "3", "last_page": "2"}, {"first_page": "1", "last_page": str(total + 1)}):
        r = admin.post(f"/review/{paper.id}/rerun", data=bad)
        assert r.status_code == 303
        assert reload(db, paper).status == "ready"
    assert db.query(models.AuditLog).filter_by(paper_id=paper.id, action="paper.rerun_start").count() == 0
    q = _by_number(db, paper)[min(n for n, q in _by_number(db, paper).items() if q.page_number == 2)]
    q.text = "GARBLED"
    db.commit()
    assert admin.post(f"/review/{paper.id}/rerun", data={"first_page": "2", "last_page": "2"}).status_code == 303
    paper = reload(db, paper)
    assert paper.status == "ready" and "read again" in paper.status_message
    assert _by_number(db, paper)[q.question_number].text != "GARBLED"
    assert db.query(models.AuditLog).filter_by(paper_id=paper.id, action="paper.rerun_start").count() == 1


def test_the_rerun_route_refuses_a_paper_with_no_pdf(admin, db, make_paper):
    paper = fresh(db, make_paper, n=4, status=QStatus.NEEDS_REVIEW)
    assert admin.post(f"/review/{paper.id}/rerun", data={"first_page": "1"}).status_code == 303
    assert reload(db, paper).status == "ready"
    assert "Read pages again" not in admin.get(f"/review/{paper.id}").text


def test_a_failed_rerun_changes_nothing_and_leaves_the_paper_usable(admin, db, read_paper, monkeypatch):
    paper = read_paper()
    before = {n: q.text for n, q in _by_number(db, paper).items()}

    def boom(*a, **k):
        raise RuntimeError("PDF exploded")
    monkeypatch.setattr(ingest, "read_questions", boom)
    ingest.rerun_pages(paper.id, 1, 5)
    paper = reload(db, paper)
    assert paper.status == "ready" and "failed" in paper.status_message and "Nothing was changed" in paper.status_message
    assert {n: q.text for n, q in _by_number(db, paper).items()} == before


def test_the_review_page_offers_the_rerun_form_for_a_paper_with_a_pdf(admin, db, read_paper):
    paper = read_paper()
    page = admin.get(f"/review/{paper.id}").text
    assert "Read pages again" in page and f'action="/review/{paper.id}/rerun"' in page
    assert "class=\"review-split\"" in page                     # pictures sit beside the form
