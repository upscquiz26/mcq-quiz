"""
Import stage 3: answer keys — the parser, the key block at the end of a paper, and the preview / apply page.
"""
import itertools
import json
import os
import re

import pytest

from app import key_parse as kp, models, text_extract as te
from app.models import QStatus
from app.practice import pool
from conftest import _client
from pdfmaker import flow, make_pdf
from test_text_extract import LEFT_X, form, pdf_files, question_lines

_n = itertools.count(1)


def answers(text, series=None):
    return dict(sorted(kp.parse_key_text(text, series).answers.items()))


# =========================================================================== the parser

@pytest.mark.parametrize("text, expected", [
    ("1-b 2-d 3-a 4-c", {1: "B", 2: "D", 3: "A", 4: "C"}),
    ("1. (b)\n2. (d)\n3. (a)", {1: "B", 2: "D", 3: "A"}),
    ("Q1: B  Q2: C  Q3: A", {1: "B", 2: "C", 3: "A"}),
    ("Question 1: (b)\nQuestion 2: (c)", {1: "B", 2: "C"}),
    ("1) b 2) c 3) a 4) d", {1: "B", 2: "C", 3: "A", 4: "D"}),
    ("1 b 2 d 3 a 4 c 5 b", {1: "B", 2: "D", 3: "A", 4: "C", 5: "B"}),
    ("1b 2d 3a 4c", {1: "B", 2: "D", 3: "A", 4: "C"}),
    ("1=a, 2=b, 3=c", {1: "A", 2: "B", 3: "C"}),
    ("Q.No. | Answer\n1 | b\n2 | d\n3 | a", {1: "B", 2: "D", 3: "A"}),
    ("1-B\n2-D\n\n\n3-A", {1: "B", 2: "D", 3: "A"}),
    ("  1 - b ,  2 - d ,  3 - a  ", {1: "B", 2: "D", 3: "A"}),
    ("100-a 101-b", {100: "A", 101: "B"}),
])
def test_the_common_ways_of_writing_a_key_are_all_read(text, expected):
    result = kp.parse_key_text(text)
    assert dict(sorted(result.answers.items())) == expected and result.ok and not result.errors


def test_only_letters_a_to_e_are_answers_and_digits_are_refused():
    r = kp.parse_key_text("1-3 2-1 3-4 4-2")
    assert not r.ok and r.answers == {} and "uses numbers (1–4)" in r.errors[0].message
    assert answers("1-e 2-f 3-b") == {1: "E", 3: "B"}                           # e is (books have five options); f isn't
    assert answers("1-b 2-z 3-c") == {1: "B", 3: "C"}


def test_a_number_with_two_different_answers_is_an_error_naming_it():
    r = kp.parse_key_text("1-b 2-c 1-d 2-c 3-a")
    assert not r.ok and [e.number for e in r.errors] == [1] and "Question 1 is given more than one answer (B, D)" in r.errors[0].message
    assert kp.parse_key_text("1-b 2-c 1-b").ok                                  # the same answer twice is harmless


def test_empty_and_unrecognisable_keys_say_so():
    assert "The key is empty" in kp.parse_key_text("   \n").errors[0].message
    r = kp.parse_key_text("hello world, this is not a key")
    assert not r.ok and "No answers were found" in r.errors[0].message


def test_numbers_are_not_confused_with_years_or_words():
    assert answers("Published in 2023 and 1-a 2-b 3-c") == {1: "A", 2: "B", 3: "C"}
    assert answers("1 apple 2 banana 3-a") == {3: "A"}


SERIES_TABLE = "Q  A B C D\n1  b c a d\n2  a a b c\n3  d b c a\n4  c d a b\n"
SERIES_BLOCKS = "Series A\n1-b 2-d 3-a\nSeries B\n1-c 2-a 3-b\nSeries C\n1-a 2-b 3-d\n"


@pytest.mark.parametrize("series, expected", [
    ("A", {1: "B", 2: "A", 3: "D", 4: "C"}), ("B", {1: "C", 2: "A", 3: "B", 4: "D"}),
    ("c", {1: "A", 2: "B", 3: "C", 4: "A"}), ("D", {1: "D", 2: "C", 3: "A", 4: "B"}),
])
def test_a_series_table_gives_the_column_of_the_papers_series(series, expected):
    r = kp.parse_key_text(SERIES_TABLE, series)
    assert r.ok and dict(sorted(r.answers.items())) == expected and r.series == series.upper() and "series table" in r.format


def test_series_blocks_give_the_block_of_the_papers_series():
    assert answers(SERIES_BLOCKS, "B") == {1: "C", 2: "A", 3: "B"}
    assert kp.parse_key_text(SERIES_BLOCKS, "C").series == "C"
    r = kp.parse_key_text(SERIES_BLOCKS, "D")
    assert not r.ok and "has blocks for series A, B, C but not for this paper's series D" in r.errors[0].message


@pytest.mark.parametrize("text", [SERIES_TABLE, SERIES_BLOCKS])
def test_a_key_with_several_series_needs_the_papers_series(text):
    r = kp.parse_key_text(text, None)
    assert not r.ok and "no series set" in r.errors[0].message and r.answers == {}


def test_labelled_lines_give_answers_and_explanations():
    text = "1. Ans– (b)\nBecause of X.\n2. Ans– (c)\nBecause of Y.\nPage No. 2\n3. Ans- (a)\nBecause of Z."
    r = kp.parse_key_text(text)
    assert r.ok and r.answers == {1: "B", 2: "C", 3: "A"} and "labelled lines" in r.format
    assert r.explanations == {1: "Because of X.", 2: "Because of Y.", 3: "Because of Z."}


def test_the_existing_key_pdf_reader_is_unchanged(tmp_path):
    from app.answer_key import parse_answer_key
    lines, _ = flow(["1. Ans- (b)", "Explanation one.", "2. Ans- (d)", "Explanation two.", "3. Ans- (a)", "Explanation three."], 60, 60)
    path = tmp_path / "k.pdf"
    path.write_bytes(make_pdf([lines]))
    assert {n: v["answer"] for n, v in parse_answer_key(str(path)).items()} == {1: "B", 2: "D", 3: "A"}
    assert kp.parse_key_pdf(str(path)).answers == {1: "B", 2: "D", 3: "A"}


def test_a_scanned_key_pdf_is_refused_with_advice(tmp_path):
    from conftest import blank_pdf_bytes
    path = tmp_path / "scan.pdf"
    path.write_bytes(blank_pdf_bytes())
    r = kp.parse_key_pdf(str(path))
    assert not r.ok and "no text layer" in r.errors[0].message and "paste" in r.errors[0].message.lower()


def test_uploaded_bytes_may_be_a_text_file_in_common_encodings():
    assert kp.parse_key_bytes("1-a 2-b".encode("utf-8-sig"), "k.txt").answers == {1: "A", 2: "B"}
    assert kp.parse_key_bytes("1-a 2-b".encode("utf-16"), "k.txt").answers == {1: "A", 2: "B"}
    assert kp.parse_key_bytes(b"1-a 2-b\r\n3-c", "k.txt").answers == {1: "A", 2: "B", 3: "C"}


@pytest.mark.parametrize("explanation, says", [
    ("Hence, option (c) is correct.", "C"), ("The correct answer is (b) because it follows.", "B"),
    ("Thus the desired answer is option (d).", "D"), ("Therefore option (a) is the correct one.", "A"),
    ("A is true and B is false, so 1 only.", None), ("Answer: (d) is stated in the key.", None),
    ("The correct answer is (a), but option (c) is correct.", None),      # contradicts itself: no opinion
    ("", None), (None, None),
])
def test_what_an_explanation_says_about_the_answer(explanation, says):
    assert kp.explanation_says(explanation) == says


# =========================================================================== a key block at the end of the question PDF

def paper_with_key_block(count=6, key="1-A 2-B 3-C 4-D 5-A 6-B", heading="ANSWER KEY"):
    lines = sum((question_lines(n, None) for n in range(1, count + 1)), [])
    lines += [heading, key] if heading else []
    items, _ = flow(lines, LEFT_X, 60)
    return make_pdf([items])


def test_a_key_block_after_the_last_question_is_found_and_kept_out_of_option_d(tmp_path):
    path = tmp_path / "p.pdf"
    path.write_bytes(paper_with_key_block())
    questions, warnings = te.extract_questions(str(path), str(tmp_path / "img"))
    assert len(questions) == 6 and warnings == []
    assert questions[-1]["option_d"] == "Neither 1 nor 2"                                # not "…ANSWER KEY 1-A 2-B …"
    assert "1-A 2-B 3-C 4-D 5-A 6-B" in questions.key_block
    assert te.find_key_block(str(path)) is not None and "3-C" in te.find_key_block(str(path))
    assert kp.parse_key_text(questions.key_block).answers == {1: "A", 2: "B", 3: "C", 4: "D", 5: "A", 6: "B"}


def test_a_heading_without_enough_answers_is_not_a_key_block(tmp_path):
    path = tmp_path / "p.pdf"
    path.write_bytes(paper_with_key_block(key="1-A 2-B"))
    questions, _ = te.extract_questions(str(path), str(tmp_path / "img"))
    assert questions.key_block is None


def test_a_paper_without_a_key_block_has_none(tmp_path):
    path = tmp_path / "p.pdf"
    path.write_bytes(paper_with_key_block(heading=None))
    questions, _ = te.extract_questions(str(path), str(tmp_path / "img"))
    assert questions.key_block is None and te.find_key_block(str(path)) is None


# =========================================================================== the page

def text_of(html_text):
    import html
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", html_text))).strip()


def bare_paper(db, make_paper, n=6, answers=None, series=None, **fields):
    """A paper whose questions have NO answers (or the given letters), all waiting for review."""
    paper = make_paper(f"Key paper {next(_n)}", n=n, series=series, **fields)
    db.rollback()
    for q in db.query(models.Question).filter_by(paper_id=paper.id):
        letter = (answers or {}).get(q.question_number)
        q.correct_answer, q.answer_source = letter, ("manual" if letter else None)
    db.commit()
    return paper


def questions_of(db, paper):
    db.rollback()
    return {q.question_number: q for q in db.query(models.Question).filter_by(paper_id=paper.id)}


def preview(admin, paper, **data):
    return admin.post(f"/review/{paper.id}/key/preview", data=data)


def token_of(response):
    m = re.search(r'name="token" value="([0-9a-f]{32})"', response.text)
    assert m, text_of(response.text)[:600]
    return m.group(1)


def apply(admin, paper, token, source="institute answer sheet", **extra):
    return admin.post(f"/review/{paper.id}/key/apply", data={"token": token, "key_source": source, **extra})


def test_the_key_page_and_the_review_page_link_to_each_other(admin, db, make_paper):
    paper = bare_paper(db, make_paper)
    review = admin.get(f"/review/{paper.id}")
    assert f'href="/review/{paper.id}/key"' in review.text and "0 of 6 questions have one" in text_of(review.text)
    page = admin.get(f"/review/{paper.id}/key")
    assert page.status_code == 200 and "Answers this paper has now" in page.text and "0</strong> of <strong>6" in page.text
    assert "6</strong> don't, and can't be published until they do" in page.text.replace("\n", "")


def test_only_admins_can_use_the_key_pages(make_user, anon, db, make_paper):
    paper = bare_paper(db, make_paper)
    student = make_user("keystudent")
    for method, url in (("get", f"/review/{paper.id}/key"), ("post", f"/review/{paper.id}/key/preview"), ("post", f"/review/{paper.id}/key/apply")):
        assert getattr(student, method)(url).status_code == 403
        r = getattr(anon, method)(url)
        assert r.status_code == 303 and r.headers["location"].startswith("/login")


def test_a_preview_changes_nothing_and_says_what_would_happen(admin, db, make_paper):
    paper = bare_paper(db, make_paper, answers={1: "A", 2: "B", 3: "D"})               # 1 and 2 already right, 3 differs, 4-6 empty
    r = preview(admin, paper, pasted="1-a 2-b 3-c 4-d 5-a 6-b 7-c")
    page = text_of(r.text)
    assert r.status_code == 200 and "detected: answer pairs" in page
    assert "Answers in the key 7 the paper has 6 questions" in page and "Would be filled in 3" in page
    assert "Already the same 2" in page and "Different from now 1" in page
    assert "Ignored — the paper has no such question: 7" in page
    assert "Q3 D C waiting for review" in page                                        # the change, current then new
    assert {n: q.correct_answer for n, q in questions_of(db, paper).items()} == {1: "A", 2: "B", 3: "D", 4: None, 5: None, 6: None}
    assert not db.query(models.AuditLog).filter_by(action="key.apply", paper_id=paper.id).count()


def test_applying_fills_only_the_empty_ones_by_default_and_records_where_the_key_came_from(admin, db, make_paper):
    paper = bare_paper(db, make_paper, answers={1: "A", 3: "D"})
    token = token_of(preview(admin, paper, pasted="1-a 2-b 3-c 4-d 5-a 6-b"))
    done = apply(admin, paper, token, source="UPSC final key", key_version="final")
    assert done.status_code == 303 and done.headers["location"] == f"/review/{paper.id}"
    got = {n: (q.correct_answer, q.answer_source) for n, q in questions_of(db, paper).items()}
    assert got == {1: ("A", "manual"), 2: ("B", "pasted"), 3: ("D", "manual"), 4: ("D", "pasted"), 5: ("A", "pasted"), 6: ("B", "pasted")}
    db.rollback()
    paper = db.get(models.Paper, paper.id)
    assert (paper.key_source, paper.key_version) == ("UPSC final key", "final")
    detail = json.loads(db.query(models.AuditLog).filter_by(action="key.apply", paper_id=paper.id).one().detail_json)
    assert (detail["filled"], detail["replaced"], detail["kept"], detail["unchanged"]) == (4, 0, 1, 1)
    assert detail["source"] == "UPSC final key" and detail["from"] == "pasted text" and len(detail["sha256"]) == 16
    message = text_of(admin.get(f"/review/{paper.id}").text)
    assert "Answer key applied: 4 filled" in message and "1 left as they were" in message
    assert "Key source: UPSC final key · final" in message
    assert "6 of 6 questions have one (4 from a pasted key, 2 from typed by hand)" in message


def test_replacing_needs_the_tick_and_sends_confirmed_questions_back_to_review(admin, db, make_paper):
    paper = bare_paper(db, make_paper, answers={1: "A", 2: "A", 3: "A"})
    qs = questions_of(db, paper)
    qs[1].status, qs[2].status = QStatus.VERIFIED, QStatus.LIVE                       # 3 stays waiting for review
    db.commit()
    token = token_of(preview(admin, paper, pasted="1-b 2-c 3-d"))
    apply(admin, paper, token, replace="true")
    qs = questions_of(db, paper)
    assert [(qs[n].correct_answer, qs[n].status) for n in (1, 2, 3)] == [("B", QStatus.NEEDS_REVIEW), ("C", QStatus.NEEDS_REVIEW), ("D", QStatus.NEEDS_REVIEW)]
    assert all(qs[n].reviewed_by is None and qs[n].flags_acknowledged is False for n in (1, 2, 3))
    versions = db.query(models.QuestionVersion).filter(models.QuestionVersion.question_id.in_([q.id for q in qs.values()])).all()
    assert len(versions) == 3 and all(v.reason == "answer key applied" and '"correct_answer": "A"' in v.snapshot_json for v in versions)
    detail = json.loads(db.query(models.AuditLog).filter_by(action="key.apply", paper_id=paper.id).one().detail_json)
    assert (detail["replaced"], detail["sent_back"], detail["replace"]) == (3, 2, True)
    assert pool.live_questions(db).filter(models.Question.paper_id == paper.id).count() == 0        # the live one is out of circulation


def test_a_source_is_required_to_apply_a_key(admin, db, make_paper):
    paper = bare_paper(db, make_paper)
    token = token_of(preview(admin, paper, pasted="1-a 2-b"))
    r = admin.post(f"/review/{paper.id}/key/apply", data={"token": token, "key_source": "  "})
    assert r.status_code == 303 and r.headers["location"] == f"/review/{paper.id}/key"
    assert "Say where this key came from" in text_of(admin.get(f"/review/{paper.id}/key").text)
    assert all(q.correct_answer is None for q in questions_of(db, paper).values())
    assert apply(admin, paper, token).status_code == 303                                # the same preview still works afterwards


def test_a_key_with_errors_shows_them_and_cannot_be_applied(admin, db, make_paper):
    paper = bare_paper(db, make_paper)
    r = preview(admin, paper, pasted="1-a 2-b 1-c")
    page = text_of(r.text)
    assert "1 problem — this key can't be applied" in page and "Question 1 is given more than one answer (A, C)" in page
    assert 'action="/review/%d/key/apply"' % paper.id not in r.text
    assert 'name="token"' not in r.text                                              # no token is offered, so there is nothing to apply


def test_a_forced_apply_of_a_key_with_errors_is_refused(admin, db, make_paper):
    paper = bare_paper(db, make_paper)
    from app.routes import keys
    preview(admin, paper, pasted="1-a 2-b 1-c")
    newest = max((os.path.join(keys.KEY_DIR, n) for n in os.listdir(keys.KEY_DIR)), key=os.path.getmtime)
    assert apply(admin, paper, os.path.basename(newest)).status_code == 400
    assert all(q.correct_answer is None for q in questions_of(db, paper).values())


def test_the_warnings_the_plan_promised(admin, db, make_paper):
    paper = bare_paper(db, make_paper, n=12)
    lopsided = "1-a 2-a 3-a 4-a 5-a 6-a 7-a 8-b 9-c 10-d"                              # 70% A, and 2 short of 12
    page = text_of(preview(admin, paper, pasted=lopsided).text)
    assert "More than half of the key (70%) is “A”" in page
    assert "The key has 10 answers but the paper has 12 questions" in page
    assert "No answer in the key for: 11, 12" in page
    balanced = text_of(preview(admin, paper, pasted="1-a 2-b 3-c 4-d 5-a 6-b 7-c 8-d 9-a 10-b 11-c 12-d").text)
    assert "More than half" not in balanced and "has 12 answers but" not in balanced and "No answer in the key" not in balanced
    few = bare_paper(db, make_paper, n=5)
    assert "More than half" not in text_of(preview(admin, few, pasted="1-a 2-a 3-a 4-a 5-a").text)     # too few to judge


def test_the_review_page_warns_about_a_lopsided_paper_and_offers_the_link(admin, db, make_paper):
    paper = bare_paper(db, make_paper, n=10, answers={i: "C" for i in range(1, 9)} | {9: "A", 10: "B"})
    page = text_of(admin.get(f"/review/{paper.id}").text)
    assert "80% of the answers are “C” — that's unusual" in page


def test_uploading_a_text_key_file(admin, db, make_paper):
    paper = bare_paper(db, make_paper)
    r = admin.post(f"/review/{paper.id}/key/preview", files={"key_file": ("my key.txt", b"1-c 2-a 3-b 4-d 5-c 6-a", "text/plain")})
    assert "From my key.txt" in text_of(r.text)
    apply(admin, paper, token_of(r), source="Sharma answer sheet")
    got = questions_of(db, paper)
    assert [got[n].correct_answer for n in range(1, 7)] == list("CABDCA") and {q.answer_source for q in got.values()} == {"key_pdf"}


def test_uploading_a_key_pdf_with_explanations_fills_gaps_only_and_keeps_them_unverified(admin, db, make_paper):
    paper = bare_paper(db, make_paper, n=3)
    qs = questions_of(db, paper)
    qs[2].explanation, qs[2].explanation_status = "Already explained.", "verified"
    db.commit()
    lines, _ = flow(["1. Ans- (b)", "Fresh explanation one.", "2. Ans- (c)", "Should not overwrite.", "3. Ans- (a)", "Fresh explanation three."], 60, 60)
    r = admin.post(f"/review/{paper.id}/key/preview", files={"key_file": ("k.pdf", make_pdf([lines]), "application/pdf")})
    assert "carries 3 explanations" in text_of(r.text)
    apply(admin, paper, token_of(r))
    got = questions_of(db, paper)
    assert (got[1].explanation, got[1].explanation_status) == ("Fresh explanation one.", "unverified")
    assert (got[2].explanation, got[2].explanation_status) == ("Already explained.", "verified")
    assert (got[3].explanation, got[3].explanation_status) == ("Fresh explanation three.", "unverified")
    assert [got[n].correct_answer for n in (1, 2, 3)] == ["B", "C", "A"]


def test_a_scanned_key_file_is_refused(admin, db, make_paper):
    from conftest import blank_pdf_bytes
    paper = bare_paper(db, make_paper)
    r = admin.post(f"/review/{paper.id}/key/preview", files={"key_file": ("scan.pdf", blank_pdf_bytes(), "application/pdf")})
    assert "no text layer" in text_of(r.text) and 'name="token"' not in r.text


def test_series_tables_use_the_papers_series_and_ask_for_it_when_missing(admin, db, make_paper):
    with_series = bare_paper(db, make_paper, n=4, series="B")
    r = preview(admin, with_series, pasted=SERIES_TABLE)
    assert "Used the column for series B" not in r.text and "series B" in text_of(r.text)
    apply(admin, with_series, token_of(r))
    assert [q.correct_answer for _, q in sorted(questions_of(db, with_series).items())] == ["C", "A", "B", "D"]
    without = bare_paper(db, make_paper, n=4)
    page = text_of(preview(admin, without, pasted=SERIES_TABLE).text)
    assert "no series set" in page and "Set the paper's series" in page


def test_one_source_at_a_time_and_at_least_one(admin, db, make_paper):
    paper = bare_paper(db, make_paper)
    assert "Paste a key, choose a key file" in text_of(admin.post(f"/review/{paper.id}/key/preview", data={}).text)
    r = admin.post(f"/review/{paper.id}/key/preview", data={"pasted": "1-a", "from_paper": "true"})
    assert r.status_code == 400 and "Use one source at a time" in text_of(r.text)
    r = admin.post(f"/review/{paper.id}/key/preview", data={"from_paper": "true"})
    assert r.status_code == 400 and "no question PDF on file" in text_of(r.text)          # this paper was made without one


def test_tokens_are_bound_to_their_paper_and_single_use(admin, db, make_paper):
    a, b = bare_paper(db, make_paper), bare_paper(db, make_paper)
    token = token_of(preview(admin, a, pasted="1-a 2-b"))
    assert admin.post(f"/review/{b.id}/key/apply", data={"token": token, "key_source": "x"}).status_code == 404
    assert all(q.correct_answer is None for q in questions_of(db, b).values())
    assert apply(admin, a, token).status_code == 303
    assert apply(admin, a, token).status_code == 404                                       # used up
    for bad in ("../../x", "0" * 31, "g" * 32):
        assert admin.post(f"/review/{a.id}/key/apply", data={"token": bad, "key_source": "x"}).status_code == 404


def test_quarantined_questions_are_left_alone(admin, db, make_paper):
    paper = bare_paper(db, make_paper, n=3)
    qs = questions_of(db, paper)
    qs[2].status = QStatus.QUARANTINED
    db.commit()
    page = text_of(preview(admin, paper, pasted="1-a 2-b 3-c").text)
    assert "the paper has 2 questions" in page and "Ignored — the paper has no such question: 2" in page
    apply(admin, paper, token_of(preview(admin, paper, pasted="1-a 2-b 3-c")))
    got = questions_of(db, paper)
    assert (got[1].correct_answer, got[2].correct_answer, got[3].correct_answer) == ("A", None, "C")


def test_text_from_the_user_is_never_echoed_unescaped(admin, db, make_paper):
    paper = bare_paper(db, make_paper)
    r = admin.post(f"/review/{paper.id}/key/preview", data={"pasted": "<script>alert(1)</script> 1-a 2-b"})
    assert "<script>alert(1)" not in r.text
    r = admin.post(f"/review/{paper.id}/key/preview", files={"key_file": ("<b>x</b>.txt", b"1-a 2-b", "text/plain")})
    assert "<b>x</b>" not in r.text and "<b>" not in r.text.split("From ")[1].split("·")[0] and "b&gt;.txt" in r.text   # a "/" in a file name is a path separator


# =========================================================================== the explanation-versus-key check

def test_a_questions_explanation_that_disagrees_with_the_key_is_flagged_and_cleared_when_fixed(admin, db, make_paper):
    paper = bare_paper(db, make_paper, n=3)
    qs = questions_of(db, paper)
    qs[1].explanation, qs[1].explanation_says = "Hence, option (c) is correct.", "C"
    qs[2].explanation, qs[2].explanation_says = "The correct answer is (b).", "B"
    db.commit()
    token = token_of(preview(admin, paper, pasted="1-a 2-b 3-d"))
    page = text_of(preview(admin, paper, pasted="1-a 2-b 3-d").text)
    assert "For 1 the question's explanation states a different answer from this key" in page
    apply(admin, paper, token)
    got = questions_of(db, paper)
    assert "explanation_mismatch" in got[1].ocr_flags and not (got[2].ocr_flags or "")
    review = text_of(admin.get(f"/review/{paper.id}").text)
    assert "The explanation states a different answer from the key" in review
    assert "1 question whose explanation states a different answer from the key (1)" in review

    # fixing the answer by hand to agree with the explanation clears the warning
    q1 = got[1]
    admin.post(f"/review/{paper.id}/question/{q1.id}", data={"text": q1.text, "option_a": q1.option_a, "option_b": q1.option_b,
                                                            "option_c": q1.option_c, "option_d": q1.option_d, "correct_answer": "C"})
    assert "explanation_mismatch" not in (questions_of(db, paper)[1].ocr_flags or "")


def test_an_answer_that_matches_its_explanation_gets_no_flag(admin, db, make_paper):
    paper = bare_paper(db, make_paper, n=2)
    qs = questions_of(db, paper)
    qs[1].explanation, qs[1].explanation_says = "Hence, option (c) is correct.", "C"
    db.commit()
    apply(admin, paper, token_of(preview(admin, paper, pasted="1-c 2-a")))
    assert "explanation_mismatch" not in (questions_of(db, paper)[1].ocr_flags or "")


# =========================================================================== import: key block note, mismatch flag, stored layout

def test_a_key_block_in_the_question_pdf_is_reported_never_applied_and_can_be_previewed(admin, db):
    r = admin.post("/upload", data=form(f"Key block paper {next(_n)}"), files=pdf_files(paper_with_key_block(key=f"1-A 2-B 3-C 4-D 5-A 6-B {next(_n)}-x")))
    assert r.status_code == 303
    db.rollback()
    paper = db.query(models.Paper).filter(models.Paper.title.like("Key block paper%")).order_by(models.Paper.id.desc()).first()
    assert "An answer key block was found at the end of the question PDF (6 answers)" in paper.status_message
    got = questions_of(db, paper)
    assert len(got) == 6 and all(q.correct_answer is None for q in got.values())              # nothing was applied by itself
    assert got[6].option_d == "Neither 1 nor 2"

    page = text_of(admin.get(f"/review/{paper.id}/key").text)
    assert "look for an “ANSWER KEY” block at the end of this paper's question PDF" in page
    r = preview(admin, paper, from_paper="true")
    assert "From the end of the question PDF" in text_of(r.text) and "Would be filled in 6" in text_of(r.text)
    apply(admin, paper, token_of(r), source="printed at the back of the paper")
    got = questions_of(db, paper)
    assert [got[n].correct_answer for n in range(1, 7)] == list("ABCDAB") and {q.answer_source for q in got.values()} == {"paper_end"}


def test_no_key_block_message_when_the_pdf_has_none(admin, db):
    admin.post("/upload", data=form(f"No block paper {next(_n)}"), files=pdf_files(paper_with_key_block(heading=None)))
    db.rollback()
    paper = db.query(models.Paper).filter(models.Paper.title.like("No block paper%")).order_by(models.Paper.id.desc()).first()
    assert paper.status_message is None
    r = preview(admin, paper, from_paper="true")
    assert r.status_code == 400 and "No answer-key block was found" in text_of(r.text)


def test_an_imported_answer_that_contradicts_its_explanation_is_flagged(admin, db):
    lines = []
    for n in range(1, 6):
        lines += question_lines(n, "ABCD", explanation=False)
        lines[-1:] = [lines[-1]]                                                         # the "Answer-(x)" line is already last
        lines += ["Hence, option (c) is correct."] if n == 2 else [f"Reason {n}."]
    items, _ = flow(lines, LEFT_X, 60)
    admin.post("/upload", data=form(f"Mismatch paper {next(_n)}"), files=pdf_files(make_pdf([items])))
    db.rollback()
    paper = db.query(models.Paper).filter(models.Paper.title.like("Mismatch paper%")).order_by(models.Paper.id.desc()).first()
    got = questions_of(db, paper)
    assert got[2].correct_answer == "B" and got[2].explanation_says == "C" and "explanation_mismatch" in got[2].ocr_flags
    assert not [n for n, q in got.items() if n != 2 and "explanation_mismatch" in (q.ocr_flags or "")]
    assert "The explanation states a different answer from the key" in admin.get(f"/review/{paper.id}").text


def test_the_chosen_page_layout_is_stored_on_the_paper(admin, db):
    admin.post("/upload", data=form(f"Layout kept paper {next(_n)}", layout="two"), files=pdf_files(paper_with_key_block()))
    db.rollback()
    paper = db.query(models.Paper).filter(models.Paper.title.like("Layout kept paper%")).order_by(models.Paper.id.desc()).first()
    assert paper.layout == "two"
