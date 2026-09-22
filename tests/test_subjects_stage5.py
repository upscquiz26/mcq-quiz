"""Import stage 5: subjects — bulk assign, range templates saved per series, keyword suggestions kept apart from the real subject."""
import itertools

import pytest

from app import ingest, models, sample_audit, subject_hints, subject_templates
from app.models import QStatus
from conftest import blank_pdf_bytes, make_student_client, question_form

_run = itertools.count(1)

POLITY = "Which Article of the Constitution deals with the powers of the President to promulgate an ordinance?"
HISTORY = "The Mughal emperor Akbar introduced which revenue system during his dynasty?"
GEOGRAPHY = "Which river of the Himalaya forms a large delta before joining the ocean?"
AMBIGUOUS = "Which of these is a river, a mountain, a constitution or an article?"
NOTHING = "Which of the following is the correct sequence of events described above?"


def subject(db, name):
    return db.query(models.Subject).filter_by(name=name).one()


def paper_with(db, make_paper, texts, status=QStatus.NEEDS_REVIEW, **fields):
    paper = make_paper(title=f"Subjects paper {next(_run)}", n=len(texts), **fields)
    for q in db.query(models.Question).filter_by(paper_id=paper.id):
        q.text = texts[q.question_number - 1]
        q.status = status
    db.commit()
    return paper


def q_of(db, paper, number):
    db.rollback()
    db.expire_all()
    return db.query(models.Question).filter_by(paper_id=paper.id, question_number=number).one()


def ids_of(db, paper, *numbers):
    return [q_of(db, paper, n).id for n in numbers]


# --------------------------------------------------------------------------- keyword suggestions

@pytest.mark.parametrize("text,expected", [
    (POLITY, "Polity"), (HISTORY, "History"), (GEOGRAPHY, "Geography"),
    ("The RBI raised the repo rate to control inflation. Which of these is affected?", "Economy"),
    ("Under the Ramsar convention a wetland of importance protects which species?", "Environment"),
    ("ISRO launched a satellite; which orbit is used?", "Science & Tech"),
    ("The ratio of ages is 3:2. What is the average speed of the train?", "CSAT"),
])
def test_a_clear_question_gets_the_matching_subject(text, expected):
    found = subject_hints.suggest(text)
    assert found and found[0] == expected


@pytest.mark.parametrize("text", [NOTHING, AMBIGUOUS, "", "Consider the following statements:", "The word river alone is one weak hint."])
def test_an_unclear_or_tied_question_gets_no_suggestion(text):
    assert subject_hints.suggest(text) is None


def test_keywords_match_whole_words_only():
    assert subject_hints.scores("cellular phones and articulate speakers")["Polity"] == 0        # 'article' inside 'articulate'
    assert subject_hints.scores("The Lok Sabha")["Polity"] == 2                                  # a phrase is worth two


def test_suggestions_are_stored_apart_from_the_subject_and_never_applied(db, make_paper):
    paper = paper_with(db, make_paper, [POLITY, HISTORY, NOTHING, GEOGRAPHY])
    assert subject_hints.suggest_for_paper(db, paper.id) == 3
    db.commit()
    by = {n: q_of(db, paper, n) for n in (1, 2, 3, 4)}
    assert by[1].suggested_subject_id == subject(db, "Polity").id
    assert by[2].suggested_subject_id == subject(db, "History").id
    assert by[3].suggested_subject_id is None
    assert all(q.subject_id is None for q in by.values())                     # a hint is never a decision


def test_a_question_that_has_a_subject_is_left_alone_and_loses_its_hint(db, make_paper):
    paper = paper_with(db, make_paper, [POLITY, HISTORY])
    subject_hints.suggest_for_paper(db, paper.id)
    q1 = q_of(db, paper, 1)
    q1.subject_id = subject(db, "Economy").id
    db.commit()
    subject_hints.suggest_for_paper(db, paper.id)
    db.commit()
    q1 = q_of(db, paper, 1)
    assert q1.subject_id == subject(db, "Economy").id and q1.suggested_subject_id is None
    assert q_of(db, paper, 2).suggested_subject_id == subject(db, "History").id


def test_a_stored_suggestion_is_kept_unless_asked_to_redo(db, make_paper):
    paper = paper_with(db, make_paper, [POLITY])
    q = q_of(db, paper, 1)
    q.suggested_subject_id = subject(db, "Geography").id
    db.commit()
    subject_hints.suggest_for_paper(db, paper.id)
    db.commit()
    assert q_of(db, paper, 1).suggested_subject_id == subject(db, "Geography").id
    subject_hints.suggest_for_paper(db, paper.id, redo=True)
    db.commit()
    assert q_of(db, paper, 1).suggested_subject_id == subject(db, "Polity").id


def test_quarantined_questions_get_no_suggestion(db, make_paper):
    paper = paper_with(db, make_paper, [POLITY], status=QStatus.QUARANTINED)
    assert subject_hints.suggest_for_paper(db, paper.id) == 0


def test_the_review_page_shows_the_suggestion_but_does_not_select_it(admin, db, make_paper):
    paper = paper_with(db, make_paper, [POLITY, NOTHING])
    admin.post(f"/review/{paper.id}/suggest-subjects")
    page = admin.get(f"/review/{paper.id}").text
    assert "Suggested subject:" in page and "Use Polity" in page
    assert page.count("Suggested subject:") == 1
    first_card = page.split('id="q1"')[1].split('id="q2"')[0]
    assert 'value="' + str(subject(db, "Polity").id) + '" selected' not in first_card             # the dropdown is not pre-selected


def test_accepting_one_suggestion_files_the_question_and_keeps_history(admin, db, make_paper):
    paper = paper_with(db, make_paper, [POLITY, HISTORY])
    subject_hints.suggest_for_paper(db, paper.id)
    db.commit()
    a, b = ids_of(db, paper, 1, 2)
    assert admin.post(f"/review/{paper.id}/accept-suggestions", data={"question_ids": [a]}).status_code == 303
    q1, q2 = q_of(db, paper, 1), q_of(db, paper, 2)
    assert q1.subject_id == subject(db, "Polity").id and q1.suggested_subject_id is None
    assert q2.subject_id is None and q2.suggested_subject_id == subject(db, "History").id          # the other is untouched
    assert [v.reason for v in db.query(models.QuestionVersion).filter_by(question_id=a)] == ["subject suggestion accepted"]


def test_accept_all_suggestions_and_the_audit_log(admin, db, make_paper):
    paper = paper_with(db, make_paper, [POLITY, HISTORY, NOTHING, GEOGRAPHY])
    admin.post(f"/review/{paper.id}/suggest-subjects")
    admin.post(f"/review/{paper.id}/accept-suggestions")
    got = {n: q_of(db, paper, n).subject_id for n in (1, 2, 3, 4)}
    assert got == {1: subject(db, "Polity").id, 2: subject(db, "History").id, 3: None, 4: subject(db, "Geography").id}
    actions = [a.action for a in db.query(models.AuditLog).filter_by(paper_id=paper.id)]
    assert "subjects.suggest" in actions and "subjects.accept_suggestions" in actions
    assert "There were no suggestions" in _flash_after(admin, paper, "accept-suggestions")


def _flash_after(admin, paper, action):
    admin.post(f"/review/{paper.id}/{action}")
    return admin.get(f"/review/{paper.id}").text


def test_a_paper_read_from_a_pdf_gets_suggestions_without_any_click(db, tmp_path):
    from pdfmaker import flow, make_pdf
    lines = []
    for n, stem in enumerate([POLITY, HISTORY, NOTHING], start=1):
        lines += [f"{n}. {stem}", "(a) 1 only (b) 2 only", "(c) Both 1 and 2 (d) Neither 1 nor 2"]
    items, _ = flow(lines, 60, 60)
    path = tmp_path / f"hint{next(_run)}.pdf"
    path.write_bytes(make_pdf([items]))
    paper = models.Paper(title=f"Hint paper {next(_run)}", exam_type=models.ExamType.full_length, status="processing", source_pdf_path=str(path))
    db.add(paper)
    db.commit()
    ingest.process_paper(paper.id)
    got = {n: q_of(db, paper, n) for n in (1, 2, 3)}
    assert got[1].suggested_subject_id == subject(db, "Polity").id and got[2].suggested_subject_id == subject(db, "History").id
    assert got[3].suggested_subject_id is None and all(q.subject_id is None for q in got.values())

# --------------------------------------------------------------------------- bulk assign

def test_bulk_assign_gives_the_ticked_questions_a_subject_and_nothing_else(admin, db, make_paper):
    paper = paper_with(db, make_paper, [NOTHING] * 6)
    ticked = ids_of(db, paper, 2, 3, 5)
    r = admin.post(f"/review/{paper.id}/bulk-subject", data={"question_ids": ticked, "subject_id": str(subject(db, "History").id)})
    assert r.status_code == 303
    got = {n: q_of(db, paper, n).subject_id for n in range(1, 7)}
    history = subject(db, "History").id
    assert got == {1: None, 2: history, 3: history, 4: None, 5: history, 6: None}
    assert [v.reason for v in db.query(models.QuestionVersion).filter_by(question_id=ticked[0])] == ["bulk subject change"]
    log = db.query(models.AuditLog).filter_by(paper_id=paper.id, action="subjects.bulk_assign").one()
    assert '"changed": 3' in log.detail_json and '"selected": 3' in log.detail_json


def test_bulk_assign_ignores_other_papers_and_quarantined_questions(admin, db, make_paper):
    paper = paper_with(db, make_paper, [NOTHING] * 3)
    other = paper_with(db, make_paper, [NOTHING] * 2)
    quarantined = q_of(db, paper, 3)
    quarantined.status = QStatus.QUARANTINED
    db.commit()
    foreign = ids_of(db, other, 1)
    admin.post(f"/review/{paper.id}/bulk-subject", data={"question_ids": foreign + [quarantined.id] + ids_of(db, paper, 1),
                                                         "subject_id": str(subject(db, "Polity").id)})
    assert q_of(db, other, 1).subject_id is None and q_of(db, paper, 3).subject_id is None
    assert q_of(db, paper, 1).subject_id == subject(db, "Polity").id


def test_bulk_assign_needs_a_selection_and_a_subject(admin, db, make_paper):
    paper = paper_with(db, make_paper, [NOTHING] * 2)
    admin.post(f"/review/{paper.id}/bulk-subject", data={"subject_id": str(subject(db, "Polity").id)})
    assert "Tick at least one question" in admin.get(f"/review/{paper.id}").text
    admin.post(f"/review/{paper.id}/bulk-subject", data={"question_ids": ids_of(db, paper, 1), "subject_id": ""})
    assert "Choose a subject" in admin.get(f"/review/{paper.id}").text
    admin.post(f"/review/{paper.id}/bulk-subject", data={"question_ids": ids_of(db, paper, 1), "subject_id": "99999"})
    assert q_of(db, paper, 1).subject_id is None


def test_a_topic_that_belongs_to_another_subject_is_cleared_on_reassignment(admin, db, make_paper):
    paper = paper_with(db, make_paper, [NOTHING])
    topic = models.Topic(name=f"Mughals {next(_run)}", subject_id=subject(db, "History").id)
    db.add(topic)
    db.commit()
    q = q_of(db, paper, 1)
    q.subject_id, q.topic_id = subject(db, "History").id, topic.id
    db.commit()
    admin.post(f"/review/{paper.id}/bulk-subject", data={"question_ids": [q.id], "subject_id": str(subject(db, "History").id)})
    assert q_of(db, paper, 1).topic_id == topic.id                                   # same subject: topic stays
    admin.post(f"/review/{paper.id}/bulk-subject", data={"question_ids": [q.id], "subject_id": str(subject(db, "Geography").id)})
    q = q_of(db, paper, 1)
    assert q.subject_id == subject(db, "Geography").id and q.topic_id is None


def test_the_no_subject_filter_and_the_bulk_bar(admin, db, make_paper):
    paper = paper_with(db, make_paper, [NOTHING] * 4)
    q = q_of(db, paper, 2)
    q.subject_id = subject(db, "Polity").id
    db.commit()
    page = admin.get(f"/review/{paper.id}?show=no_subject").text
    assert 'id="q1"' in page and 'id="q3"' in page and 'id="q2"' not in page
    assert "Select all 3 shown" in page and "Assign to ticked" in page and 'form="bulk-form"' in page


# --------------------------------------------------------------------------- filing is not editing (the audit)

def test_choosing_a_subject_while_confirming_does_not_exempt_a_question_from_the_audit(admin, db, make_paper):
    paper = paper_with(db, make_paper, [NOTHING] * 6)
    for q in db.query(models.Question).filter_by(paper_id=paper.id):
        form = question_form(q, subject_id=str(subject(db, "Polity").id), difficulty="easy")
        assert admin.post(f"/review/{paper.id}/question/{q.id}", data=form).status_code == 303
    db.rollback()
    reasons = {v.reason for v in db.query(models.QuestionVersion)}
    assert "subject/topic/difficulty" in reasons
    assert len(sample_audit.unedited_confirmed(db, db.get(models.Paper, paper.id))) == 6


def test_changing_the_text_does_exempt_it(admin, db, make_paper):
    paper = paper_with(db, make_paper, [NOTHING] * 6)
    q = q_of(db, paper, 1)
    admin.post(f"/review/{paper.id}/question/{q.id}", data=question_form(q, text="A corrected question?", subject_id=str(subject(db, "Polity").id)))
    for other in db.query(models.Question).filter(models.Question.paper_id == paper.id, models.Question.id != q.id):
        admin.post(f"/review/{paper.id}/question/{other.id}", data=question_form(other))
    db.rollback()
    pool = sample_audit.unedited_confirmed(db, db.get(models.Paper, paper.id))
    assert len(pool) == 5 and q.id not in [x.id for x in pool]


def test_bulk_filing_and_accepted_suggestions_do_not_exempt_either(admin, db, make_paper):
    paper = paper_with(db, make_paper, [POLITY] * 6, status=QStatus.VERIFIED)
    admin.post(f"/review/{paper.id}/suggest-subjects")
    admin.post(f"/review/{paper.id}/accept-suggestions")
    ids = ids_of(db, paper, 1, 2)
    admin.post(f"/review/{paper.id}/bulk-subject", data={"question_ids": ids, "subject_id": str(subject(db, "History").id)})
    db.rollback()
    assert len(sample_audit.unedited_confirmed(db, db.get(models.Paper, paper.id))) == 6


# --------------------------------------------------------------------------- range templates

def test_ranges_can_be_saved_as_a_template_for_a_series(admin, db, make_paper):
    paper = paper_with(db, make_paper, [NOTHING] * 4, series="A")
    name = f"GS layout {next(_run)}"
    admin.post(f"/review/{paper.id}/subjects", data={"subject_ranges": "1-2 history; 3-4 polity", "save_name": name, "save_series": "A"})
    db.rollback()
    t = db.query(models.SubjectTemplate).filter_by(name=name).one()
    assert t.series == "A" and t.ranges_text == "1-2 History, 3-4 Polity"                      # normalised
    assert q_of(db, paper, 1).subject_id == subject(db, "History").id and q_of(db, paper, 4).subject_id == subject(db, "Polity").id
    assert db.query(models.AuditLog).filter_by(paper_id=paper.id, action="subject_template.save").count() == 1
    assert "Saved as template" in admin.get(f"/review/{paper.id}").text


def test_saving_under_the_same_name_and_series_replaces_it(admin, db, make_paper):
    paper = paper_with(db, make_paper, [NOTHING] * 4)
    name = f"Same name {next(_run)}"
    admin.post(f"/review/{paper.id}/subjects", data={"subject_ranges": "1-4 History", "save_name": name, "save_series": "B"})
    admin.post(f"/review/{paper.id}/subjects", data={"subject_ranges": "1-4 Polity", "save_name": name.upper(), "save_series": "B"})
    admin.post(f"/review/{paper.id}/subjects", data={"subject_ranges": "1-4 Geography", "save_name": name, "save_series": ""})     # another scope
    db.rollback()
    rows = db.query(models.SubjectTemplate).filter(models.SubjectTemplate.name.ilike(name)).all()
    assert sorted((t.series or "", t.ranges_text) for t in rows) == [("", "1-4 Geography"), ("B", "1-4 Polity")]


def test_a_saved_template_is_applied_by_choosing_it(admin, db, make_paper):
    saved = paper_with(db, make_paper, [NOTHING] * 4, series="C")
    name = f"To reuse {next(_run)}"
    admin.post(f"/review/{saved.id}/subjects", data={"subject_ranges": "1-2 Economy, 3-4 CSAT", "save_name": name, "save_series": "C"})
    tid = db.query(models.SubjectTemplate).filter_by(name=name).one().id
    fresh = paper_with(db, make_paper, [NOTHING] * 4, series="C")
    page = admin.get(f"/review/{fresh.id}").text
    assert name in page and "1-2 Economy, 3-4 CSAT" in page
    assert admin.post(f"/review/{fresh.id}/subjects", data={"template_id": str(tid)}).status_code == 303
    assert [q_of(db, fresh, n).subject_id for n in (1, 2, 3, 4)] == [subject(db, "Economy").id] * 2 + [subject(db, "CSAT").id] * 2
    log = db.query(models.AuditLog).filter_by(paper_id=fresh.id, action="subjects.bulk_set").one()
    assert name in log.detail_json


def test_typed_ranges_win_over_a_template(admin, db, make_paper):
    paper = paper_with(db, make_paper, [NOTHING] * 2)
    name = f"Loser {next(_run)}"
    admin.post(f"/review/{paper.id}/subjects", data={"subject_ranges": "1-2 Polity", "save_name": name})
    tid = db.query(models.SubjectTemplate).filter_by(name=name).one().id
    other = paper_with(db, make_paper, [NOTHING] * 2)
    admin.post(f"/review/{other.id}/subjects", data={"subject_ranges": "1-2 History", "template_id": str(tid)})
    assert q_of(db, other, 1).subject_id == subject(db, "History").id


def test_a_template_for_another_series_is_refused_and_changes_nothing(admin, db, make_paper):
    saved = paper_with(db, make_paper, [NOTHING] * 2, series="A")
    name = f"Series A only {next(_run)}"
    admin.post(f"/review/{saved.id}/subjects", data={"subject_ranges": "1-2 Polity", "save_name": name, "save_series": "A"})
    tid = db.query(models.SubjectTemplate).filter_by(name=name).one().id
    admin.get(f"/review/{saved.id}")                                                        # reads (and clears) the "saved" message
    wrong = paper_with(db, make_paper, [NOTHING] * 2, series="B")
    assert name not in admin.get(f"/review/{wrong.id}").text                                # not even offered
    admin.post(f"/review/{wrong.id}/subjects", data={"template_id": str(tid)})
    assert "is for series A" in admin.get(f"/review/{wrong.id}").text
    assert q_of(db, wrong, 1).subject_id is None
    unset = paper_with(db, make_paper, [NOTHING] * 2)                                       # a paper with no series may use it
    assert name in admin.get(f"/review/{unset.id}").text
    admin.post(f"/review/{unset.id}/subjects", data={"template_id": str(tid)})
    assert q_of(db, unset, 1).subject_id == subject(db, "Polity").id


def test_an_any_series_template_fits_every_paper(admin, db, make_paper):
    a = paper_with(db, make_paper, [NOTHING] * 2, series="D")
    name = f"Any series {next(_run)}"
    admin.post(f"/review/{a.id}/subjects", data={"subject_ranges": "1-2 Polity", "save_name": name, "save_series": ""})
    b = paper_with(db, make_paper, [NOTHING] * 2, series="B")
    assert name in admin.get(f"/review/{b.id}").text


def test_bad_ranges_are_not_saved_and_apply_nothing(admin, db, make_paper):
    paper = paper_with(db, make_paper, [NOTHING] * 2)
    name = f"Bad {next(_run)}"
    admin.post(f"/review/{paper.id}/subjects", data={"subject_ranges": "1-2 Astrology", "save_name": name})
    assert "Unknown subject" in admin.get(f"/review/{paper.id}").text
    assert db.query(models.SubjectTemplate).filter_by(name=name).count() == 0
    assert q_of(db, paper, 1).subject_id is None
    admin.post(f"/review/{paper.id}/subjects", data={"subject_ranges": "1-2 Polity", "save_name": name, "save_series": "Z"})
    assert db.query(models.SubjectTemplate).filter_by(name=name).count() == 0 and q_of(db, paper, 1).subject_id is None
    admin.post(f"/review/{paper.id}/subjects", data={"subject_ranges": "", "template_id": ""})
    assert "Enter at least one range" in admin.get(f"/review/{paper.id}").text


def test_a_template_can_be_deleted_without_undoing_subjects(admin, db, make_paper):
    paper = paper_with(db, make_paper, [NOTHING] * 2)
    name = f"Doomed {next(_run)}"
    admin.post(f"/review/{paper.id}/subjects", data={"subject_ranges": "1-2 Polity", "save_name": name})
    tid = db.query(models.SubjectTemplate).filter_by(name=name).one().id
    assert admin.post(f"/review/{paper.id}/subject-templates/{tid}/delete").status_code == 303
    db.rollback()
    assert db.get(models.SubjectTemplate, tid) is None
    assert q_of(db, paper, 1).subject_id == subject(db, "Polity").id
    assert admin.post(f"/review/{paper.id}/subject-templates/{tid}/delete").status_code == 303        # already gone: harmless


def test_the_upload_form_offers_saved_templates_and_checks_their_series(admin, db, make_paper):
    saved = paper_with(db, make_paper, [NOTHING] * 2)
    name = f"Upload tpl {next(_run)}"
    admin.post(f"/review/{saved.id}/subjects", data={"subject_ranges": "1-2 Polity", "save_name": name, "save_series": "A"})
    tid = db.query(models.SubjectTemplate).filter_by(name=name).one().id
    assert name in admin.get("/upload").text
    form = {"title": f"Upload with template {next(_run)}", "exam_type": "full_length", "series": "B", "subject_template": str(tid)}
    r = admin.post("/upload", data=form, files={"pdf_file": ("q.pdf", blank_pdf_bytes(), "application/pdf")})
    assert r.status_code == 400 and "is for series A" in r.text


def test_ranges_to_use_prefers_typed_text_and_reports_a_missing_template(db):
    assert subject_templates.ranges_to_use(db, "1-2 History", "", None) == ("1-2 History", None)
    assert subject_templates.ranges_to_use(db, "", "", None) == ("", None)
    with pytest.raises(ValueError):
        subject_templates.ranges_to_use(db, "", "999999", None)


# --------------------------------------------------------------------------- access and existing behaviour

def test_every_subject_route_is_admin_only(admin, db, make_paper, anon):
    paper = paper_with(db, make_paper, [NOTHING] * 2)
    student = make_student_client(db, "subjectstudent")
    qid = ids_of(db, paper, 1)[0]
    calls = [
        ("/review/{p}/bulk-subject", {"question_ids": [qid], "subject_id": "1"}),
        ("/review/{p}/accept-suggestions", {}),
        ("/review/{p}/suggest-subjects", {}),
        ("/review/{p}/subjects", {"subject_ranges": "1-2 Polity", "save_name": "nope"}),
        ("/review/{p}/subject-templates/1/delete", {}),
    ]
    for client, expected in ((student, 403), (anon, 303)):
        for url, data in calls:
            assert client.post(url.format(p=paper.id), data=data).status_code == expected, url
    assert q_of(db, paper, 1).subject_id is None
    assert db.query(models.SubjectTemplate).filter_by(name="nope").count() == 0


def test_range_assignment_clears_the_hint_and_a_mismatched_topic(db, make_paper):
    paper = paper_with(db, make_paper, [POLITY])
    topic = models.Topic(name=f"Ordinance {next(_run)}", subject_id=subject(db, "Polity").id)
    db.add(topic)
    db.commit()
    q = q_of(db, paper, 1)
    q.topic_id, q.suggested_subject_id = topic.id, subject(db, "Polity").id
    db.commit()
    ingest.apply_subject_ranges(db, paper.id, [(1, 1, "History")])
    db.commit()
    q = q_of(db, paper, 1)
    assert q.subject_id == subject(db, "History").id and q.suggested_subject_id is None and q.topic_id is None
