"""End-to-end JSON import for the real MakeIAS RTM-70 two-blob paste (Q26–50 then Q1–25)."""
from pathlib import Path

from app import json_import as ji, models
from test_json_import import apply, paper_by_title, questions_by_number, token_of, validate

PASTE = (Path(__file__).parent / "fixtures" / "rtm70_concat.json").read_text(encoding="utf-8")
DECODER = __import__("json").JSONDecoder()


def _blobs():
    i, out = 0, []
    while i < len(PASTE):
        while i < len(PASTE) and PASTE[i].isspace():
            i += 1
        if i >= len(PASTE):
            break
        obj, end = DECODER.raw_decode(PASTE, i)
        out.append(obj)
        i = end
    return out


def _dump(obj):
    return __import__("json").dumps(obj, ensure_ascii=False)


def test_concatenated_paste_creates_fifty_questions_as_a_new_paper(admin, db):
    report = ji.build_report([("Pasted text", PASTE)], models.STANDARD_SUBJECTS)
    assert not report.errors, [i.message for i in report.errors]
    assert set(report.questions) == set(range(1, 51))

    title = "RTM70 concat new paper"
    r = validate(admin, pasted=PASTE)
    assert r.status_code == 200
    assert "50" in r.text
    done = apply(admin, token_of(r), title=title, year="2026", source_name="MakeIAS", series="C",
                 expected_total="75", skip_invalid="true")
    assert done.status_code == 303, done.text[:800]
    paper = paper_by_title(db, title)
    qs = questions_by_number(db, paper)
    assert set(qs) == set(range(1, 51))
    assert qs[26].text.startswith("‘Kala-azar’")
    assert qs[1].text.startswith("Which specific enzyme")


def test_add_second_batch_onto_first_twenty_five_without_append(admin, db):
    """Import Q1–25, then paste Q26–50 onto the same paper. New numbers should be created."""
    first, second = _blobs()
    q1_25 = first if first["questions"][0]["number"] == 1 else second
    q26_50 = second if q1_25 is first else first
    title = "RTM70 add 26-50 onto 1-25"
    r = validate(admin, pasted=_dump(q1_25))
    assert apply(admin, token_of(r), title=title, year="2026", source_name="MakeIAS", series="C",
                 expected_total="75", skip_invalid="true").status_code == 303
    paper = paper_by_title(db, title)
    assert set(questions_by_number(db, paper)) == set(range(1, 26))

    r = validate(admin, pasted=_dump(q26_50), target=str(paper.id))
    assert r.status_code == 200
    assert apply(admin, token_of(r), skip_invalid="true").status_code == 303
    qs = questions_by_number(db, paper)
    assert set(qs) == set(range(1, 51)), sorted(qs)


def test_add_restarted_1_to_25_needs_append_checkbox(admin, db):
    """Second paste numbered 1–25 again is skipped unless append_new is ticked."""
    first, second = _blobs()
    q1_25 = first if first["questions"][0]["number"] == 1 else second
    title = "RTM70 restart numbers"
    r = validate(admin, pasted=_dump(q1_25))
    first = apply(admin, token_of(r), title=title, year="2026", skip_invalid="true", allow_duplicate="true")
    assert first.status_code == 303, first.text[:800]
    paper = paper_by_title(db, title)

    r = validate(admin, pasted=_dump(q1_25), target=str(paper.id))
    assert r.status_code == 200
    assert "already on this paper" in r.text
    assert apply(admin, token_of(r), skip_invalid="true").status_code == 303
    assert set(questions_by_number(db, paper)) == set(range(1, 26))

    r = validate(admin, pasted=_dump(q1_25), target=str(paper.id))
    assert apply(admin, token_of(r), append_new="true", skip_invalid="true").status_code == 303
    qs = questions_by_number(db, paper)
    assert set(qs) == set(range(1, 51)), sorted(qs)
    assert qs[26].text.startswith("Which specific enzyme")


def test_paste_order_26_50_then_add_1_25(admin, db):
    """The user's paste order: first object is Q26–50, then add Q1–25 onto that paper."""
    first, second = _blobs()
    q26_50 = first if first["questions"][0]["number"] == 26 else second
    q1_25 = second if q26_50 is first else first
    title = "RTM70 26-50 then 1-25"
    r = validate(admin, pasted=_dump(q26_50))
    assert apply(admin, token_of(r), title=title, year="2026", source_name="MakeIAS", series="C",
                 expected_total="75", skip_invalid="true", allow_duplicate="true").status_code == 303
    paper = paper_by_title(db, title)
    assert set(questions_by_number(db, paper)) == set(range(26, 51))

    r = validate(admin, pasted=_dump(q1_25), target=str(paper.id))
    assert apply(admin, token_of(r), skip_invalid="true", allow_duplicate="true").status_code == 303
    assert set(questions_by_number(db, paper)) == set(range(1, 51))


def test_add_concatenated_paste_onto_paper_with_1_25(admin, db):
    """Paper already has Q1–25; the two-blob paste should add Q26–50 (Q1–25 already exist)."""
    first, second = _blobs()
    q1_25 = first if first["questions"][0]["number"] == 1 else second
    title = "RTM70 concat onto existing 1-25"
    r = validate(admin, pasted=_dump(q1_25))
    assert apply(admin, token_of(r), title=title, year="2026", skip_invalid="true", allow_duplicate="true").status_code == 303
    paper = paper_by_title(db, title)

    r = validate(admin, pasted=PASTE, target=str(paper.id))
    assert r.status_code == 200
    assert apply(admin, token_of(r), skip_invalid="true").status_code == 303
    qs = questions_by_number(db, paper)
    assert set(qs) == set(range(1, 51)), sorted(qs)
