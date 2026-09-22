"""Hindi support, step C: what students see — language preference, the quick switch, fallbacks, labels, results lists, reports."""
import itertools
import re
import uuid

import pytest

from app import language, models
from app.models import AttemptStatus, QStatus
from conftest import make_student_client
from test_hindi_stage_a import both, hindi_only, import_mixed
from test_json_import import q as json_q

_year = itertools.count(41000)


def hindi_paper(db, admin, verified_hi_explanation=False):
    """A published paper of four live questions: 1 both languages (both explanations), 2 Hindi only (Hindi explanation),
    3 English only (English explanation), 4 both languages (no explanation)."""
    items = [
        both(1, explanation="First English explanation text.", explanation_hi="पहली हिंदी व्याख्या का पाठ।"),
        hindi_only(2, explanation_hi="दूसरी हिंदी व्याख्या का पाठ।"),
        json_q(3, explanation="Third English explanation text."),
        both(4),
    ]
    paper = import_mixed(admin, db, items)
    row = db.get(models.Paper, paper.id)
    row.year, row.publish_status, row.marks_per_question, row.negative_fraction = next(_year), "published", 2.0, 1 / 3
    for x in db.query(models.Question).filter_by(paper_id=paper.id):
        x.status = QStatus.LIVE
        if verified_hi_explanation and x.explanation_hi:
            x.explanation_hi_status = "verified"
    db.commit()
    db.refresh(row)
    return row


def by_number(db, paper):
    db.rollback()
    db.expire_all()
    return {x.question_number: x for x in db.query(models.Question).filter_by(paper_id=paper.id)}


def student_for(db, name=None, lang=None):
    client = make_student_client(db, name or f"hi_student_{uuid.uuid4().hex[:8]}")
    if lang:
        assert client.post("/account/language", data={"language": lang, "next": "/"}).status_code == 303
    return client


def start_practice(db, client, paper, count=4):
    r = client.post("/practice/start", data={"year": str(paper.year), "count": str(count)})
    assert r.status_code == 303, r.text[:300]
    attempt_id = int(re.fullmatch(r"/attempts/(\d+)", r.headers["location"]).group(1))
    db.rollback()
    db.expire_all()
    attempt = db.get(models.Attempt, attempt_id)
    position_of = {db.get(models.Question, resp.question_id).question_number: resp.position for resp in attempt.responses}
    return attempt, position_of


def page_of(client, attempt, position):
    r = client.get(f"/attempts/{attempt.id}/q/{position}")
    assert r.status_code == 200
    return r.text


def modes_of(page, text):
    """The data-modes of the element that holds `text`."""
    m = re.search(r'data-modes="([^"]*)"[^>]*>\s*' + re.escape(text), page)
    assert m, f"{text!r} not found in a mode-tagged element"
    return m.group(1).split()


def explanation_modes(page, text):
    """The data-modes of the explanation box that holds `text`."""
    m = re.search(r'data-modes="([^"]*)">\s*<div class="paper-meta">[^<]*</div>\s*<div class="explanation-text">\s*' + re.escape(text), page)
    assert m, f"{text!r} not found in an explanation box"
    return m.group(1).split()


def scope(page):
    return re.search(r'class="lang-scope" data-lang="(\w+)"', page).group(1)


# --------------------------------------------------------------------------- the rules for what shows in each mode

def question(**kw):
    base = dict(text="English stem?", option_a="a", option_b="b", option_c="c", option_d="d", question_hi="हिंदी प्रश्न?",
                option_a_hi="क", option_b_hi="ख", option_c_hi="ग", option_d_hi="घ")
    base.update(kw)
    return models.Question(**base)


def test_a_bilingual_question_shows_english_or_hindi_or_both():
    m = language.modes(question())
    assert m["text_en"].split() == ["en", "both"] and m["text_hi"].split() == ["hi", "both"] and m["note_hi"] == "" and m["note_en"] == ""


def test_an_english_only_question_falls_back_with_a_note_in_hindi_mode():
    m = language.modes(question(question_hi=None, option_a_hi=None, option_b_hi=None, option_c_hi=None, option_d_hi=None))
    assert m["text_en"].split() == ["en", "hi", "both"] and m["text_hi"] == "" and m["note_hi"] == "hi" and m["note_en"] == ""


def test_a_hindi_only_question_falls_back_with_a_note_in_english_mode():
    m = language.modes(question(text="", option_a="", option_b="", option_c="", option_d=""))
    assert m["text_hi"].split() == ["en", "hi", "both"] and m["text_en"] == "" and m["note_en"] == "en" and m["note_hi"] == ""


def test_both_mode_adds_nothing_for_a_missing_language():
    en_only = language.modes(question(question_hi=None, option_a_hi=None, option_b_hi=None, option_c_hi=None, option_d_hi=None))
    hi_only = language.modes(question(text="", option_a="", option_b="", option_c="", option_d=""))
    assert "both" in en_only["text_en"].split() and en_only["note_hi"] != "both"
    assert "both" in hi_only["text_hi"].split() and hi_only["note_en"] != "both"


def test_an_incomplete_hindi_version_is_treated_as_not_available():
    m = language.modes(question(option_d_hi=None))                       # Hindi text with only three options
    assert m["text_hi"] == "" and m["note_hi"] == "hi" and "hi" in m["text_en"].split()


def test_explanations_have_their_own_availability():
    both_expl = language.modes(question(explanation="E", explanation_hi="ह"))
    assert both_expl["expl_en"].split() == ["en", "both"] and both_expl["expl_hi"].split() == ["hi", "both"]
    en_expl = language.modes(question(explanation="E"))                  # Hindi question, but the explanation is English only
    assert en_expl["expl_en"].split() == ["en", "hi", "both"] and en_expl["expl_note_hi"] == "hi" and en_expl["expl_hi"] == ""
    hi_expl = language.modes(question(explanation_hi="ह"))
    assert hi_expl["expl_hi"].split() == ["en", "hi", "both"] and hi_expl["expl_note_en"] == "en"


def test_list_text_and_preference_helpers():
    both_q = question()
    assert language.list_text(both_q, "en") == "English stem?" and language.list_text(both_q, "both") == "English stem?"
    assert language.list_text(both_q, "hi") == "हिंदी प्रश्न?"
    assert language.list_text(question(text=""), "en") == "हिंदी प्रश्न?" and language.list_text(question(question_hi=None), "hi") == "English stem?"
    assert language.pref_of(models.User(language="hi")) == "hi" and language.pref_of(models.User(language="fr")) == "en" and language.pref_of(None) == "en"
    assert language.explanation_label_hi(question(explanation_hi_status="verified")) == "Verified"
    assert language.explanation_label_hi(question(explanation_hi_status="unverified")) == "AI-supplied, unverified"


# --------------------------------------------------------------------------- the preference

def test_the_profile_has_a_language_setting_that_defaults_to_english(admin, db):
    student = student_for(db)
    page = student.get("/account").text
    assert 'name="language"' in page and re.search(r'<option value="en" selected>', page)
    assert student.post("/account/profile", data={"language": "both", "display_name": "", "daily_target": ""}).status_code == 303
    assert re.search(r'<option value="both" selected>', student.get("/account").text)
    student.post("/account/profile", data={"language": "klingon", "display_name": "", "daily_target": ""})       # invalid: ignored
    assert re.search(r'<option value="both" selected>', student.get("/account").text)


def test_the_quick_switch_saves_the_choice_and_only_goes_to_safe_places(admin, db):
    student = student_for(db)
    r = student.post("/account/language", data={"language": "hi", "next": "/practice"})
    assert r.status_code == 303 and r.headers["location"] == "/practice"
    assert student.post("/account/language", data={"language": "en", "next": "https://evil.example/x"}).headers["location"] == "/"
    assert student.post("/account/language", data={"language": "bogus"}).status_code == 303          # nothing changes
    r = student.post("/account/language/save", data={"language": "both"})
    assert r.status_code == 200 and r.json() == {"language": "both"}
    assert student.post("/account/language/save", data={"language": "bogus"}).status_code == 400
    assert re.search(r'<option value="both" selected>', student.get("/account").text)


def test_only_signed_in_users_can_change_their_language(anon):
    for url in ("/account/language", "/account/language/save"):
        r = anon.post(url, data={"language": "hi"})
        assert r.status_code == 303 and r.headers["location"].startswith("/login")


# --------------------------------------------------------------------------- practice

def test_the_page_carries_the_students_language_and_every_version(admin, db):
    paper = hindi_paper(db, admin)
    qs = by_number(db, paper)
    student = student_for(db, lang="hi")
    attempt, pos = start_practice(db, student, paper)
    page = page_of(student, attempt, pos[1])
    assert scope(page) == "hi"
    assert modes_of(page, qs[1].text) == ["en", "both"] and modes_of(page, qs[1].question_hi) == ["hi", "both"]
    assert modes_of(page, qs[1].option_a) == ["en", "both"] and modes_of(page, qs[1].option_a_hi) == ["hi", "both"]
    assert page.count('lang="hi"') >= 2 and "Hindi not available" not in page.split("Question 1")[0]


def test_english_students_see_english_and_the_default_changes_nothing(admin, db):
    paper = hindi_paper(db, admin)
    student = student_for(db)                                               # never chose a language
    attempt, pos = start_practice(db, student, paper)
    page = page_of(student, attempt, pos[3])
    assert scope(page) == "en" and by_number(db, paper)[3].text.splitlines()[0][:30] in page
    assert scope(page_of(student, attempt, pos[1])) == "en"


def test_an_english_only_question_in_hindi_mode_shows_english_with_a_note(admin, db):
    paper = hindi_paper(db, admin)
    qs = by_number(db, paper)
    student = student_for(db, lang="hi")
    attempt, pos = start_practice(db, student, paper)
    page = page_of(student, attempt, pos[3])
    assert modes_of(page, qs[3].text) == ["en", "hi", "both"]
    assert re.search(r'data-modes="hi">Hindi not available for this question<', page)


def test_a_hindi_only_question_in_english_mode_shows_hindi_with_a_note(admin, db):
    paper = hindi_paper(db, admin)
    qs = by_number(db, paper)
    student = student_for(db, lang="en")
    attempt, pos = start_practice(db, student, paper)
    page = page_of(student, attempt, pos[2])
    assert modes_of(page, qs[2].question_hi) == ["en", "hi", "both"] and modes_of(page, qs[2].option_c_hi) == ["en", "hi", "both"]
    assert re.search(r'data-modes="en">English not available for this question<', page)
    assert 'name="answer"' in page and page.count('name="answer"') == 4                                # the options are still answerable


def test_both_mode_stacks_the_languages_inside_each_option(admin, db):
    paper = hindi_paper(db, admin)
    qs = by_number(db, paper)
    student = student_for(db, lang="both")
    attempt, pos = start_practice(db, student, paper)
    page = page_of(student, attempt, pos[1])
    assert scope(page) == "both"
    option = re.search(r'<span class="opt-letter">A</span>\s*<span class="opt-text">(.*?)</span>\s*</span>\s*</label>', page, re.S)
    assert option and qs[1].option_a in option.group(1) and qs[1].option_a_hi in option.group(1)       # one radio, both languages inside it
    assert page.count('type="radio" name="answer"') == 4


def test_answers_and_explanations_stay_hidden_until_the_student_answers(admin, db):
    paper = hindi_paper(db, admin)
    qs = by_number(db, paper)
    student = student_for(db, lang="both")
    attempt, pos = start_practice(db, student, paper)
    page = page_of(student, attempt, pos[1])
    assert qs[1].explanation not in page and qs[1].explanation_hi not in page and "Correct answer" not in page
    assert student.post(f"/attempts/{attempt.id}/q/{pos[1]}/answer", data={"answer": "A", "confidence": "sure"}).status_code == 303
    page = page_of(student, attempt, pos[1])
    assert qs[1].explanation in page and qs[1].explanation_hi in page


def test_each_explanation_is_labelled_and_shown_per_language(admin, db):
    paper = hindi_paper(db, admin)
    qs = by_number(db, paper)
    student = student_for(db, lang="hi")
    attempt, pos = start_practice(db, student, paper)
    for n in (1, 2, 3, 4):
        student.post(f"/attempts/{attempt.id}/q/{pos[n]}/answer", data={"answer": "B", "confidence": "sure"})
    q1 = page_of(student, attempt, pos[1])
    assert explanation_modes(q1, qs[1].explanation) == ["en", "both"] and explanation_modes(q1, qs[1].explanation_hi) == ["hi", "both"]
    assert "Explanation (Hindi) · AI-supplied, unverified" in q1 and "Explanation · AI-supplied, unverified" in q1
    q3 = page_of(student, attempt, pos[3])                                   # English explanation only, viewed in Hindi
    assert explanation_modes(q3, qs[3].explanation) == ["en", "hi", "both"] and re.search(r'data-modes="hi">Hindi explanation not available', q3)
    q2 = page_of(student, attempt, pos[2])                                   # Hindi explanation only, viewed in English mode notes
    assert explanation_modes(q2, qs[2].explanation_hi) == ["en", "hi", "both"] and re.search(r'data-modes="en">English explanation not available', q2)
    q4 = page_of(student, attempt, pos[4])
    assert "No explanation is available" in q4


def test_a_verified_hindi_explanation_is_labelled_verified(admin, db):
    paper = hindi_paper(db, admin, verified_hi_explanation=True)
    student = student_for(db, lang="hi")
    attempt, pos = start_practice(db, student, paper)
    student.post(f"/attempts/{attempt.id}/q/{pos[1]}/answer", data={"answer": "B", "confidence": "sure"})
    page = page_of(student, attempt, pos[1])
    assert "Explanation (Hindi) · Verified" in page and "Explanation · AI-supplied, unverified" in page


# --------------------------------------------------------------------------- the quick switch

def test_the_switch_is_on_screens_that_have_hindi_and_off_for_all_english_sessions(admin, db, make_paper):
    paper = hindi_paper(db, admin)
    student = student_for(db)
    attempt, pos = start_practice(db, student, paper)
    page = page_of(student, attempt, pos[3])                                  # an English question, but the session has Hindi in it
    assert 'class="lang-toggle"' in page and 'data-save-url="/account/language/save"' in page
    assert all(f'name="language" value="{v}"' in page for v in ("en", "hi", "both")) and '<script src="/static/lang.js">' in page
    # an all-English session for an English student has no switch...
    plain = make_paper("Plain English paper", n=3, publish_status="published", year=next(_year))
    for x in db.query(models.Question).filter_by(paper_id=plain.id):
        x.status = QStatus.LIVE
    db.commit()
    other = student_for(db)
    attempt2, _ = start_practice(db, other, plain, 3)
    assert 'class="lang-toggle"' not in page_of(other, attempt2, 1)
    # ...but one who chose Hindi keeps it, so they can always switch back
    hindi_reader = student_for(db, lang="hi")
    attempt3, _ = start_practice(db, hindi_reader, plain, 3)
    assert 'class="lang-toggle"' in page_of(hindi_reader, attempt3, 1)


def test_the_switch_script_only_changes_the_display(admin):
    js = admin.get("/static/lang.js").text
    assert "data-lang" in js and "preventDefault" in js and "fetch(" in js
    assert "reload" not in js and ".submit(" not in js and "location" not in js                 # never reloads or resubmits anything


def test_switching_language_touches_no_attempt_answer_timer_or_position(admin, db):
    paper = hindi_paper(db, admin)
    for row in (paper,):
        db.get(models.Paper, row.id).duration_minutes = 60
    db.commit()
    student = student_for(db, lang="en")
    r = student.post("/tests/start", data={"mode": "full", "paper_id": str(paper.id)})
    assert r.status_code == 303
    attempt_id = int(re.search(r"/attempts/(\d+)", r.headers["location"]).group(1))
    db.rollback()
    attempt = db.get(models.Attempt, attempt_id)
    assert attempt.status == AttemptStatus.IN_PROGRESS
    student.post(f"/attempts/{attempt_id}/q/1/save", data={"answer": "C", "confidence": "guessed", "marked": "1"})

    def snapshot():
        db.rollback()
        db.expire_all()
        a = db.get(models.Attempt, attempt_id)
        return ({c.name: getattr(a, c.name) for c in models.Attempt.__table__.columns},
                [{c.name: getattr(x, c.name) for c in models.Response.__table__.columns} for x in a.responses],
                db.query(models.Attempt).filter_by(user_id=a.user_id).count())

    page_before = student.get(f"/attempts/{attempt_id}/q/1").text          # (viewing a question is itself recorded, so look first)
    before = snapshot()
    assert student.post("/account/language/save", data={"language": "hi"}).status_code == 200
    assert student.post("/account/language/save", data={"language": "both"}).status_code == 200
    after = snapshot()
    assert after[0] == before[0] and after[2] == before[2]                                     # deadline, status, current question, times: all as they were
    assert [r["selected_answer"] for r in after[1]] == [r["selected_answer"] for r in before[1]]
    assert [(r["confidence"], r["marked_for_review"]) for r in after[1]] == [(r["confidence"], r["marked_for_review"]) for r in before[1]]
    page_after = student.get(f"/attempts/{attempt_id}/q/1").text
    assert scope(page_before) == "en" and scope(page_after) == "both"
    assert re.search(r'data-deadline="([^"]+)"', page_before).group(1) == re.search(r'data-deadline="([^"]+)"', page_after).group(1)
    assert re.search(r'<input type="radio" name="answer" value="C" checked>', page_after)      # the saved answer is still there


def test_a_running_test_shows_the_switch_in_its_bar_and_no_explanations(admin, db):
    paper = hindi_paper(db, admin)
    qs = by_number(db, paper)
    student = student_for(db, lang="hi")
    r = student.post("/tests/start", data={"mode": "full", "paper_id": str(paper.id)})
    attempt_id = int(re.search(r"/attempts/(\d+)", r.headers["location"]).group(1))
    page = student.get(f"/attempts/{attempt_id}/q/1").text
    bar = page.split('id="test-bar"')[1].split("test-layout")[0]
    assert 'class="lang-toggle"' in bar and 'role="timer"' in bar
    assert qs[1].explanation not in page and qs[1].explanation_hi not in page and "Correct answer" not in page
    assert 'id="save-form"' in page and scope(page) == "hi"


# --------------------------------------------------------------------------- a question's own page, and the lists

def test_a_questions_own_page_has_both_versions_and_the_switch(admin, db):
    paper = hindi_paper(db, admin)
    qs = by_number(db, paper)
    student = student_for(db, lang="both")
    attempt, pos = start_practice(db, student, paper)
    student.post(f"/attempts/{attempt.id}/q/{pos[1]}/answer", data={"answer": "B", "confidence": "sure"})
    page = student.get(f"/questions/{qs[1].id}").text
    assert scope(page) == "both" and 'class="lang-toggle"' in page
    assert modes_of(page, qs[1].question_hi) == ["hi", "both"] and explanation_modes(page, qs[1].explanation_hi) == ["hi", "both"]
    assert "Explanation (Hindi)" in page


def test_a_hindi_question_the_student_has_not_met_is_still_a_404(admin, db):
    paper = hindi_paper(db, admin)
    qs = by_number(db, paper)
    student = student_for(db, lang="hi")
    assert student.get(f"/questions/{qs[1].id}").status_code == 404


def test_lists_show_the_students_language_with_fallbacks(admin, db):
    paper = hindi_paper(db, admin)
    qs = by_number(db, paper)
    student = student_for(db, lang="hi")
    attempt, pos = start_practice(db, student, paper)
    for n in (1, 2, 3, 4):
        student.post(f"/attempts/{attempt.id}/q/{pos[n]}/answer", data={"answer": "B", "confidence": "sure"})
        student.post(f"/questions/{qs[n].id}/bookmark", data={"on": "1"})
    student.post(f"/attempts/{attempt.id}/finish")
    result = student.get(f"/attempts/{attempt.id}/result").text
    marks = qs[1].question_hi[:40]
    assert marks in result and qs[2].question_hi[:40] in result                    # Hindi where there is Hindi
    assert qs[3].text.splitlines()[0][:40] in result                               # English fallback for the English-only question
    bookmarks = student.get("/bookmarks").text
    assert qs[1].question_hi[:40] in bookmarks and qs[3].text[:40] in bookmarks
    student.post("/account/language", data={"language": "en"})
    english = student.get(f"/attempts/{attempt.id}/result").text
    assert qs[1].text[:40] in english and qs[2].question_hi[:40] in english        # Hindi-only falls back to Hindi in English mode
    assert student.get("/revision").status_code == 200


# --------------------------------------------------------------------------- reporting a problem

def test_the_report_form_asks_which_language_only_for_questions_that_have_hindi(admin, db):
    paper = hindi_paper(db, admin)
    qs = by_number(db, paper)
    student = student_for(db, lang="hi")
    attempt, pos = start_practice(db, student, paper)
    for n in (1, 3):
        student.post(f"/attempts/{attempt.id}/q/{pos[n]}/answer", data={"answer": "B", "confidence": "sure"})
    with_hindi = student.get(f"/attempts/{attempt.id}/q/{pos[1]}").text
    assert "Which language version?" in with_hindi and re.search(r'<option value="hi" selected>Hindi version', with_hindi)
    assert "Which language version?" not in student.get(f"/attempts/{attempt.id}/q/{pos[3]}").text


def test_a_report_remembers_which_language_it_is_about_and_the_admin_sees_it(admin, db):
    paper = hindi_paper(db, admin)
    qs = by_number(db, paper)
    student = student_for(db, lang="en")
    attempt, pos = start_practice(db, student, paper)
    for n in (1, 3, 2):
        student.post(f"/attempts/{attempt.id}/q/{pos[n]}/answer", data={"answer": "B", "confidence": "sure"})
    r = student.post(f"/questions/{qs[1].id}/report", data={"kind": "wrong_text", "note": "The Hindi option b reads oddly", "language": "hi"})
    assert r.status_code == 303
    r = student.post(f"/questions/{qs[3].id}/report", data={"kind": "wrong_text", "note": "typo in English", "language": "hi"})     # no Hindi there
    r = student.post(f"/questions/{qs[2].id}/report", data={"kind": "wrong_text", "note": "odd", "language": "klingon"})              # invalid
    db.rollback()
    reports = {x.question_id: x for x in db.query(models.QuestionReport).filter(models.QuestionReport.question_id.in_([qs[1].id, qs[2].id, qs[3].id]))}
    assert reports[qs[1].id].language == "hi" and reports[qs[3].id].language is None and reports[qs[2].id].language is None
    page = admin.get("/admin/reports").text
    assert "Hindi version" in page and "The Hindi option b reads oddly" in page


def test_reports_without_a_language_still_work_as_before(admin, db, make_paper):
    plain = make_paper("English report paper", n=2, publish_status="published", year=next(_year))
    for x in db.query(models.Question).filter_by(paper_id=plain.id):
        x.status = QStatus.LIVE
    db.commit()
    student = student_for(db)
    attempt, pos = start_practice(db, student, plain, 2)
    student.post(f"/attempts/{attempt.id}/q/1/answer", data={"answer": "A", "confidence": "sure"})
    x = db.get(models.Question, attempt.responses[0].question_id)
    assert student.post(f"/questions/{x.id}/report", data={"kind": "wrong_answer", "note": ""}).status_code == 303
    db.rollback()
    assert db.query(models.QuestionReport).filter_by(question_id=x.id).one().language is None


# --------------------------------------------------------------------------- nothing new leaks

def test_hindi_text_of_a_question_that_is_not_live_never_reaches_a_student(admin, db):
    paper = hindi_paper(db, admin)
    qs = by_number(db, paper)
    student = student_for(db, lang="hi")
    attempt, pos = start_practice(db, student, paper)
    student.post(f"/attempts/{attempt.id}/q/{pos[1]}/answer", data={"answer": "B", "confidence": "sure"})
    row = db.get(models.Question, qs[1].id)
    row.status = QStatus.NEEDS_REVIEW                                                  # sent back to review after the student saw it
    db.commit()
    assert student.get(f"/questions/{qs[1].id}").status_code == 404
    page = student.get(f"/attempts/{attempt.id}/q/{pos[1]}").text
    assert "no longer available" in page and qs[1].question_hi not in page and qs[1].explanation_hi not in page

def test_the_switch_is_a_horizontal_segmented_control_that_is_easy_to_use(admin, db):
    css = admin.get("/static/style.css").text
    rule = re.search(r"\.lang-toggle \{([^}]*)\}", css).group(1)
    assert "flex-direction: row" in rule and "display: inline-flex" in rule and "border-radius: 999px" in rule          # (a form is a column by default)
    assert "min-height: 44px" in css.split("@media (max-width: 600px), (pointer: coarse)")[1].split("}")[0]                 # a finger-sized target on phones
    assert ".lang-btn:focus-visible" in css and ".lang-scope.lang-switching .lang-el" in css and "prefers-reduced-motion" in css
    assert '.q-text[lang="hi"]' in css                                                                                       # Devanagari gets more room
    js = admin.get("/static/lang.js").text
    assert "ArrowRight" in js and "ArrowLeft" in js and "aria-pressed" in js and "lang-live" in js and "Showing Hindi" in js
    paper = hindi_paper(db, admin)
    student = student_for(db, lang="hi")
    attempt, pos = start_practice(db, student, paper)
    page = page_of(student, attempt, pos[1])
    assert 'class="sr-only lang-live" aria-live="polite"' in page and 'title="Show questions in Hindi"' in page
    assert page.count('aria-pressed="true"') == 1 and re.search(r'class="lang-btn on" aria-pressed="true"[^>]*lang="hi"', page)
