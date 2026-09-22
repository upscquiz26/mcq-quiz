"""
Student-side Stage 1: security foundation, password reset/change, deactivation, admin dashboard
switches, profile, publish/unpublish, live_questions(), image access, and the route-security sweep.
"""
import json
import os
import re
from datetime import datetime, timedelta

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import create_engine, text

from app import auth, database, ingest, models, settings, throttle
from app.main import app
from app.models import QStatus
from app.practice import pool
from conftest import _client, _login, question_form


# --------------------------------------------------------------------------- helpers

@pytest.fixture(autouse=True)
def clean_login_failures(db):
    """Lockouts are keyed on username and IP, and every test client shares one IP. Start each test clean."""
    db.query(models.LoginFailure).delete()
    db.commit()
    yield
    db.query(models.LoginFailure).delete()
    db.commit()


def add_user(db, username, password="studentpass1", status=models.UserStatus.approved, **fields) -> int:
    user = models.User(username=username, password_hash=auth.hash_password(password), is_admin=False,
                       status=status, **fields)
    db.add(user)
    db.commit()
    return user.id


def login_data(username, password="studentpass1"):
    return {"username": username, "password": password, "next": "/"}


def actions_for(db, action):
    db.rollback()
    return db.query(models.AuditLog).filter_by(action=action).all()


def fresh_user(db, user_id):
    db.rollback()
    return db.get(models.User, user_id)


def make_live_paper(db, make_paper, title, n=3, **fields):
    paper = make_paper(title, n=n, publish_status="published", **fields)
    for q in db.query(models.Question).filter_by(paper_id=paper.id).all():
        q.status = QStatus.LIVE
    db.commit()
    return paper


def live_ids(db, paper):
    db.rollback()
    return {q.question_number for q in pool.live_questions(db).filter(models.Question.paper_id == paper.id).all()}


def flash_text(client, url):
    """Flash messages are shown on the next page view; fetch it and return the page text."""
    return client.get(url).text


# --------------------------------------------------------------------------- schema

def test_users_table_is_upgraded_with_safe_defaults(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'old_users.db'}")
    with engine.begin() as c:
        c.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY, username VARCHAR, password_hash VARCHAR)"))
        c.execute(text("INSERT INTO users (username, password_hash) VALUES ('old', 'x')"))
    monkeypatch.setattr(database, "engine", engine)

    added = database.ensure_columns()

    assert ("users", "session_version") in added and ("users", "must_change_password") in added
    with engine.connect() as c:
        row = c.execute(text("SELECT session_version, show_on_leaderboard, must_change_password, display_name "
                             "FROM users")).one()
    assert tuple(row) == (0, 1, 0, None)          # existing users stay valid, visible and unforced


def test_deactivated_status_can_be_stored(db):
    uid = add_user(db, "storedeactivated", status=models.UserStatus.deactivated)
    assert fresh_user(db, uid).status == models.UserStatus.deactivated


# --------------------------------------------------------------------------- login lockout

def test_five_wrong_passwords_lock_a_username_even_against_the_right_password(db, anon):
    add_user(db, "lockme")
    for _ in range(5):
        assert anon.post("/login", data=login_data("lockme", "wrongpassword")).status_code == 401
    r = anon.post("/login", data=login_data("lockme"))                      # the CORRECT password
    assert r.status_code == 429 and "Too many failed attempts" in r.text
    # Refused attempts are not evaluated, so they neither count nor extend the lock.
    assert db.query(models.LoginFailure).filter_by(key=throttle.key_for_username("lockme")).count() == 5


def test_lockout_message_is_identical_for_unknown_usernames(db, anon):
    add_user(db, "realone")
    for name in ("realone", "nobodyhere"):
        for _ in range(5):
            anon.post("/login", data=login_data(name, "wrongpassword"))
    real = anon.post("/login", data=login_data("realone", "wrongpassword"))
    ghost = anon.post("/login", data=login_data("nobodyhere", "wrongpassword"))
    assert real.status_code == ghost.status_code == 429
    assert re.sub(r"\s+", " ", real.text) == re.sub(r"\s+", " ", ghost.text)


def test_one_locked_user_does_not_lock_others(db, anon):
    add_user(db, "victim")
    add_user(db, "bystander")
    for _ in range(5):
        anon.post("/login", data=login_data("victim", "wrongpassword"))
    assert anon.post("/login", data=login_data("bystander")).status_code == 303


def test_lockout_ends_after_fifteen_minutes(db, anon):
    add_user(db, "waiter")
    for _ in range(5):
        anon.post("/login", data=login_data("waiter", "wrongpassword"))
    assert anon.post("/login", data=login_data("waiter")).status_code == 429

    old = datetime.utcnow() - timedelta(minutes=16)
    db.query(models.LoginFailure).update({"at": old})
    db.commit()
    assert anon.post("/login", data=login_data("waiter")).status_code == 303


def test_a_successful_login_clears_earlier_failures(db, anon):
    add_user(db, "clearing")
    for _ in range(4):
        anon.post("/login", data=login_data("clearing", "wrongpassword"))
    assert anon.post("/login", data=login_data("clearing")).status_code == 303
    for _ in range(4):
        assert anon.post("/login", data=login_data("clearing", "wrongpassword")).status_code == 401


def test_twenty_failures_from_one_address_lock_that_address(db, anon):
    add_user(db, "innocent")
    for i in range(20):
        anon.post("/login", data=login_data(f"guess{i}", "wrongpassword"))
    assert anon.post("/login", data=login_data("innocent")).status_code == 429


def test_seconds_locked_counts_down_from_the_latest_failure(db):
    key = throttle.key_for_username("countdown")
    t0 = datetime(2030, 1, 1, 12, 0, 0)
    for minute in range(5):
        db.add(models.LoginFailure(key=key, ip=None, at=t0 + timedelta(minutes=minute)))
    db.commit()
    # The fifth failure was at minute 4, so the lock runs exactly until minute 19 — however spread out the failures were.
    assert throttle.seconds_locked(db, key, now=t0 + timedelta(minutes=5)) == 14 * 60
    assert throttle.seconds_locked(db, key, now=t0 + timedelta(minutes=15)) == 4 * 60      # not released early
    assert throttle.seconds_locked(db, key, now=t0 + timedelta(minutes=19, seconds=-1)) == 1
    assert throttle.seconds_locked(db, key, now=t0 + timedelta(minutes=19)) == 0
    assert throttle.seconds_locked(db, key, now=t0 + timedelta(minutes=30)) == 0
    assert throttle.describe_wait(601) == "11 minutes" and throttle.describe_wait(30) == "1 minute"


def test_four_failures_do_not_lock(db):
    key = throttle.key_for_username("almost")
    now = datetime(2030, 1, 1, 12, 0, 0)
    for minute in range(4):
        db.add(models.LoginFailure(key=key, ip=None, at=now - timedelta(minutes=minute)))
    db.commit()
    assert throttle.seconds_locked(db, key, now=now) == 0


def test_old_failures_do_not_combine_with_a_new_one_to_lock(db):
    key = throttle.key_for_username("slowguesser")
    t0 = datetime(2030, 1, 1, 12, 0, 0)
    for minute in range(4):                                   # four failures...
        db.add(models.LoginFailure(key=key, ip=None, at=t0 + timedelta(minutes=minute)))
    db.add(models.LoginFailure(key=key, ip=None, at=t0 + timedelta(minutes=40)))   # ...then one much later
    db.commit()
    assert throttle.seconds_locked(db, key, now=t0 + timedelta(minutes=40)) == 0


def test_the_typed_username_is_never_stored_in_plain_text(db, anon):
    anon.post("/login", data=login_data("Sup3rSecretTypedThing", "wrongpassword"))
    keys = [k for (k,) in db.query(models.LoginFailure.key).all()]
    assert keys and all("secret" not in k.lower() for k in keys)


# --------------------------------------------------------------------------- admin password reset

def test_admin_reset_gives_a_one_time_temporary_password_and_forces_a_change(admin, anon, db):
    uid = add_user(db, "resetme", "oldpassword1")
    student = _login(_client(), "resetme", "oldpassword1")
    assert student.get("/").status_code == 200

    r = admin.post(f"/admin/users/{uid}/reset-password")
    assert r.status_code == 200 and "no-store" in r.headers["cache-control"]
    temp = re.search(r'id="temp-password">\s*([^<\s]+)\s*<', r.text).group(1)
    assert len(temp) == 12 and not set(temp) & set("0O1lI")

    assert student.get("/").status_code == 303                                    # their live session was ended
    assert anon.post("/login", data=login_data("resetme", "oldpassword1")).status_code == 401
    fresh = _client()
    assert fresh.post("/login", data=login_data("resetme", temp)).status_code == 303

    # Until they choose a password, everything else redirects to the change page...
    for path in ("/", "/account", "/upload"):
        r = fresh.get(path)
        assert r.status_code == 303 and r.headers["location"] == "/account/password", path
    page = fresh.get("/account/password")
    assert page.status_code == 200 and "Choose a new password" in page.text
    assert fresh.post("/logout").status_code == 303                                # ...except signing out
    fresh.post("/login", data=login_data("resetme", temp))

    # The change itself is validated.
    def change(current, new, confirm=None):
        return fresh.post("/account/password", data={
            "current_password": current, "new_password": new, "confirm_password": confirm or new})
    assert change("not-the-temp", "brandnewpass1").status_code == 400
    assert change(temp, "short").status_code == 400
    assert change(temp, "brandnewpass1", "different1234").status_code == 400
    assert change(temp, temp).status_code == 400
    assert change(temp, "brandnewpass1").status_code == 303
    assert fresh.get("/").status_code == 200                                       # no longer forced

    # The temporary password lives nowhere in plain text.
    user = fresh_user(db, uid)
    assert temp not in user.password_hash and not user.must_change_password
    resets = actions_for(db, "user.password_reset")
    assert resets and all(temp not in (r.detail_json or "") for r in resets)
    everything = json.dumps([(a.action, a.detail_json) for a in db.query(models.AuditLog).all()])
    assert temp not in everything and "brandnewpass1" not in everything
    assert actions_for(db, "user.password_change")


def test_reset_also_lifts_a_lockout(admin, anon, db):
    uid = add_user(db, "lockedout")
    for _ in range(5):
        anon.post("/login", data=login_data("lockedout", "wrongpassword"))
    assert anon.post("/login", data=login_data("lockedout")).status_code == 429
    r = admin.post(f"/admin/users/{uid}/reset-password")
    temp = re.search(r'id="temp-password">\s*([^<\s]+)\s*<', r.text).group(1)
    assert _client().post("/login", data=login_data("lockedout", temp)).status_code == 303


def test_only_ordinary_active_accounts_can_be_reset(admin, db):
    admin_id = db.query(models.User).filter_by(username="admin").one().id
    pending = add_user(db, "stillpending", status=models.UserStatus.pending)
    assert admin.post(f"/admin/users/{admin_id}/reset-password").status_code == 400
    assert admin.post(f"/admin/users/{pending}/reset-password").status_code == 400
    assert admin.post("/admin/users/999999/reset-password").status_code == 404


def test_the_reset_page_is_the_only_place_a_temporary_password_appears(admin, db):
    uid = add_user(db, "onceonly")
    response = admin.post(f"/admin/users/{uid}/reset-password")
    temp = re.search(r'id="temp-password">\s*([^<\s]+)\s*<', response.text).group(1)

    # It was on the reset response itself...
    assert temp in response.text
    # ...and it is nowhere else an admin can look, not even the very next page load.
    for path in ("/admin/users", "/admin", "/admin/audit", "/admin/audit?action=user.password_reset", "/"):
        assert temp not in admin.get(path).text, f"temporary password leaked onto {path}"
    assert "Must choose a new password" in admin.get("/admin/users").text
    assert temp not in fresh_user(db, uid).password_hash


# --------------------------------------------------------------------------- changing your own password

def test_changing_password_signs_out_other_devices_but_not_this_one(db):
    add_user(db, "twodevices")
    a = _login(_client(), "twodevices", "studentpass1")
    b = _login(_client(), "twodevices", "studentpass1")
    r = a.post("/account/password", data={"current_password": "studentpass1",
                                          "new_password": "changedpass99", "confirm_password": "changedpass99"})
    assert r.status_code == 303
    assert a.get("/").status_code == 200
    assert b.get("/").status_code == 303


def test_wrong_current_password_is_rate_limited(db):
    add_user(db, "guessingcurrent")
    c = _login(_client(), "guessingcurrent", "studentpass1")
    form = {"current_password": "wrongwrong1", "new_password": "changedpass99", "confirm_password": "changedpass99"}
    for _ in range(5):
        assert c.post("/account/password", data=form).status_code == 400
    r = c.post("/account/password", data={**form, "current_password": "studentpass1"})   # even the right one
    assert r.status_code == 429 and "Too many wrong attempts" in r.text


# --------------------------------------------------------------------------- deactivate / reactivate

def test_deactivation_signs_a_user_out_at_once_and_can_be_undone(admin, anon, db):
    uid = add_user(db, "deact")
    c = _login(_client(), "deact", "studentpass1")
    assert c.get("/").status_code == 200

    assert admin.post(f"/admin/users/{uid}/deactivate").status_code == 303
    assert c.get("/").status_code == 303                                              # signed out immediately
    r = anon.post("/login", data=login_data("deact"))
    assert r.status_code == 403 and "deactivated" in r.text
    assert anon.post("/login", data=login_data("deact", "wrongpassword")).status_code == 401   # no status leak

    assert admin.post(f"/admin/users/{uid}/reactivate").status_code == 303
    assert anon.post("/login", data=login_data("deact")).status_code == 303
    assert actions_for(db, "user.deactivate") and actions_for(db, "user.reactivate")


def test_deactivate_and_reactivate_only_apply_to_the_right_accounts(admin, db):
    pending = add_user(db, "pend2", status=models.UserStatus.pending)
    active = add_user(db, "active2")
    admin_id = db.query(models.User).filter_by(username="admin").one().id
    assert admin.post(f"/admin/users/{pending}/deactivate").status_code == 400
    assert admin.post(f"/admin/users/{admin_id}/deactivate").status_code == 400
    assert admin.post(f"/admin/users/{active}/reactivate").status_code == 400
    page = admin.get("/admin/users")
    assert "Deactivate" in page.text and "Reset password" in page.text


# --------------------------------------------------------------------------- sessions and cross-site requests

def test_session_cookie_is_httponly_lax_and_expires(db, anon):
    add_user(db, "cookieuser")
    cookie = anon.post("/login", data=login_data("cookieuser")).headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie and "max-age=1209600" in cookie


def test_cross_site_posts_are_refused(admin, anon):
    url = "/admin/backups/create"
    assert admin.post(url, headers={"Origin": "http://evil.example"}).status_code == 403
    assert admin.post(url, headers={"Origin": "null"}).status_code == 403
    assert admin.post(url, headers={"Referer": "http://evil.example/page"}).status_code == 403
    assert admin.post(url, headers={"Origin": "http://testserver"}).status_code == 303   # same site is fine
    assert admin.post(url).status_code == 303                                            # non-browser client
    assert anon.post("/login", data=login_data("x"), headers={"Origin": "http://evil.example"}).status_code == 403


def test_api_docs_are_not_exposed(admin, anon):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert admin.get(path).status_code == 404
        assert anon.get(path).status_code == 303


def test_last_active_is_recorded(admin, db):
    uid = add_user(db, "activeuser")
    c = _login(_client(), "activeuser", "studentpass1")
    c.get("/")
    assert fresh_user(db, uid).last_active_at is not None


# --------------------------------------------------------------------------- dashboard switches and profile

def test_dashboard_switches_default_on_and_are_admin_controlled(admin, make_user, db):
    assert settings.get_bool(db, "leaderboard_enabled") and settings.get_bool(db, "user_performance_enabled")
    page = admin.get("/admin")
    assert page.status_code == 200 and "User performance" in page.text and "Leaderboard" in page.text

    assert admin.post("/admin/settings/user-performance", data={"enabled": "0"}).status_code == 303
    assert admin.post("/admin/settings/leaderboard", data={"enabled": "0"}).status_code == 303
    db.rollback()
    assert not settings.get_bool(db, "user_performance_enabled") and not settings.get_bool(db, "leaderboard_enabled")
    entries = actions_for(db, "settings.change")
    assert {json.loads(e.detail_json)["setting"] for e in entries} >= {"user_performance_enabled", "leaderboard_enabled"}

    assert admin.post("/admin/settings/nonsense", data={"enabled": "1"}).status_code == 404
    student = make_user("switchstudent")
    assert student.post("/admin/settings/leaderboard", data={"enabled": "1"}).status_code == 403
    assert student.get("/admin").status_code == 403

    admin.post("/admin/settings/user-performance", data={"enabled": "1"})
    admin.post("/admin/settings/leaderboard", data={"enabled": "1"})


def test_students_are_told_when_the_admin_can_see_their_results(admin, make_user, db):
    student = make_user("noticestudent")
    settings.set_bool(db, "user_performance_enabled", True)
    db.commit()
    assert "Your admin can view your practice results." in student.get("/account").text
    admin.post("/admin/settings/user-performance", data={"enabled": "0"})
    assert "Your admin can view your practice results." not in student.get("/account").text
    admin.post("/admin/settings/user-performance", data={"enabled": "1"})


def test_profile_display_name_and_leaderboard_opt_out(make_user, db):
    c = make_user("profileuser")
    uid = db.query(models.User).filter_by(username="profileuser").one().id

    r = c.post("/account/profile", data={"display_name": "  Priya   S ", "show_on_leaderboard": "true"})
    assert r.status_code == 303
    user = fresh_user(db, uid)
    assert user.display_name == "Priya S" and user.show_on_leaderboard is True
    assert "Priya S" in c.get("/").text                                         # shown in the header

    c.post("/account/profile", data={"display_name": "Priya S"})                # checkbox left unticked
    assert fresh_user(db, uid).show_on_leaderboard is False
    c.post("/account/profile", data={"display_name": "", "show_on_leaderboard": "true"})
    assert fresh_user(db, uid).display_name is None                             # blank falls back to the username


@pytest.mark.parametrize("name,fragment", [("x", "2–30"), ("y" * 31, "2–30"), ("admin", "already in use")])
def test_profile_rejects_bad_or_borrowed_display_names(make_user, name, fragment):
    c = make_user("namecheck")
    r = c.post("/account/profile", data={"display_name": name})
    assert r.status_code == 400 and fragment in r.text


def test_display_names_must_be_unique(make_user, db):
    first = make_user("firstnamer")
    first.post("/account/profile", data={"display_name": "Unique Name"})
    second = make_user("secondnamer")
    assert second.post("/account/profile", data={"display_name": "unique name"}).status_code == 400


# --------------------------------------------------------------------------- live_questions()

def test_live_questions_only_returns_what_a_student_may_see(db, make_paper):
    paper = make_live_paper(db, make_paper, "Pool paper", n=6)
    qs = {q.question_number: q for q in db.query(models.Question).filter_by(paper_id=paper.id)}
    qs[2].status = QStatus.NEEDS_REVIEW                     # unreviewed
    qs[3].status = QStatus.QUARANTINED
    qs[4].correct_answer = None                             # no answer
    qs[5].has_image = True                                  # needs an image and has no snapshot
    qs[6].has_image, qs[6].source_image_path = True, "q6.jpg"   # needs an image and has one
    db.commit()
    assert live_ids(db, paper) == {1, 6}


@pytest.mark.parametrize("change", ["draft", "archived", "processing"])
def test_a_paper_that_is_not_published_ready_and_active_contributes_nothing(db, make_paper, change):
    paper = make_live_paper(db, make_paper, f"Hidden paper {change}", n=2)
    assert live_ids(db, paper) == {1, 2}
    db.rollback()
    paper = db.get(models.Paper, paper.id)
    if change == "draft":
        paper.publish_status = "draft"
    elif change == "archived":
        paper.archived_at = datetime.utcnow()
    else:
        paper.status = "processing"
    db.commit()
    assert live_ids(db, paper) == set()


# --------------------------------------------------------------------------- publish / unpublish

def test_publishing_needs_every_question_confirmed_then_makes_them_live(admin, make_user, db, make_paper):
    paper = make_paper("Publish flow paper", n=3)
    student = make_user("publishwatcher")

    assert admin.post(f"/papers/{paper.id}/publish").status_code == 303
    assert "still to confirm" in flash_text(admin, f"/review/{paper.id}")
    assert "Publish flow paper" not in student.get("/").text
    assert live_ids(db, paper) == set()
    assert "disabled" in admin.get(f"/review/{paper.id}").text                    # the button is disabled too

    admin.post(f"/review/{paper.id}/confirm-clean")
    assert admin.post(f"/papers/{paper.id}/publish").status_code == 303
    db.rollback()
    paper = db.get(models.Paper, paper.id)
    assert paper.publish_status == "published" and paper.published_at is not None
    assert live_ids(db, paper) == {1, 2, 3}
    assert "Publish flow paper" in student.get("/").text
    entry = actions_for(db, "paper.publish")[-1]
    assert json.loads(entry.detail_json)["made_live"] == 3

    assert admin.post(f"/papers/{paper.id}/unpublish").status_code == 303
    assert live_ids(db, paper) == set()
    assert {q.status for q in db.query(models.Question).filter_by(paper_id=paper.id)} == {QStatus.VERIFIED}
    assert "Publish flow paper" not in student.get("/").text
    assert actions_for(db, "paper.unpublish")


def test_editing_a_live_question_removes_it_from_students_until_reconfirmed(admin, db, make_paper):
    paper = make_paper("Live edit publish paper", n=2)
    admin.post(f"/review/{paper.id}/confirm-clean")
    admin.post(f"/papers/{paper.id}/publish")
    assert live_ids(db, paper) == {1, 2}

    q = db.query(models.Question).filter_by(paper_id=paper.id, question_number=1).one()
    admin.post(f"/review/{paper.id}/question/{q.id}", data=question_form(q, text="Reworded?"))
    assert live_ids(db, paper) == {2}
    assert "1 still to confirm" in admin.get(f"/review/{paper.id}").text.replace("\n", " ") or \
        "still to confirm" in admin.get(f"/review/{paper.id}").text


def test_image_dependent_questions_without_a_snapshot_are_left_out_and_reported(admin, db, make_paper):
    paper = make_paper("Image paper", n=2)
    db.query(models.Question).filter_by(paper_id=paper.id, question_number=1).one().has_image = True
    db.commit()
    admin.post(f"/review/{paper.id}/confirm-clean")
    admin.post(f"/papers/{paper.id}/publish")
    assert live_ids(db, paper) == {2}
    assert "depend on an image" in admin.get(f"/review/{paper.id}").text or "left out" in flash_text(admin, "/")


def test_the_review_form_can_mark_a_question_as_image_dependent(admin, db, make_paper):
    paper = make_paper("Has image form paper", n=1)
    q = db.query(models.Question).filter_by(paper_id=paper.id).one()
    admin.post(f"/review/{paper.id}/question/{q.id}", data=question_form(q, has_image="true"))
    db.rollback()
    assert db.get(models.Question, q.id).has_image is True
    assert any("has_image" in json.loads(e.detail_json)["fields"] for e in actions_for(db, "question.edit"))


def test_archiving_a_published_paper_hides_it_from_students(admin, make_user, db, make_paper):
    paper = make_live_paper(db, make_paper, "Archive published paper", n=2)
    student = make_user("archivewatcher")
    assert "Archive published paper" in student.get("/").text
    admin.post(f"/papers/{paper.id}/archive")
    assert "Archive published paper" not in student.get("/").text
    assert live_ids(db, paper) == set()


# --------------------------------------------------------------------------- question images

def test_students_only_get_snapshots_of_live_image_dependent_questions(admin, make_user, db, make_paper):
    paper = make_live_paper(db, make_paper, "Media paper", n=3)
    folder = ingest.images_dir_for(paper.id)
    os.makedirs(folder, exist_ok=True)
    for n in (1, 2, 3):
        with open(os.path.join(folder, f"q{n}.jpg"), "wb") as f:
            f.write(b"\xff\xd8\xff fake jpeg")
    q1, q2, q3 = (db.query(models.Question).filter_by(paper_id=paper.id, question_number=n).one() for n in (1, 2, 3))
    q1.has_image, q1.source_image_path = True, "q1.jpg"       # live, needs its image -> visible
    q2.source_image_path = "q2.jpg"                          # live text question -> snapshot is a review aid only
    q3.has_image, q3.source_image_path, q3.status = True, "q3.jpg", QStatus.NEEDS_REVIEW   # not live
    db.commit()

    student = make_user("mediastudent")
    base = f"/media/{paper.id}"
    assert student.get(f"{base}/q1.jpg").status_code == 200
    assert student.get(f"{base}/q2.jpg").status_code == 404
    assert student.get(f"{base}/q3.jpg").status_code == 404
    assert student.get(f"/media/{paper.id + 999}/q1.jpg").status_code == 404
    for name in ("../../upsc_pyq.db", "q1.png", "q1.jpg/../x"):
        assert student.get(f"{base}/{name}").status_code == 404
    for n in (1, 2, 3):
        assert admin.get(f"{base}/q{n}.jpg").status_code == 200                     # the admin sees them all

    admin.post(f"/papers/{paper.id}/unpublish")
    assert student.get(f"{base}/q1.jpg").status_code == 404


# --------------------------------------------------------------------------- where people land after logging in

def test_after_login_the_admin_lands_on_the_dashboard_and_students_on_their_home(db, anon):
    add_user(db, "landingstudent")
    admin_client = _client()
    r = admin_client.post("/login", data={"username": "admin", "password": "adminpass1", "next": "/"})
    assert r.status_code == 303 and r.headers["location"] == "/admin"
    assert admin_client.get("/admin").status_code == 200

    student = _client()
    r = student.post("/login", data=login_data("landingstudent"))
    assert r.status_code == 303 and r.headers["location"] == "/"
    home = student.get("/")
    assert home.status_code == 200 and "Welcome, landingstudent" in home.text


def test_a_page_the_user_was_heading_to_is_still_honoured(db):
    add_user(db, "deeplinker")
    c = _client()
    assert c.get("/account").headers["location"].startswith("/login?next=")
    r = c.post("/login", data={"username": "deeplinker", "password": "studentpass1", "next": "/account"})
    assert r.headers["location"] == "/account"
    admin_client = _client()
    r = admin_client.post("/login", data={"username": "admin", "password": "adminpass1", "next": "/admin/users"})
    assert r.headers["location"] == "/admin/users"


def test_visiting_the_login_page_while_signed_in_goes_to_your_home(admin, make_user):
    assert admin.get("/login").headers["location"] == "/admin"
    assert make_user("alreadyin").get("/login").headers["location"] == "/"


def test_student_home_shows_a_welcome_and_only_papers_with_live_questions(db, make_paper, make_user):
    student = make_user("homestudent")
    student.post("/account/profile", data={"display_name": "Asha K", "show_on_leaderboard": "true"})
    live = make_live_paper(db, make_paper, "Home live paper", n=4)
    draft = make_paper("Home draft paper", n=2)                                       # not published
    held = make_live_paper(db, make_paper, "Home image-only paper", n=1)              # published, but nothing usable
    q = db.query(models.Question).filter_by(paper_id=held.id).one()
    q.has_image, q.source_image_path = True, None
    db.commit()

    page = student.get("/")
    assert page.status_code == 200
    assert "Welcome, Asha K" in page.text
    assert "Home live paper" in page.text and "4 questions" in page.text
    assert "Home draft paper" not in page.text and "Home image-only paper" not in page.text
    for admin_only in ("/upload", "/admin", "Publish"):
        assert admin_only not in page.text.replace("/account", "")


def test_student_home_has_a_friendly_empty_state(make_user, db):
    db.rollback()
    for paper in db.query(models.Paper).filter(models.Paper.publish_status == "published").all():
        paper.publish_status = "draft"                                                # nothing published anywhere
    db.commit()
    page = make_user("emptyhome").get("/")
    assert page.status_code == 200 and "No papers are available yet" in page.text


# --------------------------------------------------------------------------- the route-security sweep

ADMIN_PREFIXES = ("/admin", "/review", "/upload", "/quarantine", "/papers")
LOGIN_ONLY = {"/", "/logout"}
LOGIN_ONLY_PREFIXES = ("/account", "/media", "/practice", "/attempts", "/tests", "/revision", "/questions", "/bookmarks", "/leaderboard",
                       "/analytics")
PUBLIC = {"/login", "/signup"}


def classify(path: str) -> str:
    if path in PUBLIC:
        return "public"
    if path.startswith(ADMIN_PREFIXES):
        return "admin"
    if path in LOGIN_ONLY or path.startswith(LOGIN_ONLY_PREFIXES):
        return "login"
    pytest.fail(f"Route {path!r} is not classified. Decide who may use it (public, any signed-in user, "
                "or admin only) and add it to ADMIN_PREFIXES / LOGIN_ONLY / PUBLIC in this test.")


def all_routes():
    """Every (path, method) in the app. Logout goes last: hitting it signs the test user out."""
    found = [(route.path, method)
             for route in app.routes if isinstance(route, APIRoute)
             for method in sorted(route.methods - {"HEAD", "OPTIONS"})]
    return sorted(found, key=lambda pm: pm[0] == "/logout")


def concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "1", path)


def test_every_route_is_classified_and_enforced(anon, make_user):
    student = make_user("sweepstudent")
    seen = 0
    for path, method in all_routes():
        kind = classify(path)
        url = concrete(path)
        seen += 1

        anon_response = getattr(anon, method.lower())(url)
        if kind == "public":
            assert not (anon_response.status_code == 303 and anon_response.headers["location"].startswith("/login?")), \
                f"{method} {path} should be reachable without logging in"
        else:
            assert anon_response.status_code == 303 and anon_response.headers["location"].startswith("/login"), \
                f"{method} {path} must send anonymous visitors to the login page"

        student_response = getattr(student, method.lower())(url)
        if kind == "admin":
            assert student_response.status_code == 403, f"{method} {path} must reject students (got {student_response.status_code})"
        elif kind == "login":
            assert student_response.status_code != 403, f"{method} {path} should be open to any signed-in user"
        assert student_response.status_code != 500 and anon_response.status_code != 500, f"{method} {path} crashed"
    assert seen > 30                                   # the sweep is really looking at the whole app


def test_admin_pages_reject_students_who_type_the_url(make_user):
    student = make_user("urltyper")
    for url in ("/admin", "/admin/users", "/admin/audit", "/admin/backups", "/upload", "/review/1",
                "/review/1/question/1/history", "/quarantine"):
        assert student.get(url).status_code == 403, url
