"""
Student stage 7: the admin's read-only view of how each student is doing.

The figures come from the same functions as the student's own Progress page, so most numbers here are the ones worked
out by hand in test_analytics_stage6 (the `world` student: 3 timed tests averaging 55%, 6 of 12 answers right = 50%,
History weak).
"""
import html
import re
from datetime import timedelta

import pytest

from app import auth, models, settings
from app.models import AttemptKind, AttemptStatus
from test_analytics_stage6 import NOW, add_attempt, bank, topic, world  # noqa: F401  (world is a fixture)
from test_practice_stage2 import user_id
from test_revision_stage5 import page


@pytest.fixture(autouse=True)
def performance_view_on(db):
    """The switch is global state; every test starts and ends with it on."""
    def reset():
        db.rollback()
        settings.set_bool(db, "user_performance_enabled", True)
        db.commit()
    reset()
    yield
    reset()


def row_of(client, username):
    """The list row for one student, as plain text."""
    raw = client.get("/admin/performance").text
    for row in re.findall(r"<tr>.*?</tr>", raw, flags=re.S):
        if f"/admin/performance/{user_id_of(username)}\"" in row:
            return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", row))).strip()
    return None


_ids: dict = {}


def user_id_of(username):
    return _ids[username]


def new_student(db, name, status=models.UserStatus.approved, display_name=None, last_active=None):
    if not db.query(models.User).filter_by(username=name).first():
        db.add(models.User(username=name, password_hash=auth.hash_password("studentpass1"), is_admin=False,
                           status=status, display_name=display_name, last_active_at=last_active))
        db.commit()
    _ids[name] = user_id(db, name)
    return _ids[name]


def audit_views(db, uid):
    db.rollback()
    return db.query(models.AuditLog).filter_by(action="performance.view", entity_id=uid).count()


# --------------------------------------------------------------------------- the list

def test_the_list_shows_each_students_numbers(db, world, admin):
    student, uid, _, _ = world
    name = db.get(models.User, uid).username
    _ids[name] = uid
    row = row_of(admin, name)
    assert row is not None
    # 3 timed tests averaging (60+75+30)/3 = 55%; 6 right of 12 = 50%; History is the weak subject (3 of 8 right).
    assert " 3 " in row and "55%" in row and "50% (6 of 12)" in row
    assert "History" in row and "(subjects)" in row


def test_the_weakest_topics_are_named_when_topics_exist(db, make_paper, make_user, admin):
    make_user("perftopicstudent")
    uid = user_id(db, "perftopicstudent")
    _ids["perftopicstudent"] = uid
    weak_topic = topic(db, "Perf weak topic")
    _, q = bank(db, make_paper, "Perf topic paper", 6, subjects={i: "History" for i in range(1, 7)},
                topics={i: weak_topic for i in range(1, 7)})
    add_attempt(db, uid, AttemptKind.TOPIC, [(q[1], True, 10)] + [(q[i], False, 10) for i in range(2, 7)])
    row = row_of(admin, "perftopicstudent")
    assert "Perf weak topic" in row and "(subjects)" not in row and "17% (1 of 6)" in row


def test_a_student_with_no_activity_gets_dashes_not_zeros_or_errors(db, admin):
    new_student(db, "perfquietstudent")
    row = row_of(admin, "perfquietstudent")
    assert "never" in row and "—" in row and "NaN" not in row and "None" not in row
    assert admin.get("/admin/performance").status_code == 200


def test_only_approved_students_are_listed(db, admin):
    for name, status in (("perfpending", models.UserStatus.pending), ("perfrejected", models.UserStatus.rejected),
                         ("perfdeactivated", models.UserStatus.deactivated), ("perfapproved", models.UserStatus.approved)):
        new_student(db, name, status)
    raw = admin.get("/admin/performance").text
    assert "perfapproved" in raw
    assert not any(n in raw for n in ("perfpending", "perfrejected", "perfdeactivated"))
    admin_id = db.query(models.User).filter_by(username="admin").one().id
    assert f"/admin/performance/{admin_id}\"" not in raw                        # admins aren't students


def test_the_list_is_most_recently_active_first_and_never_active_last(db, admin):
    new_student(db, "perforder_old", last_active=NOW - timedelta(days=9))
    new_student(db, "perforder_new", last_active=NOW - timedelta(hours=1))
    new_student(db, "perforder_never")
    raw = admin.get("/admin/performance").text
    a, b, c = (raw.index(n) for n in ("perforder_new", "perforder_old", "perforder_never"))
    assert a < b < c


def test_the_list_uses_the_display_name_and_escapes_it(db, admin):
    new_student(db, "perfnamed", display_name="<b>Big</b> Name")
    raw = admin.get("/admin/performance").text
    assert "<b>Big</b>" not in raw and "&lt;b&gt;Big&lt;/b&gt; Name" in raw and "@perfnamed" in raw


# --------------------------------------------------------------------------- one student

def test_the_detail_page_shows_the_same_numbers_the_student_sees(db, world, admin):
    student, uid, paper, _ = world
    name = db.get(models.User, uid).username
    mine, theirs = page(student, "/analytics"), page(admin, f"/admin/performance/{uid}")
    for fragment in ("Timed tests taken 3", "Average test score 55%", "Accuracy 50% 6 of 12 answers right"):
        assert fragment in mine and fragment in theirs
    assert name in theirs and "read-only view" in theirs
    assert "World paper P" in theirs and "Weak areas" in theirs and "History" in theirs


def test_the_detail_page_draws_the_charts_and_offers_table_twins(db, world, admin):
    _, uid, _, _ = world
    raw = admin.get(f"/admin/performance/{uid}").text
    assert raw.count("<svg") >= 2 and raw.count("View as a table") >= 2
    assert "Score in each timed test" in raw and "Accuracy by subject" in raw


def test_the_admins_trend_chart_does_not_link_into_the_students_results(db, world, admin):
    student, uid, _, _ = world
    raw = admin.get(f"/admin/performance/{uid}").text
    assert "/result" not in raw and "/attempts/" not in raw                     # those pages belong to the student
    assert "/attempts/" in student.get("/analytics").text                       # ...whereas the student's own chart links


def test_the_session_list_shows_every_kind_of_session_with_its_status(db, world, admin):
    _, uid, paper, q = world
    add_attempt(db, uid, AttemptKind.FULL, [(q[1], True, 10), (q[2], False, 10)], score=1, max_marks=10, paper_id=paper.id,
                status=AttemptStatus.EXPIRED)
    add_attempt(db, uid, AttemptKind.FULL, [(q[3], True, 10)], paper_id=paper.id, status=AttemptStatus.IN_PROGRESS)
    text = page(admin, f"/admin/performance/{uid}").split("Tests and practice sessions")[1]
    assert "Topic practice" in text and "Full-length test" in text and "Sectional test" in text
    assert "Time ran out" in text and "In progress" in text and "Finished" in text
    assert "2 of 2" in text                                                     # the expired test: 2 answered of 2 total


def test_the_range_filter_scopes_the_numbers(db, make_paper, admin):
    uid = new_student(db, "perfrangestudent")
    _, q = bank(db, make_paper, "Range paper", 6, subjects={i: "Polity" for i in range(1, 7)})
    add_attempt(db, uid, AttemptKind.TOPIC, [(q[i], i <= 3, 10) for i in range(1, 7)], started=NOW - timedelta(days=100))
    everything = page(admin, f"/admin/performance/{uid}?range=all")
    recent = page(admin, f"/admin/performance/{uid}?range=30")
    assert "3 of 6 answers right" in everything
    assert "no answers in this period" in recent and "3 of 6" not in recent.split("Tests and practice sessions")[0]
    assert "Topic practice" in recent.split("Tests and practice sessions")[1]   # the session list is not date-scoped
    assert admin.get(f"/admin/performance/{uid}?range=bogus").status_code == 200


def test_names_from_data_cannot_inject_markup(db, make_paper, admin):
    uid = new_student(db, "perfescape", display_name="<script>alert(1)</script>")
    evil = topic(db, "<img src=x onerror=alert(2)>")
    _, q = bank(db, make_paper, "Escape perf paper", 6, subjects={i: "History" for i in range(1, 7)},
                topics={i: evil for i in range(1, 7)})
    add_attempt(db, uid, AttemptKind.TOPIC, [(q[i], False, 10) for i in range(1, 7)])
    raw = admin.get(f"/admin/performance/{uid}").text
    assert "<script>alert(1)" not in raw and "<img src=x" not in raw
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in raw


def test_unknown_admin_and_unapproved_ids_are_404(db, admin):
    admin_id = db.query(models.User).filter_by(username="admin").one().id
    pending = new_student(db, "perfpending2", models.UserStatus.pending)
    for target in (999999, admin_id, pending):
        assert admin.get(f"/admin/performance/{target}").status_code == 404


# --------------------------------------------------------------------------- read-only and private

def test_the_pages_have_no_forms_and_no_write_routes(db, world, admin):
    _, uid, _, _ = world
    attempts_before = db.query(models.Attempt).filter_by(user_id=uid).count()
    responses_before = db.query(models.Response).join(models.Attempt).filter(models.Attempt.user_id == uid).count()
    for url in ("/admin/performance", f"/admin/performance/{uid}"):
        forms = re.findall(r'<form[^>]*action="([^"]*)"', admin.get(url).text)
        assert set(forms) <= {"/logout"}, forms                                 # nothing on the page submits anything else
        for verb in ("post", "put", "patch", "delete"):
            assert getattr(admin, verb)(url).status_code == 405
    db.rollback()
    assert db.query(models.Attempt).filter_by(user_id=uid).count() == attempts_before
    assert db.query(models.Response).join(models.Attempt).filter(models.Attempt.user_id == uid).count() == responses_before


def test_no_password_or_hash_ever_appears(db, world, admin):
    _, uid, _, _ = world
    stored = db.get(models.User, uid).password_hash
    for url in ("/admin/performance", f"/admin/performance/{uid}"):
        raw = admin.get(url).text
        assert stored not in raw and "scrypt" not in raw.lower() and "studentpass1" not in raw
        assert "password" not in page(admin, url).lower().replace("change password", "")      # (the account menu has a "Change password" link)


def test_students_and_visitors_are_kept_out(db, world, anon):
    student, uid, _, _ = world
    for url in ("/admin/performance", f"/admin/performance/{uid}"):
        assert student.get(url).status_code == 403
        r = anon.get(url)
        assert r.status_code == 303 and r.headers["location"].startswith("/login")


def test_viewing_a_students_page_is_audited_but_the_list_is_not(db, world, admin):
    _, uid, _, _ = world
    before = audit_views(db, uid)
    admin.get("/admin/performance")
    assert audit_views(db, uid) == before
    admin.get(f"/admin/performance/{uid}")
    assert audit_views(db, uid) == before + 1
    entry = (db.query(models.AuditLog).filter_by(action="performance.view", entity_id=uid)
             .order_by(models.AuditLog.id.desc()).first())
    assert entry.username == "admin" and entry.entity_type == "user"


# --------------------------------------------------------------------------- the switch

def test_switching_it_off_blocks_both_pages_and_hides_the_students_notice(db, world, admin):
    student, uid, _, _ = world
    assert "Your admin can view your practice results." in page(student, "/account")
    assert "See how users are doing" in page(admin, "/admin")

    assert admin.post("/admin/settings/user-performance", data={"enabled": "0"}).status_code == 303
    for url in ("/admin/performance", f"/admin/performance/{uid}"):
        r = admin.get(url)
        assert r.status_code == 303 and r.headers["location"] == "/admin"
    assert "See how users are doing" not in page(admin, "/admin")
    assert "Your admin can view your practice results." not in page(student, "/account")

    assert admin.post("/admin/settings/user-performance", data={"enabled": "1"}).status_code == 303
    assert admin.get("/admin/performance").status_code == 200
    assert "Your admin can view your practice results." in page(student, "/account")


def test_the_blocked_page_explains_itself_on_the_dashboard(db, admin):
    admin.post("/admin/settings/user-performance", data={"enabled": "0"})
    admin.get("/admin/performance")                                            # sets the flash message
    assert "switched off" in page(admin, "/admin")


def test_a_blocked_view_is_not_audited(db, world, admin):
    _, uid, _, _ = world
    admin.post("/admin/settings/user-performance", data={"enabled": "0"})
    before = audit_views(db, uid)
    admin.get(f"/admin/performance/{uid}")
    assert audit_views(db, uid) == before


def test_students_cannot_flip_the_switch(db, world):
    student, _, _, _ = world
    assert student.post("/admin/settings/user-performance", data={"enabled": "0"}).status_code == 403
    db.rollback()
    assert settings.get_bool(db, "user_performance_enabled") is True
