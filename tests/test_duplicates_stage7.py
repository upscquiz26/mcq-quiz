"""Import stage 7: duplicates — exact and near matching, keep both, merge (with 'also appeared in'), and the import hooks."""
import itertools
import random
import uuid

import pytest

from app import duplicates, ingest, models
from app.models import QStatus
from app.practice import pool
from conftest import make_student_client, question_form
from test_json_import import apply as json_apply, doc, q as json_q, token_of, validate

_run = itertools.count(1)
WORDS = ("treaty council river empire monsoon charter tribunal harvest census lagoon senate plateau dynasty ordinance glacier pilgrim "
         "manuscript tariff canal reform assembly frontier granite archive pension viceroy meridian ledger lantern voyage pillar "
         "outpost cabinet pageant vessel gazette district quorum foundry orchard steppe rampart chapter bureau novel harbor "
         "lighthouse pamphlet caravan mosaic ferry banner cliff tavern glossary bastion meadow parcel scroll anchor market").split()


def sentence(words=16):
    """A question stem made of random words: two of these are never mistaken for each other."""
    return "Which of the following statements about the " + " ".join(random.Random(uuid.uuid4().hex).sample(WORDS, words)) + " is correct?"


def tweak(text):
    """The same stem with one word changed: a near duplicate."""
    parts = text.split(" ")
    parts[8] = parts[8] + "s"
    return " ".join(parts)


OPTIONS = ("1 only", "2 only", "Both 1 and 2", "Neither 1 nor 2")


def paper_of(db, texts, options=OPTIONS, status=QStatus.NEEDS_REVIEW, **fields):
    paper = models.Paper(title=f"Dup paper {next(_run)}", exam_type=models.ExamType.full_length, status="ready", **fields)
    db.add(paper)
    db.flush()
    for i, text in enumerate(texts, start=1):
        db.add(models.Question(paper_id=paper.id, question_number=i, text=text, option_a=options[0], option_b=options[1],
                               option_c=options[2], option_d=options[3], correct_answer="ABCD"[i % 4], status=status, source="pdf_text"))
    db.commit()
    return paper


def question(db, paper, number):
    db.rollback()
    db.expire_all()
    return db.query(models.Question).filter_by(paper_id=paper.id, question_number=number).one()


def pairs(db, *papers, status=None):
    db.rollback()
    db.expire_all()
    ids = {q.id for p in papers for q in db.query(models.Question).filter_by(paper_id=p.id)}
    rows = [r for r in db.query(models.QuestionDuplicate) if r.question_id in ids or r.other_id in ids]
    return [r for r in rows if status is None or r.status == status]


def scan(db, paper=None, **kw):
    counts = duplicates.scan(db, paper.id if paper else None, **kw)
    db.commit()
    return counts


# --------------------------------------------------------------------------- matching

def test_the_hash_ignores_case_punctuation_spacing_and_option_order(db):
    a = models.Question(text="Who wrote  'Discovery of India'?", option_a="Gandhi", option_b="Nehru", option_c="Patel", option_d="Ambedkar")
    b = models.Question(text="who wrote Discovery of India", option_a="Ambedkar.", option_b="patel", option_c="NEHRU", option_d="gandhi!")
    c = models.Question(text="Who wrote Glimpses of World History?", option_a="Gandhi", option_b="Nehru", option_c="Patel", option_d="Ambedkar")
    assert duplicates.norm_hash(a) == duplicates.norm_hash(b) != duplicates.norm_hash(c)
    assert duplicates.normalised(a) == duplicates.normalised(b)


def test_an_identical_question_in_another_paper_is_found(db):
    text = sentence()
    one, two = paper_of(db, [text]), paper_of(db, [text.upper() + "!!", ])
    assert scan(db, two) == {"exact": 1, "near": 0, "cleared": 0}
    (row,) = pairs(db, one, two)
    older, newer = question(db, one, 1), question(db, two, 1)
    assert (row.other_id, row.question_id, row.kind, row.score, row.status) == (older.id, newer.id, "exact", 100, "open")
    assert older.norm_hash == newer.norm_hash                                            # the stored hash is kept in step


def test_a_reworded_copy_is_a_near_duplicate(db):
    text = sentence()
    one, two = paper_of(db, [text]), paper_of(db, [tweak(text)])
    assert scan(db, two) == {"exact": 0, "near": 1, "cleared": 0}
    (row,) = pairs(db, one, two)
    assert row.kind == "near" and duplicates.NEAR_SCORE <= row.score < 100


def test_different_questions_are_not_matched(db):
    one, two = paper_of(db, [sentence(), sentence()]), paper_of(db, [sentence(), sentence()])
    assert scan(db, two) == {"exact": 0, "near": 0, "cleared": 0}
    assert pairs(db, one, two) == []


def test_same_stem_with_different_options_is_not_a_near_duplicate(db):
    stem = sentence()
    one = paper_of(db, [stem], options=("Delhi", "Mumbai", "Chennai", "Kolkata"))
    two = paper_of(db, [stem], options=("Gandhi", "Nehru", "Patel", "Bose"))
    assert scan(db, two) == {"exact": 0, "near": 0, "cleared": 0}


def test_short_questions_are_only_compared_exactly(db):
    one, two = paper_of(db, ["What is 2+2 in maths?"]), paper_of(db, ["What is 3+2 in maths?"])
    assert scan(db, two) == {"exact": 0, "near": 0, "cleared": 0}
    three = paper_of(db, ["What is 2+2 in maths?"])
    assert scan(db, three)["exact"] >= 1


def test_duplicates_inside_one_paper_are_found_and_three_copies_make_three_pairs(db):
    text = sentence()
    paper = paper_of(db, [text, text, text, sentence()])
    assert scan(db, paper)["exact"] == 3
    assert len(pairs(db, paper)) == 3


def test_archived_papers_and_quarantined_questions_are_not_compared(db):
    text = sentence()
    old, new = paper_of(db, [text]), paper_of(db, [text])
    old.archived_at = models.datetime.utcnow()
    db.commit()
    assert scan(db, new) == {"exact": 0, "near": 0, "cleared": 0}
    old.archived_at = None
    question(db, new, 1).status = QStatus.QUARANTINED
    db.commit()
    assert scan(db, new) == {"exact": 0, "near": 0, "cleared": 0}


def test_scanning_again_adds_nothing_and_never_reopens_a_decision(db):
    text = sentence()
    one, two = paper_of(db, [text]), paper_of(db, [text])
    scan(db, two)
    assert scan(db, two) == {"exact": 0, "near": 0, "cleared": 0}
    (row,) = pairs(db, one, two)
    duplicates.keep_both(db, None, row)
    db.commit()
    assert scan(db, two) == {"exact": 0, "near": 0, "cleared": 0}
    assert [r.status for r in pairs(db, one, two)] == ["kept_both"]


def test_an_open_pair_that_no_longer_matches_is_removed(db):
    text = sentence()
    one, two = paper_of(db, [text]), paper_of(db, [text])
    scan(db, two)
    question(db, two, 1).text = sentence()
    db.commit()
    assert scan(db, two) == {"exact": 0, "near": 0, "cleared": 1}
    assert pairs(db, one, two) == []


# --------------------------------------------------------------------------- decisions

def test_keep_both_is_remembered_and_logged(admin, db):
    text = sentence()
    one, two = paper_of(db, [text]), paper_of(db, [text])
    scan(db, two)
    (row,) = pairs(db, one, two)
    assert admin.post(f"/admin/duplicates/{row.id}/keep-both").status_code == 303
    (row,) = pairs(db, one, two)
    assert row.status == "kept_both" and row.decided_at is not None
    assert question(db, one, 1).status == QStatus.NEEDS_REVIEW and question(db, two, 1).status == QStatus.NEEDS_REVIEW
    assert db.query(models.AuditLog).filter_by(action="duplicate.keep_both", entity_id=row.question_id).count() == 1
    assert admin.post(f"/admin/duplicates/{row.id}/keep-both").status_code == 303                # harmless the second time


def test_merging_keeps_one_quarantines_the_other_and_remembers_where_it_came_from(admin, db):
    text = sentence()
    one, two = paper_of(db, [text], year=2019), paper_of(db, [text], year=2021)
    scan(db, two)
    (row,) = pairs(db, one, two)
    older, newer = question(db, one, 1), question(db, two, 1)
    assert admin.post(f"/admin/duplicates/{row.id}/merge", data={"keep": str(older.id)}).status_code == 303
    older, newer = question(db, one, 1), question(db, two, 1)
    assert older.status == QStatus.NEEDS_REVIEW and older.text == text                            # the kept one is untouched
    assert newer.status == QStatus.QUARANTINED and "Merged into a duplicate" in newer.quarantine_reason and one.title in newer.quarantine_reason
    (source,) = db.query(models.QuestionSource).filter_by(question_id=older.id).all()
    assert source.from_question_id == newer.id and source.paper_id == two.id and source.question_number == 1
    assert source.label == f"{two.title} · 2021 · Q1"
    (row,) = pairs(db, one, two)
    assert row.status == "merged" and row.merged_into == older.id
    log = db.query(models.AuditLog).filter_by(action="duplicate.merge", entity_id=older.id).one()
    assert two.title in log.detail_json and '"answers_differed"' in log.detail_json
    assert f"Also appeared in: {two.title} · 2021 · Q1" in admin.get(f"/review/{one.id}").text


def test_merging_the_other_way_keeps_the_newer_one(admin, db):
    text = sentence()
    one, two = paper_of(db, [text]), paper_of(db, [text])
    scan(db, two)
    (row,) = pairs(db, one, two)
    admin.post(f"/admin/duplicates/{row.id}/merge", data={"keep": str(question(db, two, 1).id)})
    assert question(db, one, 1).status == QStatus.QUARANTINED and question(db, two, 1).status == QStatus.NEEDS_REVIEW
    assert db.query(models.QuestionSource).filter_by(question_id=question(db, two, 1).id).one().paper_id == one.id


def test_a_merged_away_live_question_leaves_the_students_pool(admin, db):
    text = sentence()
    one, two = paper_of(db, [text], status=QStatus.LIVE), paper_of(db, [text], status=QStatus.LIVE)
    for p in (one, two):
        p.publish_status = "published"
    db.commit()
    scan(db, two)
    (row,) = pairs(db, one, two)
    admin.post(f"/admin/duplicates/{row.id}/merge", data={"keep": str(question(db, one, 1).id)})
    live_ids = {q.id for q in pool.live_questions(db)}
    assert question(db, one, 1).id in live_ids and question(db, two, 1).id not in live_ids


def test_sources_follow_a_question_that_is_itself_merged_away(admin, db):
    text = sentence()
    a, b, c = paper_of(db, [text]), paper_of(db, [text]), paper_of(db, [text])
    scan(db, a)
    scan(db, b)
    ab = next(r for r in pairs(db, a, b, c) if {r.question_id, r.other_id} == {question(db, a, 1).id, question(db, b, 1).id})
    admin.post(f"/admin/duplicates/{ab.id}/merge", data={"keep": str(question(db, a, 1).id)})            # b folded into a
    bc = [r for r in pairs(db, a, b, c, status="open")]
    assert all(question(db, b, 1).id not in (r.question_id, r.other_id) for r in bc)                       # b's other pairs went with it
    ac = next(r for r in bc if {r.question_id, r.other_id} == {question(db, a, 1).id, question(db, c, 1).id})
    admin.post(f"/admin/duplicates/{ac.id}/merge", data={"keep": str(question(db, c, 1).id)})            # now a folded into c
    labels = sorted(s.label for s in db.query(models.QuestionSource).filter_by(question_id=question(db, c, 1).id))
    assert len(labels) == 2 and any(a.title in l for l in labels) and any(b.title in l for l in labels)


def test_a_decided_pair_cannot_be_decided_again_and_the_kept_id_must_be_in_the_pair(admin, db):
    text = sentence()
    one, two, other = paper_of(db, [text]), paper_of(db, [text]), paper_of(db, [sentence()])
    scan(db, two)
    (row,) = pairs(db, one, two)
    admin.post(f"/admin/duplicates/{row.id}/merge", data={"keep": str(question(db, other, 1).id)})
    assert "Choose one of the two" in admin.get("/admin/duplicates").text
    assert pairs(db, one, two)[0].status == "open"
    admin.post(f"/admin/duplicates/{row.id}/merge", data={"keep": str(question(db, one, 1).id)})
    admin.get("/admin/duplicates")
    admin.post(f"/admin/duplicates/{row.id}/merge", data={"keep": str(question(db, two, 1).id)})
    assert "already been decided" in admin.get("/admin/duplicates").text
    assert question(db, two, 1).status == QStatus.QUARANTINED and question(db, one, 1).status != QStatus.QUARANTINED
    assert admin.post("/admin/duplicates/99999999/merge", data={"keep": "1"}).status_code == 404


def test_restoring_a_merged_question_reopens_the_pair_and_drops_its_source(admin, db):
    text = sentence()
    one, two = paper_of(db, [text]), paper_of(db, [text])
    scan(db, two)
    (row,) = pairs(db, one, two)
    admin.post(f"/admin/duplicates/{row.id}/merge", data={"keep": str(question(db, one, 1).id)})
    dropped = question(db, two, 1)
    assert admin.post(f"/quarantine/{dropped.id}/restore").status_code == 303
    assert question(db, two, 1).status == QStatus.NEEDS_REVIEW
    assert db.query(models.QuestionSource).filter_by(question_id=question(db, one, 1).id).count() == 0
    (row,) = pairs(db, one, two)
    assert row.status == "open" and row.merged_into is None


# --------------------------------------------------------------------------- when scans happen

def test_editing_a_questions_wording_rescans_it(admin, db):
    text = sentence()
    one, two = paper_of(db, [text]), paper_of(db, [sentence()])
    assert pairs(db, one, two) == []
    q = question(db, two, 1)
    admin.post(f"/review/{two.id}/question/{q.id}", data=question_form(q, text=text))
    assert [r.kind for r in pairs(db, one, two)] == ["exact"]
    q = question(db, two, 1)
    admin.post(f"/review/{two.id}/question/{q.id}", data=question_form(q, text=sentence()))
    assert pairs(db, one, two) == []                                                       # edited away: the open pair goes


def test_filing_a_subject_does_not_trigger_a_scan_or_change_pairs(admin, db):
    text = sentence()
    one, two = paper_of(db, [text]), paper_of(db, [text])
    scan(db, two)
    q = question(db, two, 1)
    subject = db.query(models.Subject).first()
    admin.post(f"/review/{two.id}/question/{q.id}", data=question_form(q, subject_id=str(subject.id)))
    assert [r.status for r in pairs(db, one, two)] == ["open"]


def test_a_paper_read_from_a_pdf_is_scanned_for_duplicates(db, tmp_path):
    from test_text_extract import paper_pdf

    def read(count):
        path = tmp_path / f"dup{next(_run)}.pdf"
        path.write_bytes(paper_pdf(count=count))
        paper = models.Paper(title=f"Dup pdf {next(_run)}", exam_type=models.ExamType.full_length, status="processing", source_pdf_path=str(path))
        db.add(paper)
        db.commit()
        ingest.process_paper(paper.id)
        db.rollback()
        return db.get(models.Paper, paper.id)

    first, second = read(4), read(4)              # the generated papers print the very same questions
    found = pairs(db, second, status="open")
    assert len([r for r in found if r.kind == "exact"]) >= 4
    assert any(question(db, first, 1).id in (r.other_id, r.question_id) for r in found)


def test_json_import_lists_duplicates_and_can_skip_them(admin, db):
    unique = sentence()
    known = doc([json_q(1, question=unique, options={"a": "One", "b": "Two", "c": "Three", "d": "Four"}),
                 json_q(2, question=sentence(), options={"a": "One", "b": "Two", "c": "Three", "d": "Four"})])
    json_apply(admin, token_of(validate(admin, ("a.json", known))), title=f"JSON known {next(_run)}")
    fresh_title = f"JSON copy {next(_run)}"
    copy = doc([json_q(1, question=unique.lower(), options={"a": "four", "b": "three", "c": "two", "d": "one"}),      # same, shuffled options
                json_q(2, question=sentence(), options={"a": "One", "b": "Two", "c": "Three", "d": "Four"})])
    report = validate(admin, ("b.json", copy))
    assert "Skip the 1 question that duplicate" in report.text.replace("questions", "question")
    token = token_of(report)
    assert json_apply(admin, token, title=fresh_title, skip_duplicates="true").status_code == 303
    db.rollback()
    paper = db.query(models.Paper).filter_by(title=fresh_title).one()
    assert [q.question_number for q in db.query(models.Question).filter_by(paper_id=paper.id)] == [2]
    assert '"skipped_duplicates": 1' in db.query(models.AuditLog).filter_by(paper_id=paper.id, action="paper.json_import").one().detail_json


def test_json_import_without_skipping_keeps_them_and_lists_them_for_review(admin, db):
    unique = sentence()
    first = doc([json_q(1, question=unique, options={"a": "One", "b": "Two", "c": "Three", "d": "Four"})])
    json_apply(admin, token_of(validate(admin, ("a.json", first))), title=f"JSON first {next(_run)}")
    title = f"JSON second {next(_run)}"
    r = json_apply(admin, token_of(validate(admin, ("b.json", first))), title=title, allow_duplicate="true")
    assert r.status_code == 303
    db.rollback()
    paper = db.query(models.Paper).filter_by(title=title).one()
    assert len(pairs(db, paper, status="open")) >= 1
    assert "possible duplicate" in admin.get(f"/review/{paper.id}").text


def test_skipping_everything_is_refused(admin, db):
    unique = sentence()
    only = doc([json_q(1, question=unique, options={"a": "One", "b": "Two", "c": "Three", "d": "Four"})])
    json_apply(admin, token_of(validate(admin, ("a.json", only))), title=f"JSON only {next(_run)}")
    r = json_apply(admin, token_of(validate(admin, ("b.json", only))), title=f"JSON again {next(_run)}", allow_duplicate="true", skip_duplicates="true")
    assert r.status_code == 400 and "nothing left to import" in r.text


# --------------------------------------------------------------------------- the pages

def test_the_duplicates_page_shows_both_sides_and_warns_when_answers_differ(admin, db):
    text = sentence()
    one, two = paper_of(db, [text], year=2018), paper_of(db, [text], year=2020)
    a, b = question(db, one, 1), question(db, two, 1)
    a.correct_answer, b.correct_answer = "A", "C"
    db.commit()
    scan(db, two)
    page = admin.get(f"/admin/duplicates?paper_id={two.id}").text
    assert one.title in page and two.title in page and "Identical" in page
    assert "disagree on the answer (older: A: 1 only — newer: C: Both 1 and 2)" in page
    assert "Keep both" in page and "Keep older, merge newer" in page and "Keep newer, merge older" in page
    assert "Marked correct" in page


def test_a_pair_with_matching_answers_has_no_warning_and_a_near_one_shows_its_score(admin, db):
    text = sentence()
    one, two = paper_of(db, [text]), paper_of(db, [tweak(text)])
    for p in (one, two):
        question(db, p, 1).correct_answer = "B"
    db.commit()
    scan(db, two)
    page = admin.get(f"/admin/duplicates?paper_id={two.id}").text
    assert "disagree on the answer" not in page and "Very similar" in page and "%" in page


def test_the_decided_tab_and_the_paper_filter(admin, db):
    text = sentence()
    one, two, unrelated = paper_of(db, [text]), paper_of(db, [text]), paper_of(db, [sentence()])
    scan(db, two)
    (row,) = pairs(db, one, two)
    admin.post(f"/admin/duplicates/{row.id}/keep-both")
    assert "Kept both" in admin.get(f"/admin/duplicates?show=decided&paper_id={two.id}").text
    assert one.title not in admin.get(f"/admin/duplicates?show=decided&paper_id={unrelated.id}").text
    assert one.title not in admin.get(f"/admin/duplicates?paper_id={two.id}").text                        # it is decided, so not open


def test_the_review_page_flags_the_paper_and_the_question(admin, db):
    text = sentence()
    one, two = paper_of(db, [text, sentence()]), paper_of(db, [text])
    scan(db, two)
    page = admin.get(f"/review/{two.id}").text
    assert "may duplicate another" in page and "Possible duplicate" in page
    assert f'/admin/duplicates?paper_id={two.id}' in page
    clean = paper_of(db, [sentence()])
    assert "may duplicate another" not in admin.get(f"/review/{clean.id}").text


def test_look_again_scans_one_paper_or_the_whole_bank(admin, db):
    text = sentence()
    one, two = paper_of(db, [text]), paper_of(db, [text])
    assert pairs(db, one, two) == []                                        # created directly: nobody has scanned yet
    assert admin.post("/admin/duplicates/scan", data={"paper_id": str(two.id)}).status_code == 303
    assert len(pairs(db, one, two)) == 1
    three = paper_of(db, [tweak(text)])
    assert admin.post("/admin/duplicates/scan", data={"paper_id": ""}).status_code == 303
    assert len(pairs(db, three)) >= 1
    assert admin.post("/admin/duplicates/scan", data={"paper_id": "99999999"}).status_code == 404
    assert db.query(models.AuditLog).filter_by(action="duplicates.scan").count() >= 2


def test_every_duplicate_route_is_admin_only(admin, db, anon):
    text = sentence()
    one, two = paper_of(db, [text]), paper_of(db, [text])
    scan(db, two)
    (row,) = pairs(db, one, two)
    student = make_student_client(db, "dupstudent")
    calls = [("get", "/admin/duplicates", {}), ("post", "/admin/duplicates/scan", {}),
             ("post", f"/admin/duplicates/{row.id}/keep-both", {}),
             ("post", f"/admin/duplicates/{row.id}/merge", {"keep": str(question(db, one, 1).id)})]
    for client, expected in ((student, 403), (anon, 303)):
        for method, url, data in calls:
            assert getattr(client, method)(url, **({"data": data} if method == "post" else {})).status_code == expected, url
    assert pairs(db, one, two)[0].status == "open"


def test_the_admin_menu_has_a_duplicates_link_and_students_do_not(admin, db):
    assert 'href="/admin/duplicates"' in admin.get("/admin").text
    student = make_student_client(db, "dupnavstudent")
    assert "/admin/duplicates" not in student.get("/").text
