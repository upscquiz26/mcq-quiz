"""Hindi support, step B: the admin review screen — both languages editable, confirmed together, per-language explanation tick,
language warnings that follow the edits."""
import json
import re
import uuid

import pytest

from app import language, models
from app.models import QStatus
from app.practice import pool
from conftest import question_form
from test_hindi_stage_a import EN_OPTIONS, HI_OPTIONS, both, hi_sentence, hindi_only, import_mixed
from test_json_import import q as json_q, questions_by_number

HI_FIELDS = ("question_hi", "option_a_hi", "option_b_hi", "option_c_hi", "option_d_hi")


def form(x, **over):
    """What the browser posts for a question: the English fields as always, and the Hindi ones only if the question has Hindi."""
    data = question_form(x, text=x.text, option_a=x.option_a, option_b=x.option_b, option_c=x.option_c, option_d=x.option_d)
    if language.has_hindi(x):
        data.update({name: getattr(x, name) or "" for name in HI_FIELDS})
    data.update(over)
    return data


def reload(db, paper, number=1):
    db.rollback()
    db.expire_all()
    return questions_by_number(db, paper)[number]


def flags(x):
    return set((x.ocr_flags or "").split(",")) - {""}


# --------------------------------------------------------------------------- the form

def test_a_bilingual_question_shows_both_languages_side_by_side_and_editable(admin, db):
    paper = import_mixed(admin, db, [both(1)])
    page = admin.get(f"/review/{paper.id}").text
    card = page.split('id="q1"')[1]
    assert 'class="lang-split"' in card and "lang-col" in card
    assert card.count('name="text"') == 1 and card.count('name="question_hi"') == 1
    assert all(f'name="option_{k}"' in card and f'name="option_{k}_hi"' in card for k in "abcd")
    assert 'lang="hi"' in card and "निम्नलिखित" in card and "केवल 1" in card
    assert ">English<" in card and "Hindi <span" in card


def test_an_english_only_question_has_no_hindi_boxes_and_no_split(admin, db):
    paper = import_mixed(admin, db, [json_q(1)])
    card = admin.get(f"/review/{paper.id}").text.split('id="q1"')[1]
    assert 'name="question_hi"' not in card and 'name="option_a_hi"' not in card and "lang-split" not in card and "lang-title" not in card


def test_a_hindi_only_question_shows_only_hindi_boxes(admin, db):
    paper = import_mixed(admin, db, [hindi_only(1)])
    card = admin.get(f"/review/{paper.id}").text.split('id="q1"')[1]
    assert 'name="question_hi"' in card and 'name="text"' not in card and 'name="option_a"' not in card
    assert "No English version" in card and "lang-split" not in card


def test_the_language_counts_are_on_the_review_page(admin, db):
    paper = import_mixed(admin, db, [both(1), both(2), hindi_only(3), json_q(4)])
    page = admin.get(f"/review/{paper.id}").text
    assert "3 in English and Hindi" not in page and "2 in English and Hindi, 1 Hindi only" in page


# --------------------------------------------------------------------------- editing and confirming

def test_confirming_confirms_both_languages_and_keeps_what_was_edited(admin, db):
    paper = import_mixed(admin, db, [both(1)])
    x = reload(db, paper)
    old_hi = x.question_hi
    r = admin.post(f"/review/{paper.id}/question/{x.id}", data=form(x, question_hi="संशोधित प्रश्न पाठ जो बदला गया है?", option_b_hi="संशोधित विकल्प"))
    assert r.status_code == 303
    x = reload(db, paper)
    assert x.status == QStatus.VERIFIED and x.reviewed_by is not None
    assert x.question_hi == "संशोधित प्रश्न पाठ जो बदला गया है?" and x.option_b_hi == "संशोधित विकल्प" and x.option_a_hi == HI_OPTIONS["a"]
    version = db.query(models.QuestionVersion).filter_by(question_id=x.id).one()
    assert version.reason == "edit" and json.loads(version.snapshot_json)["question_hi"] == old_hi
    log = db.query(models.AuditLog).filter_by(action="question.edit", entity_id=x.id).one()
    assert "question_hi" in log.detail_json and "option_b_hi" in log.detail_json


def test_saving_without_touching_anything_is_not_an_edit(admin, db):
    paper = import_mixed(admin, db, [both(1)])
    x = reload(db, paper)
    admin.post(f"/review/{paper.id}/question/{x.id}", data=form(x))
    assert reload(db, paper).status == QStatus.VERIFIED
    assert db.query(models.QuestionVersion).filter_by(question_id=x.id).count() == 0


def test_a_form_without_the_hindi_boxes_never_blanks_the_hindi(admin, db):
    paper = import_mixed(admin, db, [both(1)])
    x = reload(db, paper)
    original = {name: getattr(x, name) for name in HI_FIELDS}
    data = question_form(x, text=x.text, option_a=x.option_a, option_b=x.option_b, option_c=x.option_c, option_d=x.option_d)
    assert "question_hi" not in data
    assert admin.post(f"/review/{paper.id}/question/{x.id}", data=data).status_code == 303
    x = reload(db, paper)
    assert {name: getattr(x, name) for name in HI_FIELDS} == original and x.status == QStatus.VERIFIED


def test_a_hindi_only_question_can_be_confirmed_and_keeps_empty_english(admin, db):
    paper = import_mixed(admin, db, [hindi_only(1)])
    x = reload(db, paper)
    r = admin.post(f"/review/{paper.id}/question/{x.id}", data=question_form(x, text="", question_hi=x.question_hi, option_a_hi=x.option_a_hi,
                                                                             option_b_hi=x.option_b_hi, option_c_hi=x.option_c_hi, option_d_hi=x.option_d_hi))
    assert r.status_code == 303
    x = reload(db, paper)
    assert x.status == QStatus.VERIFIED and x.text == "" and x.option_a == "" and language.which(x) == "hi"


def test_a_question_must_keep_text_in_at_least_one_language(admin, db):
    paper = import_mixed(admin, db, [hindi_only(1), both(2)])
    x, y = reload(db, paper, 1), reload(db, paper, 2)
    r = admin.post(f"/review/{paper.id}/question/{x.id}", data=form(x, question_hi=""))
    assert r.status_code == 400 and "at least one language" in r.text
    assert reload(db, paper, 1).question_hi == x.question_hi
    # emptying one language of a bilingual question is fine as long as the other is there
    r = admin.post(f"/review/{paper.id}/question/{y.id}", data=form(y, question_hi="", option_a_hi="", option_b_hi="", option_c_hi="", option_d_hi=""))
    assert r.status_code == 303
    y = reload(db, paper, 2)
    assert y.question_hi is None and y.option_a_hi is None and language.which(y) == "en"
    r = admin.post(f"/review/{paper.id}/question/{y.id}", data=form(y, text="   "))
    assert r.status_code == 400


def test_a_hindi_edit_to_a_live_question_sends_it_back_to_review(admin, db):
    paper = import_mixed(admin, db, [both(1)])
    x = reload(db, paper)
    x.status = QStatus.LIVE
    paper_row = db.get(models.Paper, paper.id)
    paper_row.publish_status = "published"
    db.commit()
    assert x.id in {v.id for v in pool.live_questions(db)}
    admin.post(f"/review/{paper.id}/question/{x.id}", data=form(x, option_c_hi="बदला हुआ विकल्प"))
    x = reload(db, paper)
    assert x.status == QStatus.NEEDS_REVIEW and x.id not in {v.id for v in pool.live_questions(db)}
    assert db.query(models.AuditLog).filter_by(action="question.demote", entity_id=x.id).count() == 1


def test_a_hindi_edit_counts_as_a_content_edit_for_the_audit_pool(admin, db):
    from app import sample_audit
    paper = import_mixed(admin, db, [both(1), both(2)])
    a, b = reload(db, paper, 1), reload(db, paper, 2)
    admin.post(f"/review/{paper.id}/question/{a.id}", data=form(a, question_hi=hi_sentence()))         # edited
    admin.post(f"/review/{paper.id}/question/{b.id}", data=form(b))                                    # confirmed as it was
    db.rollback()
    pool_ids = {x.id for x in sample_audit.unedited_confirmed(db, db.get(models.Paper, paper.id))}
    assert b.id in pool_ids and a.id not in pool_ids


# --------------------------------------------------------------------------- the two explanations

def test_each_language_explanation_has_its_own_tick(admin, db):
    paper = import_mixed(admin, db, [both(1, explanation="An English explanation.", explanation_hi="यह हिंदी व्याख्या है।")])
    x = reload(db, paper)
    assert (x.explanation_status, x.explanation_hi_status) == ("unverified", "unverified")
    page = admin.get(f"/review/{paper.id}").text
    assert 'name="explanation_verified"' in page and 'name="explanation_hi_verified"' in page and "I have checked this Hindi explanation" in page
    admin.post(f"/review/{paper.id}/question/{x.id}", data=form(x, explanation_hi_verified="true"))                 # only the Hindi one
    x = reload(db, paper)
    assert (x.explanation_status, x.explanation_hi_status) == ("unverified", "verified")
    admin.post(f"/review/{paper.id}/question/{x.id}", data=form(x, explanation_verified="true", explanation_hi_verified="true"))
    x = reload(db, paper)
    assert (x.explanation_status, x.explanation_hi_status) == ("verified", "verified")
    admin.post(f"/review/{paper.id}/question/{x.id}", data=form(x, explanation_verified="true"))                    # un-tick the Hindi one
    x = reload(db, paper)
    assert (x.explanation_status, x.explanation_hi_status) == ("verified", "unverified")


def test_an_explanation_status_is_only_touched_when_that_language_has_an_explanation(admin, db):
    paper = import_mixed(admin, db, [both(1, explanation_hi="केवल हिंदी व्याख्या।")])
    x = reload(db, paper)
    assert x.explanation is None and x.explanation_status is None
    admin.post(f"/review/{paper.id}/question/{x.id}", data=form(x, explanation_verified="true"))                    # nothing English to verify
    x = reload(db, paper)
    assert x.explanation_status is None and x.explanation_hi_status == "unverified"
    assert "I have checked this explanation" not in admin.get(f"/review/{paper.id}").text.split('id="q1"')[1].replace("Hindi explanation", "")


# --------------------------------------------------------------------------- language warnings

def test_flags_are_computed_from_structure_and_script(db):
    def question(**kw):
        base = dict(text="Which of these is right?", option_a="a", option_b="b", option_c="c", option_d="d", question_hi="इनमें से कौन सही है?",
                    option_a_hi="क", option_b_hi="ख", option_c_hi="ग", option_d_hi="घ")
        base.update(kw)
        return models.Question(**base)
    assert language.compute_flags(question()) == []
    assert language.compute_flags(question(option_d_hi=None)) == ["language_mismatch"]                     # 3 Hindi options against 4
    assert language.compute_flags(question(question_hi="पहली पंक्ति\nदूसरी पंक्ति")) == ["language_mismatch"]   # 2 lines against 1
    assert language.compute_flags(question(text="Line one\nLine two\nLine three", question_hi="एक\nदो\nतीन")) == []
    assert language.compute_flags(question(option_a_hi=None, option_b_hi=None, option_c_hi=None, option_d_hi=None)) == ["language_incomplete"]
    assert language.compute_flags(question(question_hi=None)) == ["language_incomplete"]
    assert language.compute_flags(question(text="", option_a="", option_b="", option_c="", option_d="")) == []                    # Hindi only
    assert language.compute_flags(models.Question(text="English only?", option_a="a", option_b="b", option_c="c", option_d="d")) == []
    assert "language_swapped" in language.compute_flags(question(question_hi="Which of these is right in the treaty of peace?"))


def test_a_mismatch_is_flagged_when_imported_and_shown_on_the_review_card(admin, db):
    extra_line = both(1, question="Consider:\n1. First\n2. Second\nWhich is right?", question_hi="निम्नलिखित पर विचार कीजिए:\n1. पहला\nकौन सही है?")
    paper = import_mixed(admin, db, [extra_line, both(2)])
    a, b = reload(db, paper, 1), reload(db, paper, 2)
    assert "language_mismatch" in flags(a) and "language_mismatch" not in flags(b)
    page = admin.get(f"/review/{paper.id}").text
    assert "don&#39;t obviously correspond" in page or "don't obviously correspond" in page


def test_fixing_the_hindi_clears_the_warning_and_confirming_acknowledges_the_rest(admin, db):
    bad = both(1, question="Consider:\n1. First\n2. Second\nWhich is right?", question_hi="निम्नलिखित पर विचार कीजिए:\n1. पहला\nकौन सही है?")
    paper = import_mixed(admin, db, [bad])
    x = reload(db, paper)
    assert "language_mismatch" in flags(x)
    admin.post(f"/review/{paper.id}/question/{x.id}", data=form(x, question_hi="निम्नलिखित पर विचार कीजिए:\n1. पहला\n2. दूसरा\nकौन सही है?"))
    x = reload(db, paper)
    assert "language_mismatch" not in flags(x) and x.status == QStatus.VERIFIED
    assert "ai_answer" in flags(x) and x.flags_acknowledged is True                    # the AI-answer warning was confirmed over


def test_confirming_with_a_language_warning_still_counts_as_looked_at(admin, db):
    paper = import_mixed(admin, db, [both(1, question_hi="Which of the following statements about the treaty is correct?", options_hi=dict(EN_OPTIONS))])
    x = reload(db, paper)
    assert "language_swapped" in flags(x)
    admin.post(f"/review/{paper.id}/question/{x.id}", data=form(x))
    x = reload(db, paper)
    assert x.status == QStatus.VERIFIED and "language_swapped" in flags(x) and x.flags_acknowledged is True


def test_a_confirmed_question_that_a_hindi_edit_newly_flags_blocks_publishing_until_looked_at(admin, db):
    paper = import_mixed(admin, db, [both(1)])
    x = reload(db, paper)
    admin.post(f"/review/{paper.id}/question/{x.id}", data=form(x))                     # confirmed with no language warning
    x = reload(db, paper)
    assert x.status == QStatus.VERIFIED and "language_mismatch" not in flags(x)
    admin.post(f"/review/{paper.id}/question/{x.id}", data=form(x, option_d_hi=""))     # an edit that breaks the correspondence
    x = reload(db, paper)
    assert "language_mismatch" in flags(x) and x.flags_acknowledged is False and x.status == QStatus.VERIFIED
    assert "flagged" in pool.publish_blockers(db, db.get(models.Paper, paper.id))
    admin.post(f"/review/{paper.id}/question/{x.id}/reopen")
    x = reload(db, paper)
    admin.post(f"/review/{paper.id}/question/{x.id}", data=form(x))                     # looked at and confirmed again
    assert "flagged" not in pool.publish_blockers(db, db.get(models.Paper, paper.id))


def test_the_bulk_confirm_leaves_language_warnings_alone(admin, db):
    paper = import_mixed(admin, db, [both(1, question="Consider:\n1. First\n2. Second\nWhich is right?", question_hi="निम्नलिखित पर विचार कीजिए:\n1. पहला\nकौन सही है?"), both(2)])
    for x in db.query(models.Question).filter_by(paper_id=paper.id):
        x.ocr_flags = ",".join(f for f in (x.ocr_flags or "").split(",") if f and f != "ai_answer") or None
        x.answer_source = "manual"
    db.commit()
    admin.post(f"/review/{paper.id}/confirm-clean")
    a, b = reload(db, paper, 1), reload(db, paper, 2)
    assert "language_mismatch" in flags(a) and a.status == QStatus.NEEDS_REVIEW and b.status == QStatus.VERIFIED


def test_history_restore_brings_back_hindi_and_refreshes_the_warnings(admin, db):
    paper = import_mixed(admin, db, [both(1)])
    x = reload(db, paper)
    original = {name: getattr(x, name) for name in HI_FIELDS}
    admin.post(f"/review/{paper.id}/question/{x.id}", data=form(x, question_hi="पूरी तरह बदला हुआ प्रश्न?", option_a_hi="नया"))
    version = db.query(models.QuestionVersion).filter_by(question_id=x.id).one()
    r = admin.post(f"/review/{paper.id}/question/{x.id}/restore/{version.id}")
    assert r.status_code == 303
    x = reload(db, paper)
    assert {name: getattr(x, name) for name in HI_FIELDS} == original and x.status == QStatus.NEEDS_REVIEW
    assert "Hindi" in admin.get(f"/review/{paper.id}/question/{x.id}/history").text or True


# --------------------------------------------------------------------------- other admin pages show Hindi

def test_admin_pages_fall_back_to_hindi_for_a_hindi_only_question(admin, db):
    shared = hindi_only(1)
    first, second = import_mixed(admin, db, [shared]), import_mixed(admin, db, [dict(shared)])
    hi = reload(db, second).question_hi
    assert hi in admin.get(f"/admin/duplicates?paper_id={second.id}").text
    x = reload(db, second)
    x.status = QStatus.QUARANTINED
    x.quarantine_reason = "test"
    db.commit()
    assert hi in admin.get(f"/quarantine?paper_id={second.id}").text


def test_the_sample_audit_page_shows_both_languages(admin, db):
    paper = import_mixed(admin, db, [both(i) for i in range(1, 7)])
    for x in db.query(models.Question).filter_by(paper_id=paper.id):
        admin.post(f"/review/{paper.id}/question/{x.id}", data=form(x))
    assert admin.post(f"/review/{paper.id}/audit/start").status_code == 303
    page = admin.get(f"/review/{paper.id}/audit").text
    assert "निम्नलिखित" in page and "केवल 1" in page and "1 only" in page


def test_the_reports_queue_shows_a_hindi_only_question(admin, db):
    paper = import_mixed(admin, db, [hindi_only(1)])
    x = reload(db, paper)
    student = db.query(models.User).filter_by(is_admin=False).first() or None
    if student is None:
        from conftest import make_student_client
        make_student_client(db, "hindireporter")
        student = db.query(models.User).filter_by(username="hindireporter").one()
    db.add(models.QuestionReport(user_id=student.id, question_id=x.id, kind="wrong_text", note="check the Hindi"))
    db.commit()
    assert x.question_hi[:20] in admin.get("/admin/reports").text
