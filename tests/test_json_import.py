"""
Import stage 6: questions from JSON (typically written by another AI) — validation report, merge of parts, and a review-gated import.
"""
import itertools
import json
import os
import re

import pytest

from app import json_import as ji, models
from app.practice import attempts as engine, pool
from conftest import _client, pass_audit
from pdfmaker import flow, make_pdf

SUBJECTS = models.STANDARD_SUBJECTS
_run = itertools.count(1)


def q(n, **kw):
    d = {"number": n, "question": f"Sample question number {n} about the Constitution of India?",
         "options": {"a": "Option alpha", "b": "Option beta", "c": "Option gamma", "d": "Option delta"},
         "correct_answer": "b", "explanation": None, "subject": "Polity", "topic": None, "has_image": False,
         "page": None, "uncertain": False}
    d.update(kw)
    return d


def doc(questions, **paper):
    body = {"schema_version": 1, "paper": {"title": f"JSON test paper {next(_run)}", **paper}, "questions": questions}
    return json.dumps(body)


def report(*parts, **kw):
    parts = [(f"part{i}.json", p) if not isinstance(p, tuple) else p for i, p in enumerate(parts, start=1)]
    return ji.build_report(parts, SUBJECTS, **kw)


def errors(r):
    return [i.message for i in r.errors]


def warnings(r):
    return [i.message for i in r.warnings]


# =========================================================================== the prompt and the template

SPEC_RULES = [
    "Convert the attached question paper into JSON exactly in the format below.",
    "- Copy question and option text exactly as written. Do not rephrase, fix, or improve.",
    "- Extract BOTH languages when the document has both. Put English in question and\n  options. Put Hindi in question_hi and options_hi, in Devanagari (Unicode).",
    "- Never translate. Never transliterate. If a question exists in only one\n  language in the document, leave the other language's fields null. Do not create\n  a Hindi or English version yourself.",
    "- Do not correct Hindi spelling, matras or grammar. Copy it as printed.",
    "- Never guess an answer. Use correct_answer only if the answer key or the\n  document itself states it. Otherwise set it to null.",
    "- Same for explanation and explanation_hi: copy if present, else null.\n  Do not write your own.",
    "- Keep statement lists inside the question text, one per line, using \\n.",
    "- Each language given for a question has exactly 4 options with keys a, b, c, d.\n  If Hindi options are labelled क, ख, ग, घ, map them in order to a, b, c, d.",
    "- If a question depends on an image, map, table or chart, set has_image to true\n  and put [IMAGE] where it appears. Do not describe or invent it.",
    "- If you are unsure about any question or any Hindi word (unclear text, missing\n  option), set \"uncertain\": true. Never silently skip a question.",
    "- subject must be one of: Polity, History, Geography, Economy, Environment,\n  Science & Tech, Current Affairs, CSAT, Other. Use null if unsure.",
    "- page = the PDF page number where the question starts.",
    "- Output ONLY valid JSON. No commentary, no markdown.",
    "- Do 25 questions per reply, then stop and wait for me to say \"next\".",
    "- At the end of each reply, add one line outside the JSON:\n  \"Covered questions X to Y.\"",
]


def test_the_prompt_is_the_specs_text_exactly():
    prompt = ji.prompt_text()
    for chunk in SPEC_RULES:
        assert chunk in prompt, chunk
    assert prompt.index("RULES") < prompt.index("PROCESS") < prompt.index("FORMAT")
    assert "Ignore Hindi text" not in prompt and "extract English only" not in prompt          # the old English-only instruction is gone
    assert prompt.rstrip().endswith(ji.TEMPLATE_TEXT.rstrip())


def test_the_prompts_subject_list_follows_the_apps_subjects():
    assert "Polity, History, Geography, Economy, Environment,\n  Science & Tech, Current Affairs, CSAT, Other." in ji.prompt_text()
    assert "Polity, History, Law" in ji.prompt_text(["Polity", "History", "Law"])


def test_the_template_is_valid_json_and_passes_its_own_validation():
    template = json.loads(ji.TEMPLATE_TEXT)
    assert template["schema_version"] == 2 and template["questions"][0]["options"].keys() == {"a", "b", "c", "d"}
    assert template["questions"][0]["options_hi"].keys() == {"a", "b", "c", "d"} and template["questions"][0]["explanation_hi"] is None
    r = report(ji.TEMPLATE_TEXT)
    assert r.ok and len(r.questions) == 1 and r.paper["title"] == "Prelims GS Paper I 2023"
    assert r.questions[1]["text"].splitlines() == ["Consider the following statements:", "1. Statement one", "2. Statement two",
                                                   "Which of the above is/are correct?"]


# =========================================================================== reading the JSON

def test_invalid_json_says_where_it_broke_and_which_question():
    broken = '{"schema_version": 1, "questions": [\n {"number": 1, "question": "Fine question text here?", "options": {"a":"1","b":"2","c":"3","d":"4"}},\n {"number": 2, "question": "Broken one" "options": {}}\n]}'
    r = report(broken)
    assert len(r.errors) == 1 and "Invalid JSON" in errors(r)[0]
    assert "line 3" in errors(r)[0] and "near question 2" in errors(r)[0]
    assert r.errors[0].part == "part1.json" and r.errors[0].number == 2 and not r.ok


def test_text_with_no_json_is_an_error_and_a_markdown_fence_is_tolerated():
    assert "No JSON was found" in errors(report("Sorry, I can't do that."))[0]
    fenced = "```json\n" + doc([q(1)]) + "\n```"
    r = report(fenced)
    assert r.ok and not r.warnings


def test_a_covered_line_after_the_json_is_understood_and_other_trailing_text_is_warned_about():
    r = report(doc([q(1), q(2)]) + '\n"Covered questions 1 to 2."')
    assert r.ok and r.covered == [("part1.json", 1, 2)] and not r.warnings
    r = report(doc([q(1)]) + "\nHope this helps!")
    assert r.ok and any("Text after the JSON was ignored" in w for w in warnings(r))
    r = report("Here you go:\n" + doc([q(1)]))
    assert r.ok and any("Text before the JSON was ignored" in w for w in warnings(r))


def test_a_covered_line_that_overstates_is_noticed():
    r = report(doc([q(1), q(2)]) + "\nCovered questions 1 to 4.")
    assert any("says it covered questions 1 to 4" in w and "3, 4" in w for w in warnings(r))


def test_a_bare_list_is_accepted_with_a_warning_and_a_wrong_schema_version_is_refused():
    r = report(json.dumps([q(1)]))
    assert r.ok and any("bare list" in w for w in warnings(r))
    bad = json.dumps({"schema_version": 3, "questions": [q(1)]})
    assert "schema_version 3 isn't supported (expected 1 or 2)" in errors(report(bad))[0]
    assert report(json.dumps({"schema_version": 2, "questions": [q(1)]})).ok and report(json.dumps({"schema_version": 1, "questions": [q(1)]})).ok
    assert any("No \"schema_version\"" in w for w in warnings(report(json.dumps({"questions": [q(1)]}))))
    assert "\"questions\" must be a list" in errors(report(json.dumps({"schema_version": 1, "questions": {"a": 1}})))[0]


def test_bytes_that_are_not_utf8_or_too_large_are_refused():
    assert "isn't UTF-8" in errors(ji.build_report([("x.json", b"\xff\xfe\x00bad")], SUBJECTS))[0]
    r = ji.build_report([("bom.json", b"\xef\xbb\xbf" + doc([q(1)]).encode())], SUBJECTS)
    assert r.ok


# =========================================================================== validating each question

@pytest.mark.parametrize("change, expected", [
    ({"number": None}, "no valid \"number\""),
    ({"number": 0}, "no valid \"number\""),
    ({"number": True}, "no valid \"number\""),
    ({"question": ""}, "\"question\" is missing or empty"),
    ({"question": 5}, "\"question\" is missing or empty"),
    ({"options": ["a", "b", "c", "d"]}, "\"options\" must be an object with the keys a, b, c and d"),
    ({"options": {"a": "1", "b": "2", "c": "3"}}, "Missing option d"),
    ({"options": {"a": "1", "b": "2"}}, "Missing options c, d"),
    ({"options": {"a": "1", "b": "2", "c": "3", "d": "4", "e": "5"}}, "Unexpected option key e"),
    ({"options": {"a": "1", "b": "2", "c": "", "d": "4"}}, "Option c is empty"),
    ({"options": {"a": "1", "b": None, "c": "3", "d": "4"}}, "Option b is empty"),
    ({"correct_answer": "e"}, "\"correct_answer\" must be a, b, c, d or null"),
    ({"correct_answer": "3"}, "\"correct_answer\" must be a, b, c, d or null"),
    ({"correct_answer": "ab"}, "\"correct_answer\" must be a, b, c, d or null"),
    ({"correct_answer": 2}, "\"correct_answer\" must be a, b, c, d or null"),
    ({"explanation": 12}, "\"explanation\" must be text or null"),
])
def test_each_kind_of_bad_question_is_an_error_naming_its_number(change, expected):
    r = report(doc([q(1), q(2, **change), q(3)]))
    assert not r.ok
    bad = [i for i in r.errors if expected in i.message]
    assert bad, errors(r)
    assert bad[0].number == (2 if change.get("number", 2) == 2 else None) or "number" in change
    assert sorted(r.questions) == [1, 3]                                       # the good ones are still parsed


def test_option_keys_may_be_in_any_case_and_answers_in_any_common_spelling():
    for spelled in ("c", "C", "(c)", " c) ", "c."):
        r = report(doc([q(1, correct_answer=spelled, options={"A": "1 alpha", "B": "2 beta", "C": "3 gamma", "D": "4 delta"})]))
        assert r.ok and r.questions[1]["answer"] == "C" and r.questions[1]["options"]["c"] == "3 gamma", spelled
    for none in (None, ""):
        assert report(doc([q(1, correct_answer=none)])).questions[1]["answer"] is None


def test_numbers_may_be_digit_strings_and_numeric_options_become_text():
    r = report(doc([q("7", options={"a": 1, "b": 2.5, "c": "three", "d": "four"})]))
    assert r.ok and 7 in r.questions and r.questions[7]["options"]["a"] == "1" and r.questions[7]["options"]["b"] == "2.5"


def test_an_unknown_subject_is_a_warning_and_is_never_created():
    r = report(doc([q(1, subject="Astronomy"), q(2, subject="polity"), q(3, subject=None)]))
    assert r.ok
    assert r.questions[1]["subject"] is None and r.questions[2]["subject"] == "Polity" and r.questions[3]["subject"] is None
    assert any("Subject \"Astronomy\" isn't in the fixed list" in w for w in warnings(r))


def test_softer_problems_are_warnings_that_do_not_block():
    r = report(doc([
        q(1, question="Too short"),
        q(2, question="Look at the picture [IMAGE] and answer this question please"),
        q(3, has_image="yes"), q(4, page=0), q(5, uncertain="maybe"),
        q(6, options={"a": "x" * 350, "b": "b", "c": "c", "d": "d"}),
    ]))
    assert r.ok and len(r.questions) == 6
    joined = " | ".join(warnings(r))
    for expected in ("very short", "[IMAGE] but \"has_image\" isn't true", "\"has_image\" should be true or false",
                     "\"page\" should be a page number", "\"uncertain\" should be true or false", "Option a is very long"):
        assert expected in joined, expected


def test_literal_backslash_n_becomes_a_line_break_and_control_characters_go():
    r = report(doc([q(1, question="Consider the following:\\n1. First\\n2. Second\\nWhich is right?")]))
    assert r.questions[1]["text"].splitlines() == ["Consider the following:", "1. First", "2. Second", "Which is right?"]
    r = report(doc([q(1, question="Real newline\nstays  put,\x00 and control\x07 characters vanish")]))
    assert r.questions[1]["text"] == "Real newline\nstays put, and control characters vanish"


# =========================================================================== numbers and totals

def test_missing_and_extra_numbers_are_reported_against_the_expected_total():
    r = report(doc([q(1), q(2), q(4), q(7)]), expected_total=5)
    joined = " | ".join(warnings(r))
    assert "2 question numbers missing from 1–5: 3, 5" in joined and "beyond the expected 5: 7" in joined and "4 questions found but 5 expected" in joined


def test_without_an_expected_total_gaps_below_the_highest_number_are_still_reported():
    r = report(doc([q(1), q(3)]))
    assert any("1 question number missing from 1–3: 2" in w for w in warnings(r))
    assert not any("expected" in w for w in warnings(report(doc([q(1), q(2)]))))


def test_the_expected_total_can_come_from_the_paper_block():
    r = report(doc([q(1), q(2)], expected_total=3))
    assert any("2 questions found but 3 expected" in w for w in warnings(r))


# =========================================================================== merging parts

def test_parts_are_merged_by_number_and_identical_overlaps_are_noted():
    r = report(doc([q(1), q(2), q(3)]), doc([q(3), q(4)]))
    assert r.ok and sorted(r.questions) == [1, 2, 3, 4] and r.overlaps == [3] and not r.conflicts
    assert any("Q3 is in both part1.json and part2.json" in i.message for i in r.infos)
    assert [p["questions"] for p in r.parts] == [3, 2]


def test_an_overlap_fills_in_what_the_first_part_left_blank():
    r = report(doc([q(1, correct_answer=None, explanation=None)]), doc([q(1, correct_answer="c", explanation="Because.")]))
    assert r.ok and r.questions[1]["answer"] == "C" and r.questions[1]["explanation"] == "Because."


def test_same_text_but_different_answers_is_a_conflict_unless_the_later_part_wins():
    a, b = doc([q(1, correct_answer="a")]), doc([q(1, correct_answer="d")])
    r = report(a, b)
    assert not r.ok and r.conflicts == [1] and "different answer" in errors(r)[0]
    r = report(a, b, later_wins=True)
    assert r.ok and r.questions[1]["answer"] == "D"


def test_different_text_under_one_number_is_a_conflict_unless_the_later_part_wins():
    a, b = doc([q(1, question="The first version of this question text?")]), doc([q(1, question="A totally different question text?")])
    r = report(a, b)
    assert not r.ok and "different text or options" in errors(r)[0] and r.conflicts == [1]
    r = report(a, b, later_wins=True)
    assert r.ok and r.questions[1]["text"].startswith("A totally different") and any("replaces the different Q1" in w for w in warnings(r))


def test_a_number_repeated_inside_one_part_is_handled_like_a_conflict():
    r = report(doc([q(1), q(1, question="A different question with the same number?")]))
    assert not r.ok and r.conflicts == [1]


def test_the_paper_block_comes_from_the_first_part_and_differences_are_reported():
    r = report(doc([q(1)], title="Title One", year=2023), doc([q(2)], title="Title Two", year=2023))
    assert r.paper["title"] == "Title One" and any("\"paper.title\" differs between parts" in w for w in warnings(r))


def test_one_bad_part_does_not_hide_the_good_one():
    r = report(doc([q(1), q(2)]), "not json at all {")
    assert not r.ok and sorted(r.questions) == [1, 2] and [p["questions"] for p in r.parts] == [2, 0]


def test_there_is_no_limit_on_parts_questions_or_size():
    many_parts = report(*[doc([q(i)]) for i in range(1, 41)])                                # forty parts
    assert many_parts.ok and len(many_parts.questions) == 40
    big = report(doc([q(i) for i in range(1, 1501)]))                                       # fifteen hundred questions in one part
    assert big.ok and len(big.questions) == 1500
    huge = ji.build_report([("huge.json", doc([q(1, question="x" * (6 * 1024 * 1024))]).encode())], SUBJECTS)
    assert huge.ok                                                                            # a 6 MB part is fine
    assert not hasattr(ji, "MAX_PARTS") and not hasattr(ji, "MAX_QUESTIONS") and not hasattr(ji, "MAX_PART_BYTES")
    assert "Add at least one JSON part" in errors(ji.build_report([], SUBJECTS))[0]
    assert "No questions were found" in errors(report(doc([])))[0]


# =========================================================================== topics and duplicates

def test_topics_are_used_only_if_they_already_exist_under_that_subject():
    r = report(doc([q(1, topic="Fundamental Rights"), q(2, topic="Invented Topic"), q(3, subject="History", topic="Fundamental Rights")]),
               topics={"Polity": {"fundamental rights"}})
    assert r.questions[1]["topic"] == "Fundamental Rights" and r.questions[2]["topic"] is None and r.questions[3]["topic"] is None
    assert any("2 topics in the JSON don't exist under that subject" in w for w in warnings(r))


def test_normalised_hash_ignores_case_punctuation_spacing_and_option_order():
    a = ji.norm_hash("Who wrote  'Discovery of India'?", {"a": "Gandhi", "b": "Nehru", "c": "Patel", "d": "Ambedkar"})
    b = ji.norm_hash("who wrote Discovery of India", ["Ambedkar.", "patel", "NEHRU", "gandhi!"])
    assert a == b and a != ji.norm_hash("Who wrote Glimpses of World History?", ["Ambedkar", "patel", "NEHRU", "gandhi"])


def test_a_question_that_is_already_in_the_system_or_repeated_in_the_import_is_warned_about():
    text = "Which article of the Constitution guarantees equality before the law?"
    opts = {"a": "Article 14", "b": "Article 19", "c": "Article 21", "d": "Article 32"}
    known = {ji.norm_hash(text, opts): "“Old paper” Q9"}
    r = report(doc([q(1, question=text.upper(), options={"a": "article 32", "b": "Article 14.", "c": "Article 19", "d": "Article 21"}),
                    q(2, question=text, options=opts)]), known_hashes=known)
    joined = " | ".join(warnings(r))
    assert "Q1 looks like “Old paper” Q9" in joined and "Q2 looks like “Old paper” Q9" in joined
    assert any("Q2 looks like Q1 in this import" in w for w in warnings(r))
    assert len(r.duplicates) == 3


def test_numbers_already_in_the_target_paper_are_listed_not_flagged_as_duplicates():
    text = "A question that already exists in the paper being added to?"
    known = {ji.norm_hash(text, ["Option alpha", "Option beta", "Option gamma", "Option delta"]): "“This paper” Q4"}
    r = report(doc([q(4, question=text), q(5)]), existing_numbers={4: "needs_review", 9: "live"}, known_hashes=known)
    assert r.existing == {4: "needs_review"} and not r.duplicates


# =========================================================================== what gets saved

def test_flags_follow_what_the_json_claims():
    assert ji.flags_for({"uncertain": False, "answer": None, "has_image": False}) == []
    assert ji.flags_for({"uncertain": True, "answer": "A", "has_image": True}) == ["ai_uncertain", "ai_answer", "image_needed"]


# =========================================================================== through the pages

def token_of(response):
    m = re.search(r'name="token" value="([0-9a-f]{32})"', response.text)
    assert m, response.text[:400]
    return m.group(1)


def validate(admin, *parts, target="new", pasted="", later_wins=False, pdf=None):
    files = [("files", (name, data.encode() if isinstance(data, str) else data, "application/json")) for name, data in parts]
    if pdf is not None:
        files.append(("pdf_file", ("paper.pdf", pdf, "application/pdf")))
    data = {"target": target, "pasted": pasted}
    if later_wins:
        data["later_wins"] = "true"
    return admin.post("/admin/import/json/validate", data=data, files=files or None)


def apply(admin, token, title=None, **extra):
    data = {"token": token, "title": title or f"Imported JSON paper {next(_run)}", "exam_type": "full_length",
            "source_type": "coaching_test", "marks_per_question": "2", "negative_fraction": "1/3", **extra}
    return admin.post("/admin/import/json/apply", data=data)


def paper_by_title(db, title):
    db.rollback()
    return db.query(models.Paper).filter_by(title=title).one()


def questions_by_number(db, paper):
    db.rollback()
    return {x.question_number: x for x in db.query(models.Question).filter_by(paper_id=paper.id)}


def test_only_admins_can_reach_any_of_the_json_pages(make_user, anon):
    student = make_user("jsonstudent")
    for method, url in (("get", "/admin/import/json"), ("get", "/admin/import/json/template"), ("get", "/admin/import/json/prompt"),
                        ("post", "/admin/import/json/validate"), ("post", "/admin/import/json/apply")):
        assert getattr(student, method)(url).status_code == 403, url
        r = getattr(anon, method)(url)
        assert r.status_code == 303 and r.headers["location"].startswith("/login"), url


def test_the_form_offers_the_prompt_the_template_and_existing_papers(admin, db, make_paper):
    make_paper("Existing paper for JSON form", n=2)
    page = admin.get("/admin/import/json").text
    assert "Copy prompt" in page and "Convert the attached question paper into JSON" in page
    assert "/admin/import/json/template" in page and "Add to: Existing paper for JSON form" in page and 'name="files"' in page
    template = admin.get("/admin/import/json/template")
    assert template.headers["content-type"].startswith("application/json") and json.loads(template.text)["schema_version"] == 2
    assert "attachment" in template.headers["content-disposition"]
    prompt = admin.get("/admin/import/json/prompt")
    assert prompt.headers["content-type"].startswith("text/plain") and prompt.text == ji.prompt_text()


def test_validating_saves_nothing_and_shows_the_report(admin, db):
    before = (db.query(models.Paper).count(), db.query(models.Question).count())
    r = validate(admin, ("a.json", doc([q(1), q(2, subject="Astronomy")])))
    assert r.status_code == 200 and "No errors." in r.text and "2 questions can be imported" in r.text
    assert "Subject &#34;Astronomy&#34;" in r.text or "Astronomy" in r.text
    db.rollback()
    assert (db.query(models.Paper).count(), db.query(models.Question).count()) == before


def test_a_validation_with_errors_lists_them_and_offers_no_import_button(admin):
    r = validate(admin, ("a.json", doc([q(1, correct_answer="z")])))
    assert "1 error" in r.text and "can't go ahead" in r.text and "Q1" in r.text
    assert 'action="/admin/import/json/apply"' not in r.text


def test_pasted_text_and_files_are_both_parts(admin):
    r = validate(admin, ("one.json", doc([q(1)])), pasted=doc([q(2)]))
    assert "one.json (1), Pasted text (1)" in " ".join(r.text.split()) and "2 questions can be imported" in r.text


def test_nothing_to_validate_and_a_bad_pdf_are_refused(admin):
    assert "Upload at least one JSON file or paste some JSON" in admin.post("/admin/import/json/validate", data={"target": "new"}).text
    r = validate(admin, ("a.json", doc([q(1)])), pdf=b"this is not a pdf")
    assert r.status_code == 400 and "is not a PDF file" in r.text
    assert admin.post("/admin/import/json/validate", data={"target": "999999", "pasted": doc([q(1)])}).status_code == 404


def test_importing_creates_a_paper_of_questions_that_wait_for_review(admin, db):
    from app import backup
    backups_before = set(os.listdir(backup.BACKUP_DIR)) if os.path.isdir(backup.BACKUP_DIR) else set()
    r = validate(admin, ("part1.json", doc([q(1, explanation="Because it is so.", uncertain=True), q(2, correct_answer=None),
                                             q(3, has_image=True, question="See the map [IMAGE] and answer this one please")],
                                            expected_total=3)))
    token = token_of(r)
    title = f"Created from JSON {next(_run)}"
    done = apply(admin, token, title=title, expected_total="3", year="2026", series="a")
    assert done.status_code == 303

    paper = paper_by_title(db, title)
    assert paper.status == "ready" and paper.publish_status == "draft" and paper.series == "A" and paper.year == 2026
    assert paper.expected_total == 3 and paper.marks_per_question == 2.0 and paper.key_source == "AI-supplied (JSON) — unverified"
    assert done.headers["location"] == f"/review/{paper.id}"
    qs = questions_by_number(db, paper)
    assert sorted(qs) == [1, 2, 3]
    for n in (1, 2, 3):
        assert (qs[n].status, qs[n].source, qs[n].extraction_method) == (models.QStatus.NEEDS_REVIEW, "ai_json", "json")
    assert (qs[1].answer_source, qs[1].correct_answer, qs[1].explanation_status) == ("json", "B", "unverified")
    assert qs[1].uncertain is True and set(qs[1].ocr_flags.split(",")) == {"ai_uncertain", "ai_answer"}
    assert (qs[2].correct_answer, qs[2].answer_source, qs[2].ocr_flags) == (None, None, None)
    assert qs[3].has_image is True and "image_needed" in qs[3].ocr_flags and qs[3].explanation_status is None
    assert qs[1].subject_id == db.query(models.Subject).filter_by(name="Polity").one().id
    assert qs[1].text.startswith("Sample question number 1")
    entry = db.query(models.AuditLog).filter_by(action="paper.json_import", paper_id=paper.id).one()
    detail = json.loads(entry.detail_json)
    assert detail["created"] == 3 and detail["new_paper"] is True and detail["backup"] and detail["parts"][0]["name"] == "part1.json"
    assert set(os.listdir(backup.BACKUP_DIR)) - backups_before                              # a backup was taken first
    assert not os.path.isdir(os.path.join(os.environ["UPSC_DATA_DIR"], "json_imports", token))
    assert "AI-supplied, unverified" in admin.get(f"/review/{paper.id}").text


def test_the_staged_files_are_gone_and_a_token_cannot_be_reused(admin):
    token = token_of(validate(admin, ("a.json", doc([q(1)]))))
    assert apply(admin, token).status_code == 303
    assert apply(admin, token).status_code == 404
    for bad in ("../../etc", "0" * 31, "z" * 32, ""):
        assert admin.post("/admin/import/json/apply", data={"token": bad, "title": "x"}).status_code in (404, 422)


def test_nothing_from_json_can_go_live_until_it_is_reviewed(admin, db, make_user):
    token = token_of(validate(admin, ("a.json", doc([q(i, explanation="An AI explanation.") for i in range(1, 6)]))))
    title = f"Gated JSON paper {next(_run)}"
    apply(admin, token, title=title)
    paper = paper_by_title(db, title)
    admin.post(f"/papers/{paper.id}/publish")
    db.rollback()
    assert db.get(models.Paper, paper.id).publish_status == "draft"                              # blocked: nothing reviewed
    assert pool.live_questions(db).filter(models.Question.paper_id == paper.id).count() == 0

    # The bulk confirm skips AI-supplied answers — even if someone cleared their warning flags first.
    assert "Confirm all" not in admin.get(f"/review/{paper.id}").text
    for x in db.query(models.Question).filter_by(paper_id=paper.id):
        x.ocr_flags = None
    db.commit()
    assert "Confirm all" not in admin.get(f"/review/{paper.id}").text
    admin.post(f"/review/{paper.id}/confirm-clean")
    db.rollback()
    assert all(x.status == models.QStatus.NEEDS_REVIEW for x in db.query(models.Question).filter_by(paper_id=paper.id))

    # Individual confirmation is the way through.
    for x in questions_by_number(db, paper).values():
        assert admin.post(f"/review/{paper.id}/question/{x.id}", data={
            "text": x.text, "option_a": x.option_a, "option_b": x.option_b, "option_c": x.option_c, "option_d": x.option_d,
            "correct_answer": x.correct_answer, "subject_id": str(x.subject_id or "")}).status_code == 303
    admin.post(f"/papers/{paper.id}/publish")                                                    # five unedited confirmations: audit first
    db.rollback()
    assert db.get(models.Paper, paper.id).publish_status == "draft"
    pass_audit(db, paper)
    assert admin.post(f"/papers/{paper.id}/publish").status_code == 303
    db.rollback()
    live =pool.live_questions(db).filter(models.Question.paper_id == paper.id).all()
    assert len(live) == 5
    assert engine.explanation_label(live[0]) == "AI-supplied, unverified"                        # students are told what it is


def test_the_review_page_labels_ai_questions_and_their_answers(admin, db):
    token = token_of(validate(admin, ("a.json", doc([q(1, explanation="An AI explanation.")]))))
    title = f"Labelled JSON paper {next(_run)}"
    apply(admin, token, title=title)
    page = admin.get(f"/review/{paper_by_title(db, title).id}").text
    assert "AI-supplied, unverified." in page and "AI-supplied</span>" in page and "AI-supplied — check it" in page
    assert "Explanation (AI-supplied, unverified)" in page
    assert "check it against the printed key before confirming" in page


def latest_token():
    """The staging folder of the most recent validation (the error page deliberately offers no import form to take it from)."""
    from app.routes import json_import as route
    folders = [os.path.join(route.IMPORT_DIR, n) for n in os.listdir(route.IMPORT_DIR)]
    return os.path.basename(max(folders, key=os.path.getmtime))


def test_an_import_with_errors_is_refused_even_if_the_token_is_posted_directly(admin, db):
    validate(admin, ("a.json", doc([q(1, correct_answer="z")])))
    token = latest_token()
    before = db.query(models.Paper).count()
    r = apply(admin, token)
    assert r.status_code == 400 and "can&#39;t go ahead until the errors" in r.text
    db.rollback()
    assert db.query(models.Paper).count() == before


def test_a_new_paper_needs_a_title_and_a_valid_scheme_and_source(admin, db):
    token = token_of(validate(admin, ("a.json", doc([q(1)]))))
    assert "Give the new paper a title" in admin.post("/admin/import/json/apply", data={"token": token, "title": " ", "exam_type": "full_length"}).text
    assert "Marks per question must be a positive number" in apply(admin, token, marks_per_question="zero").text
    assert "Choose Official PYQ or Coaching test" in apply(admin, token, source_type="mystery").text
    assert "check the paper type" in apply(admin, token, exam_type="weird").text.lower()
    assert apply(admin, token).status_code == 303                                                # the token survived every refusal


def test_importing_the_same_json_twice_is_refused_unless_asked(admin, db):
    content = doc([q(1, question=f"A question that is only in this test, run {next(_run)}, honestly?")])
    first = token_of(validate(admin, ("a.json", content)))
    assert apply(admin, first).status_code == 303
    second = token_of(validate(admin, ("a.json", content)))
    r = apply(admin, second)
    assert r.status_code == 400 and "already imported as" in r.text
    assert apply(admin, second, allow_duplicate="true").status_code == 303


def test_a_paper_for_the_same_test_is_refused_unless_asked(admin, db):
    name = f"Institute {next(_run)}"
    first = token_of(validate(admin, ("a.json", doc([q(1, question=f"Question for test 7 of {name}, first copy?")]))))
    assert apply(admin, first, source_name=name, test_name="Prelims", test_number="7").status_code == 303
    second = token_of(validate(admin, ("a.json", doc([q(1, question=f"Question for test 7 of {name}, another copy?")]))))
    r = apply(admin, second, source_name=name, test_name="Prelims", test_number="7")
    assert r.status_code == 400 and "A paper for this test already exists" in r.text


def test_multiple_parts_with_a_conflict_import_only_when_the_later_part_wins(admin, db):
    a, b = doc([q(1, correct_answer="a"), q(2)]), doc([q(1, correct_answer="d"), q(3)])
    blocked = validate(admin, ("p1.json", a), ("p2.json", b))
    assert "1 error" in blocked.text and 'action="/admin/import/json/apply"' not in blocked.text
    token = token_of(validate(admin, ("p1.json", a), ("p2.json", b), later_wins=True))
    title = f"Later wins paper {next(_run)}"
    assert apply(admin, token, title=title, later_wins="true").status_code == 303
    qs = questions_by_number(db, paper_by_title(db, title))
    assert sorted(qs) == [1, 2, 3] and qs[1].correct_answer == "D"


def test_topics_in_json_reuse_existing_ones_and_never_create_new_ones(admin, db):
    polity = db.query(models.Subject).filter_by(name="Polity").one()
    existing = models.Topic(name=f"JSON Existing Topic {next(_run)}", subject_id=polity.id)
    db.add(existing)
    db.commit()
    topics_before = db.query(models.Topic).count()
    token = token_of(validate(admin, ("a.json", doc([q(1, topic=existing.name.upper()), q(2, topic="Brand New Topic")]))))
    title = f"Topic paper {next(_run)}"
    apply(admin, token, title=title)
    qs = questions_by_number(db, paper_by_title(db, title))
    assert qs[1].topic_id == existing.id and qs[2].topic_id is None
    assert db.query(models.Topic).count() == topics_before


def test_text_from_json_is_escaped_in_the_report_and_the_review_page(admin, db):
    evil = doc([q(1, question="<script>alert(1)</script> Which one is right, honestly?", explanation="<img src=x onerror=alert(2)>")])
    r = validate(admin, ("evil.json", evil))
    assert "<script>alert(1)" not in r.text and "&lt;script&gt;alert(1)&lt;/script&gt;" in r.text
    title = f"Escaped paper {next(_run)}"
    apply(admin, token_of(r), title=title)
    page = admin.get(f"/review/{paper_by_title(db, title).id}").text
    assert "<script>alert(1)" not in page and "<img src=x" not in page


def test_old_staged_imports_are_swept(admin):
    from app.routes import json_import as route
    old = os.path.join(route.IMPORT_DIR, "a" * 32)
    os.makedirs(old, exist_ok=True)
    stale = os.path.getmtime(old) - route.KEEP_SECONDS - 100
    os.utime(old, (stale, stale))
    validate(admin, ("a.json", doc([q(1)])))
    assert not os.path.exists(old)


# =========================================================================== adding to an existing paper

def existing_paper_with(db, admin, count=3):
    token = token_of(validate(admin, ("base.json", doc([q(i, question=f"Base question {i} in run {next(_run)}, original wording?") for i in range(1, count + 1)]))))
    title = f"Base paper {next(_run)}"
    apply(admin, token, title=title)
    return paper_by_title(db, title)


def test_adding_to_an_existing_paper_skips_numbers_it_already_has(admin, db):
    paper = existing_paper_with(db, admin)
    original = {n: x.text for n, x in questions_by_number(db, paper).items()}
    r = validate(admin, ("more.json", doc([q(2, question="A replacement for two that must be ignored here?"), q(4), q(5)])), target=str(paper.id))
    assert "already exist in" in r.text and "will be skipped" in r.text and "Adding to" in r.text
    done = apply(admin, token_of(r))
    assert done.status_code == 303 and done.headers["location"] == f"/review/{paper.id}"
    qs = questions_by_number(db, paper)
    assert sorted(qs) == [1, 2, 3, 4, 5] and qs[2].text == original[2]                      # 2 untouched; 4 and 5 added
    detail = json.loads(db.query(models.AuditLog).filter_by(action="paper.json_import", paper_id=paper.id)
                        .order_by(models.AuditLog.id.desc()).first().detail_json)
    assert (detail["created"], detail["skipped_existing"], detail["overwritten"], detail["new_paper"]) == (2, 1, 0, False)


def test_overwriting_replaces_only_questions_still_waiting_for_review_and_keeps_their_history(admin, db):
    paper = existing_paper_with(db, admin)
    qs = questions_by_number(db, paper)
    qs[3].status = models.QStatus.VERIFIED                                                       # already confirmed by an admin
    verified_text = qs[3].text
    old_text = qs[1].text
    db.commit()
    r = validate(admin, ("fix.json", doc([q(1, question="The corrected wording of question one, properly?"),
                                          q(3, question="An attempt to replace a verified question, no?")])), target=str(paper.id))
    assert apply(admin, token_of(r), overwrite_needs_review="true").status_code == 303
    qs = questions_by_number(db, paper)
    assert qs[1].text.startswith("The corrected wording") and qs[3].text == verified_text
    assert qs[3].status == models.QStatus.VERIFIED and qs[1].status == models.QStatus.NEEDS_REVIEW and qs[1].source == "ai_json"
    versions = db.query(models.QuestionVersion).filter_by(question_id=qs[1].id).all()
    assert len(versions) == 1 and old_text in versions[0].snapshot_json and versions[0].reason == "replaced by JSON import"
    assert not db.query(models.QuestionVersion).filter_by(question_id=qs[3].id).count()


def test_without_the_tick_nothing_is_overwritten(admin, db):
    paper = existing_paper_with(db, admin)
    before = {n: x.text for n, x in questions_by_number(db, paper).items()}
    r = validate(admin, ("fix.json", doc([q(1, question="Some other wording for the first question, right?")])), target=str(paper.id))
    apply(admin, token_of(r))
    assert {n: x.text for n, x in questions_by_number(db, paper).items()} == before


def test_an_archived_paper_cannot_be_added_to(admin, db):
    paper = existing_paper_with(db, admin)
    admin.post(f"/papers/{paper.id}/archive")
    assert admin.post("/admin/import/json/validate", data={"target": str(paper.id), "pasted": doc([q(9)])}).status_code == 404


# =========================================================================== the optional PDF

def page_pdf(pages=3):
    return make_pdf([flow([f"Page {n} of the original paper", "1. Some printed text"], 60, 60)[0] for n in range(1, pages + 1)])


def test_a_pdf_gives_the_admin_the_original_page_but_never_a_student(admin, db, make_user):
    r = validate(admin, ("a.json", doc([q(1, page=1), q(2, page=3), q(3, page=99), q(4)])), pdf=page_pdf(3))
    title = f"With PDF paper {next(_run)}"
    assert apply(admin, token_of(r), title=title).status_code == 303
    paper = paper_by_title(db, title)
    qs = questions_by_number(db, paper)
    assert (qs[1].page_number, qs[2].page_number, qs[3].page_number, qs[4].page_number) == (1, 3, 99, None)
    assert paper.source_pdf_path and os.path.exists(paper.source_pdf_path)
    from app import ingest
    folder = ingest.images_dir_for(paper.id)
    assert sorted(f for f in os.listdir(folder) if f.startswith("page")) == ["page1.jpg", "page3.jpg"]     # 99 is beyond the PDF
    assert admin.get(f"/media/{paper.id}/page1.jpg").status_code == 200
    assert admin.get(f"/media/{paper.id}/page2.jpg").status_code == 404
    review = admin.get(f"/review/{paper.id}").text
    assert "Original page 1" in review and "Original page 3" in review and "Original page 99" not in review
    assert make_user("pagepicstudent").get(f"/media/{paper.id}/page1.jpg").status_code == 404
    assert audit_pictures(db, paper) == 2


def audit_pictures(db, paper):
    entry = db.query(models.AuditLog).filter_by(action="paper.json_import", paper_id=paper.id).one()
    return json.loads(entry.detail_json)["pdf"]["page_pictures"]


def test_page_pictures_never_leak_even_from_a_published_paper(admin, db, make_user):
    r = validate(admin, ("a.json", doc([q(1, page=1)])), pdf=page_pdf(1))
    title = f"Live with PDF paper {next(_run)}"
    apply(admin, token_of(r), title=title)
    paper = paper_by_title(db, title)
    x = questions_by_number(db, paper)[1]
    admin.post(f"/review/{paper.id}/question/{x.id}", data={"text": x.text, "option_a": x.option_a, "option_b": x.option_b,
                                                            "option_c": x.option_c, "option_d": x.option_d, "correct_answer": "B"})
    admin.post(f"/papers/{paper.id}/publish")
    student = make_user("pagepicstudent2")
    assert student.get(f"/media/{paper.id}/page1.jpg").status_code == 404
    assert student.get(f"/media/{paper.id}/q1.jpg").status_code == 404                           # and there is no per-question snapshot


def test_an_image_question_from_json_stays_out_of_tests_because_it_has_no_snapshot(admin, db):
    r = validate(admin, ("a.json", doc([q(1, has_image=True, question="Look at the figure [IMAGE] and answer this one?"), q(2)])))
    title = f"Image JSON paper {next(_run)}"
    apply(admin, token_of(r), title=title)
    paper = paper_by_title(db, title)
    for x in questions_by_number(db, paper).values():
        admin.post(f"/review/{paper.id}/question/{x.id}", data={"text": x.text, "option_a": x.option_a, "option_b": x.option_b,
                                                                "option_c": x.option_c, "option_d": x.option_d, "correct_answer": "B",
                                                                "has_image": "true" if x.has_image else ""})
    admin.post(f"/papers/{paper.id}/publish")
    db.rollback()
    live = {x.question_number for x in pool.live_questions(db).filter(models.Question.paper_id == paper.id)}
    assert live == {2}


def test_the_pages_accept_any_number_of_parts(admin):
    parts = [(f"part{i}.json", doc([q(i, question=f"Part {i} question that is only in this test run {next(_run)}?")])) for i in range(1, 31)]
    r = validate(admin, *parts)
    assert r.status_code == 200 and "30 questions can be imported" in r.text and "No errors." in r.text
    assert "At most" not in r.text and "Limits:" not in r.text
    assert "There is no limit on the number of parts" in admin.get("/admin/import/json").text
