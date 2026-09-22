"""Hindi support, step A: the data model and the JSON import (schema 2, validation, prompt, template, duplicates)."""
import json
import random
import re
import uuid

import pytest
from sqlalchemy import inspect

from app import duplicates, json_import as ji, language, models, versions
from app.database import engine
from app.models import QStatus
from conftest import make_student_client
from test_json_import import (SUBJECTS, apply, doc, errors, paper_by_title, q, questions_by_number, report, token_of, validate, warnings)

HI_WORDS = ("भारत संविधान अनुच्छेद राष्ट्रपति संसद न्यायालय नदी पर्वत सम्राट साम्राज्य व्यापार कृषि जलवायु वन्यजीव अर्थव्यवस्था बैंक "
            "कर बजट विज्ञान प्रौद्योगिकी उपग्रह इतिहास भूगोल राजनीति समाज संस्कृति भाषा आयोग समिति योजना नीति अधिनियम विधेयक राज्य केंद्र "
            "चुनाव नागरिक अधिकार कर्तव्य").split()
HI_OPTIONS = {"a": "केवल 1", "b": "केवल 2", "c": "1 और 2 दोनों", "d": "न तो 1 और न ही 2"}
EN_OPTIONS = {"a": "1 only", "b": "2 only", "c": "Both 1 and 2", "d": "Neither 1 nor 2"}


def hi_sentence():
    """A Hindi question stem built from random words, so two of them are never mistaken for each other."""
    return "निम्नलिखित में से " + " ".join(random.Random(uuid.uuid4().hex).sample(HI_WORDS, 10)) + " के बारे में कौन-सा कथन सही है?"


def both(n, **kw):
    d = q(n, options=dict(EN_OPTIONS))
    d.update(question_hi=hi_sentence(), options_hi=dict(HI_OPTIONS))
    d.update(kw)
    return d


def hindi_only(n, **kw):
    d = q(n, question=None, options=None)
    d.update(question_hi=hi_sentence(), options_hi=dict(HI_OPTIONS))
    d.update(kw)
    return d


def one(item, **kw):
    r = report(doc([item]), **kw)
    return r, (r.questions.get(item["number"]))


# --------------------------------------------------------------------------- the schema

def test_the_new_columns_exist():
    inspector = inspect(engine)
    questions = {c["name"] for c in inspector.get_columns("questions")}
    assert {"question_hi", "option_a_hi", "option_b_hi", "option_c_hi", "option_d_hi", "explanation_hi", "explanation_hi_status"} <= questions
    assert "language" in {c["name"] for c in inspector.get_columns("users")}
    assert "language" in {c["name"] for c in inspector.get_columns("question_reports")}


def test_users_start_in_english(db):
    user = models.User(username=f"hi_{uuid.uuid4().hex[:8]}", password_hash="x", is_admin=False, status=models.UserStatus.approved)
    db.add(user)
    db.commit()
    db.refresh(user)
    assert user.language == "en"


# --------------------------------------------------------------------------- script and language helpers

def test_devanagari_share():
    assert language.devanagari_share("भारत का संविधान") == 1.0
    assert language.devanagari_share("The Constitution of India") == 0.0
    mixed = language.devanagari_share("भारत के संविधान का Article 21 और Article 32")
    assert 0.3 < mixed < 0.8
    assert language.devanagari_share("1 or 2") is None and language.devanagari_share("") is None and language.devanagari_share("१२३।") is None


def test_which_language_a_question_has(db):
    both_q = models.Question(text="English?", question_hi="हिंदी?", option_a_hi="क")
    assert language.which(both_q) == "both" and language.primary(both_q)[0] == "en"
    hindi = models.Question(text="", question_hi="हिंदी?", option_a_hi="क", option_b_hi="ख", option_c_hi="ग", option_d_hi="घ")
    assert language.which(hindi) == "hi" and language.primary(hindi) == ("hi", "हिंदी?", ["क", "ख", "ग", "घ"])
    assert language.which(models.Question(text="English only?")) == "en"


# --------------------------------------------------------------------------- validating both languages

def test_a_question_in_both_languages_is_read():
    r, item = one(both(1))
    assert r.ok and not r.warnings
    assert item["text"] and item["options"] == EN_OPTIONS and item["text_hi"].startswith("निम्नलिखित") and item["options_hi"] == HI_OPTIONS
    assert item["lang_flags"] == [] and item["explanation_hi"] is None


def test_english_only_files_import_exactly_as_before():
    r = report(doc([q(1), q(2)]))                                            # schema_version 1, no Hindi anywhere
    assert r.ok and not r.warnings
    assert all(not v["text_hi"] and v["options_hi"] == {} and v["lang_flags"] == [] for v in r.questions.values())


def test_a_version_1_file_that_carries_hindi_fields_still_has_them_read():
    body = json.loads(doc([both(1)]))
    assert body["schema_version"] == 1
    r = report(json.dumps(body))
    assert r.ok and r.questions[1]["text_hi"]


def test_a_hindi_only_question_is_accepted():
    r, item = one(hindi_only(1))
    assert r.ok and item["text"] == "" and item["options"] == {} and item["text_hi"] and len(item["options_hi"]) == 4
    assert language.devanagari_share(item["text_hi"]) > 0.9


def test_empty_english_fields_on_a_hindi_only_question_are_tolerated():
    item = hindi_only(1)
    item["question"] = ""
    item["options"] = {"a": "", "b": None, "c": "", "d": ""}
    r, got = one(item)
    assert r.ok and not r.warnings and got["options"] == {}


def test_a_question_with_no_text_in_either_language_is_refused():
    r, _ = one(q(1, question=None, options=None))
    assert not r.ok and any("\"question\" is missing or empty" in e for e in errors(r))
    r, _ = one(q(1, question="", question_hi=""))
    assert any("\"question\" is missing or empty" in e for e in errors(r))


def test_a_question_without_a_complete_option_set_in_any_language_is_refused():
    r, _ = one(q(1, options=None, question_hi=hi_sentence()))                                   # Hindi text but no options anywhere
    assert not r.ok and any("\"options\" must be an object" in e for e in errors(r))
    r, _ = one(hindi_only(1, options_hi=None))
    assert not r.ok and any("\"options\" must be an object" in e for e in errors(r))


# --------------------------------------------------------------------------- the Hindi options

@pytest.mark.parametrize("change, expected", [
    ({"options_hi": ["क", "ख", "ग", "घ"]}, "\"options_hi\" must be an object with the keys a, b, c and d"),
    ({"options_hi": {"a": "केवल 1", "b": "केवल 2", "c": "1 और 2 दोनों"}}, "Missing Hindi option d"),
    ({"options_hi": {**HI_OPTIONS, "e": "पाँच"}}, "Unexpected Hindi option key e"),
    ({"options_hi": {"क": "केवल 1", "ख": "केवल 2", "ग": "दोनों", "घ": "कोई नहीं"}}, "Unexpected Hindi option keys"),
    ({"options_hi": {**HI_OPTIONS, "b": ""}}, "Hindi option b is empty"),
    ({"options_hi": {"a": "केवल 1", "A": "फिर", "b": "x", "c": "y", "d": "z"}}, "differ only by case"),
    ({"explanation_hi": 5}, "\"explanation_hi\" must be text or null"),
    ({"question_hi": 5}, "\"question_hi\" must be text or null"),
])
def test_hindi_fields_are_held_to_the_same_rules_as_english_ones(change, expected):
    r, _ = one(both(1, **change))
    assert not r.ok and any(expected in e for e in errors(r)), errors(r)


def test_a_very_long_hindi_option_is_warned_about():
    r, _ = one(both(1, options_hi={**HI_OPTIONS, "a": "भारत " * 80}))
    assert r.ok and any("Hindi option a is very long" in w for w in warnings(r))


# --------------------------------------------------------------------------- an incomplete language is flagged, not refused

def test_hindi_text_without_hindi_options_is_importable_but_flagged():
    r, item = one(q(1, question_hi=hi_sentence()))
    assert r.ok and item["lang_flags"] == ["language_incomplete"]
    assert any("Hindi question text without its options" in w for w in warnings(r))


def test_hindi_options_without_hindi_text_are_flagged_too():
    r, item = one(q(1, options_hi=dict(HI_OPTIONS)))
    assert r.ok and item["lang_flags"] == ["language_incomplete"]
    assert any("Hindi options without a question text" in w for w in warnings(r))


def test_english_text_without_english_options_is_flagged_when_hindi_is_complete():
    r, item = one(hindi_only(1, question="A stray English stem that has no options at all?"))
    assert r.ok and item["lang_flags"] == ["language_incomplete"] and item["options"] == {}


def test_a_complete_language_pair_or_a_single_language_gets_no_flag():
    for item in (both(1), hindi_only(1), q(1)):
        r, got = one(item)
        assert got["lang_flags"] == [] and not r.warnings


# --------------------------------------------------------------------------- the script check

def test_swapped_languages_are_flagged_not_blocked():
    swapped = q(1, question=hi_sentence(), options=dict(HI_OPTIONS), question_hi="Which of the following statements about the treaty is correct?",
                options_hi=dict(EN_OPTIONS))
    r, item = one(swapped)
    assert r.ok and item["lang_flags"] == ["language_swapped"]
    text = " ".join(warnings(r))
    assert "Hindi fields" in text and "mostly not Devanagari" in text and "English fields" in text and "mostly Devanagari" in text


def test_hindi_that_is_really_english_is_flagged():
    r, item = one(both(1, question_hi="Which of the following statements about the treaty is correct?", options_hi=dict(EN_OPTIONS)))
    assert r.ok and item["lang_flags"] == ["language_swapped"]


def test_hindi_with_english_words_inside_it_is_fine():
    r, item = one(both(1, question_hi="भारत के संविधान का Article 21 किस अधिकार से संबंधित है और इसका क्या महत्व है?",
                       options_hi={"a": "जीवन का अधिकार", "b": "समानता का अधिकार", "c": "स्वतंत्रता का अधिकार", "d": "शिक्षा का अधिकार"}))
    assert r.ok and item["lang_flags"] == [] and not r.warnings


def test_short_numeric_options_do_not_trip_the_script_check():
    r, item = one(both(1, options_hi={"a": "1", "b": "2", "c": "1, 2", "d": "3"}, options={"a": "1", "b": "2", "c": "1, 2", "d": "3"}))
    assert item["lang_flags"] == []


# --------------------------------------------------------------------------- merging parts

def test_a_second_part_can_add_the_hindi_version_to_an_english_question():
    english = doc([q(1, options=dict(EN_OPTIONS))])
    hi = hi_sentence()
    bilingual = doc([q(1, options=dict(EN_OPTIONS), question_hi=hi, options_hi=dict(HI_OPTIONS))])
    # the English text must be identical for the two entries to be recognised as the same question
    text = json.loads(english)["questions"][0]["question"]
    assert json.loads(bilingual)["questions"][0]["question"] == text
    r = report(english, bilingual)
    assert r.ok and r.overlaps == [1] and r.questions[1]["text_hi"] == hi and r.questions[1]["options_hi"] == HI_OPTIONS


def test_different_hindi_for_the_same_number_is_a_conflict():
    a, b = both(1), both(1)
    b["question"], b["options"] = a["question"], a["options"]
    r = report(doc([a]), doc([b]))
    assert not r.ok and r.conflicts == [1] and any("different text or options" in e for e in errors(r))


def test_english_only_and_hindi_only_for_one_number_cannot_be_checked_against_each_other():
    r = report(doc([q(1)]), doc([hindi_only(1)]))
    assert not r.ok and any("can't be checked to be the same question" in e for e in errors(r))
    later = report(doc([q(1)]), doc([hindi_only(1)]), later_wins=True)
    assert later.ok and later.questions[1]["text"] == "" and any("share no language" in w for w in warnings(later))


def test_hindi_explanations_conflict_like_english_ones():
    a = both(1, explanation_hi="पहली व्याख्या")
    b = dict(a, explanation_hi="दूसरी व्याख्या")
    r = report(doc([a]), doc([b]))
    assert not r.ok and any("explanation_hi" in e for e in errors(r))


# --------------------------------------------------------------------------- hashes and duplicates

def test_english_hashes_are_unchanged_by_the_hindi_work():
    def old(text, options):
        import hashlib
        n = lambda s: re.sub(r"[^a-z0-9]+", "", (s or "").lower())      # noqa: E731
        return hashlib.sha1((n(text) + "|" + "|".join(sorted(n(o) for o in options))).encode()).hexdigest()[:16]
    for text, opts in (("Who wrote  'Discovery of India'?", ["Gandhi", "Nehru", "Patel", "Ambedkar"]), ("1 + 1 = ?", ["1", "2", "3", "4"])):
        assert ji.norm_hash(text, opts) == old(text, opts)


def test_hindi_questions_get_their_own_hashes():
    a, b = hindi_only(1), hindi_only(2)
    ra, rb = report(doc([a, b])), None
    hashes = {v["hash"] for v in ra.questions.values()}
    assert len(hashes) == 2                                                          # not both the hash of an empty string
    assert ji.norm_hash("भारत  का संविधान?", ["क", "ख", "ग", "घ"]) == ji.norm_hash("भारत का संविधान", ["घ", "ग", "ख", "क"])
    assert ji.norm_hash("भारत का संविधान", ["क", "ख", "ग", "घ"]) != ji.norm_hash("भारत का ध्वज", ["क", "ख", "ग", "घ"])


# --------------------------------------------------------------------------- the pages and downloads

def test_the_prompt_and_template_downloads_are_utf8_devanagari(admin):
    prompt = admin.get("/admin/import/json/prompt")
    assert "utf-8" in prompt.headers["content-type"].lower() and "क, ख, ग, घ" in prompt.content.decode("utf-8")
    assert "Polity, History, Geography, Economy, Environment" in prompt.text
    template = admin.get("/admin/import/json/template")
    body = json.loads(template.content.decode("utf-8"))
    assert body["schema_version"] == 2 and body["questions"][0]["question_hi"].startswith("निम्नलिखित")
    assert "निम्नलिखित" in admin.get("/admin/import/json").text


def test_the_template_validates_and_carries_both_languages():
    r = report(ji.TEMPLATE_TEXT)
    assert r.ok and not r.errors
    q1 = r.questions[1]
    assert q1["options_hi"]["c"] == "1 और 2 दोनों" and q1["text_hi"].splitlines()[1] == "1. कथन एक" and q1["lang_flags"] == []


def test_the_report_page_counts_the_languages(admin, db):
    parts = doc([both(1), both(2), q(3), hindi_only(4), q(5, question_hi=hi_sentence())])
    r = validate(admin, ("mixed.json", parts))
    assert r.status_code == 200
    page = r.text
    assert re.search(r'stat-num">3</div><div class="paper-meta">in English and Hindi', page)          # Q1, Q2 and Q5 (Q5's Hindi lacks options)
    assert re.search(r'stat-num">1</div><div class="paper-meta">English only', page)                 # Q3
    assert re.search(r'stat-num">1</div><div class="paper-meta">Hindi only', page)
    assert "with a language warning" in page and "Hindi question text without its options" in page
    assert "English + Hindi" in page and ">Hindi<" in page


# --------------------------------------------------------------------------- importing

def import_mixed(admin, db, items, **extra):
    title = f"Hindi import {uuid.uuid4().hex[:8]}"
    token = token_of(validate(admin, ("h.json", doc(items))))
    assert apply(admin, token, title=title, **extra).status_code == 303
    return paper_by_title(db, title)


def test_everything_is_saved_and_needs_review(admin, db):
    items = [both(1, explanation_hi="यह व्याख्या हिंदी में है।"), hindi_only(2), q(3), q(4, question_hi=hi_sentence())]
    paper = import_mixed(admin, db, items)
    qs = questions_by_number(db, paper)
    q1, q2, q3, q4 = qs[1], qs[2], qs[3], qs[4]
    assert all(x.status == QStatus.NEEDS_REVIEW and x.source == "ai_json" for x in qs.values())
    assert q1.text and q1.question_hi.startswith("निम्नलिखित") and (q1.option_a_hi, q1.option_d_hi) == ("केवल 1", "न तो 1 और न ही 2")
    assert q1.explanation_hi == "यह व्याख्या हिंदी में है।" and q1.explanation_hi_status == "unverified" and q1.explanation_status is None
    assert (q2.text, q2.option_a, q2.option_b, q2.option_c, q2.option_d) == ("", "", "", "", "") and q2.question_hi and q2.option_c_hi == "1 और 2 दोनों"
    assert language.which(q2) == "hi" and language.which(q1) == "both" and language.which(q3) == "en"
    assert q3.question_hi is None and q3.option_a_hi is None and q3.explanation_hi_status is None
    assert "language_incomplete" in q4.ocr_flags.split(",") and q4.question_hi and q4.option_a_hi is None
    assert not q1.ocr_flags.replace("ai_answer", "").strip(",")


def test_the_review_page_shows_the_hindi_version_and_says_when_english_is_missing(admin, db):
    paper = import_mixed(admin, db, [both(1), hindi_only(2)])
    page = admin.get(f"/review/{paper.id}").text
    assert page.count('name="question_hi"') == 2 and "केवल 1" in page and "1 और 2 दोनों" in page          # both are editable since step B
    assert "No English version" in page and page.count("No English version") == 1
    assert "One language is incomplete" not in page
    assert "lang-title" in page


def test_language_warnings_appear_on_the_review_card(admin, db):
    paper = import_mixed(admin, db, [q(1, question_hi=hi_sentence()), both(2, question_hi="Which of the following is correct about treaties?", options_hi=dict(EN_OPTIONS))])
    page = admin.get(f"/review/{paper.id}").text
    assert "One language is incomplete" in page and "look swapped" in page


def test_replacing_a_waiting_question_brings_its_hindi_along_and_keeps_the_old_one_in_history(admin, db):
    paper = import_mixed(admin, db, [both(1)])
    old = questions_by_number(db, paper)[1]
    old_hi = old.question_hi
    replacement = both(1)
    token = token_of(validate(admin, ("r.json", doc([replacement])), target=str(paper.id)))
    assert apply(admin, token, overwrite_needs_review="true").status_code == 303
    new = questions_by_number(db, paper)[1]
    assert new.question_hi == replacement["question_hi"] != old_hi
    snapshot = json.loads(db.query(models.QuestionVersion).filter_by(question_id=new.id).order_by(models.QuestionVersion.id.desc()).first().snapshot_json)
    assert snapshot["question_hi"] == old_hi and "option_a_hi" in snapshot and "explanation_hi_status" in snapshot


def test_history_and_undo_cover_the_hindi_fields(db, admin):
    paper = import_mixed(admin, db, [both(1)])
    q1 = questions_by_number(db, paper)[1]
    original = q1.question_hi
    user = db.query(models.User).filter_by(is_admin=True).first()
    version = versions.snapshot(db, q1, user, "edit")
    q1.question_hi, q1.option_b_hi = "बदला हुआ पाठ", "बदला विकल्प"
    db.commit()
    assert "question_hi" in versions.changed_fields(q1, json.loads(version.snapshot_json))
    versions.restore(db, q1, version, user)
    db.commit()
    assert q1.question_hi == original and q1.option_b_hi == HI_OPTIONS["b"] and q1.status == QStatus.NEEDS_REVIEW


def test_a_hindi_edit_counts_as_a_content_edit_for_the_audit():
    from app import sample_audit
    assert {"question_hi", "option_a_hi", "option_d_hi"} <= set(sample_audit.CONTENT_FIELDS)


def test_the_same_hindi_question_in_two_papers_is_an_exact_duplicate_and_two_different_ones_are_not(admin, db):
    shared = hindi_only(1)
    first = import_mixed(admin, db, [shared, hindi_only(2)])
    second = import_mixed(admin, db, [dict(shared), hindi_only(2)], )
    ids = {x.id for x in db.query(models.Question).filter(models.Question.paper_id.in_([first.id, second.id]))}
    db.rollback()
    pairs = [d for d in db.query(models.QuestionDuplicate) if d.question_id in ids and d.other_id in ids]
    kinds = sorted(d.kind for d in pairs)
    assert kinds == ["exact"]                                                        # only Q1 matches; the two different Q2s do not
    a, b = pairs[0].other_id, pairs[0].question_id
    assert questions_by_number(db, first)[1].id == a and questions_by_number(db, second)[1].id == b


def test_importing_a_hindi_question_that_already_exists_gets_the_duplicate_warning(admin, db):
    item = hindi_only(1)
    import_mixed(admin, db, [item])
    r = validate(admin, ("again.json", doc([dict(item)])))
    assert "looks like" in r.text and "same text and options" in r.text


def test_the_duplicates_page_shows_a_hindi_only_question_as_a_pair(admin, db):
    shared = hindi_only(1)
    first, second = import_mixed(admin, db, [shared]), import_mixed(admin, db, [dict(shared)])
    page = admin.get(f"/admin/duplicates?paper_id={second.id}").text
    assert first.title in page and second.title in page and "Identical" in page


def test_answers_of_two_hindi_copies_are_compared_by_their_option_text(db):
    a = models.Question(text="", question_hi="प्रश्न", option_a_hi="दिल्ली", option_b_hi="मुंबई", option_c_hi="चेन्नई", option_d_hi="कोलकाता", correct_answer="A")
    b = models.Question(text="", question_hi="प्रश्न", option_a_hi="कोलकाता", option_b_hi="चेन्नई", option_c_hi="दिल्ली", option_d_hi="मुंबई", correct_answer="C")
    assert not duplicates.answers_differ(a, b)                                          # both say Delhi
    b.correct_answer = "A"
    assert duplicates.answers_differ(a, b)


def test_pdf_questions_and_the_english_only_pipeline_are_untouched(admin, db, make_paper):
    paper = make_paper("English PDF paper", n=3)
    for x in db.query(models.Question).filter_by(paper_id=paper.id):
        assert x.question_hi is None and language.which(x) == "en"
    assert "hindi_text" in __import__("app.ingest", fromlist=["FLAG_LABELS"]).FLAG_LABELS               # still flagged when a PDF has Hindi
