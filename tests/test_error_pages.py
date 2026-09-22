"""Friendly error pages for browsers; plain JSON errors for everything else."""
import html as html_
import re


def text(body):
    return re.sub(r"\s+", " ", html_.unescape(re.sub(r"<[^>]+>", " ", body))).strip()


BROWSER = {"Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}


def test_a_browser_gets_a_proper_403_page_inside_the_layout(make_user):
    student = make_user("errstudent")
    r = student.get("/admin", headers=BROWSER)
    assert r.status_code == 403 and "text/html" in r.headers["content-type"]
    page = text(r.text)
    assert "You don't have access to this page" in page and "403" in page and "Go to the home page" in page
    assert 'class="sidebar"' in r.text and "Your account" in page                     # still inside the app's layout
    assert 'href="/admin/users"' not in r.text                                         # and the student still sees no admin links


def test_a_browser_gets_a_proper_404_page_with_the_reason_when_there_is_one(admin):
    r = admin.get("/review/999999", headers=BROWSER)
    assert r.status_code == 404 and "We couldn't find that page" in text(r.text) and "Paper not found" in text(r.text)
    r = admin.get("/admin/import/json/apply-nothing", headers=BROWSER)
    assert r.status_code == 404 and "We couldn't find that page" in text(r.text)
    r = admin.post("/admin/import/json/apply", data={"token": "0" * 32, "title": "x"}, headers=BROWSER)
    assert r.status_code == 404 and "That import session has expired" in text(r.text)      # the specific reason is shown


def test_scripts_and_the_autosave_calls_still_get_json_errors(admin, make_user):
    r = admin.get("/review/999999")
    assert r.status_code == 404 and r.json() == {"detail": "Paper not found"}
    student = make_user("errstudent2")
    r = student.get("/admin", headers={"Accept": "application/json"})
    assert r.status_code == 403 and r.json() == {"detail": "Admin only"}


def test_a_signed_out_visitor_is_still_sent_to_log_in(anon):
    r = anon.get("/no/such/page", headers=BROWSER)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


def test_the_405_page_is_friendly_too(admin):
    r = admin.post("/admin", headers=BROWSER)
    assert r.status_code == 405 and "That isn't allowed here" in text(r.text)


def test_empty_states_offer_a_next_step(make_user):
    student = make_user("emptystudent")
    page = text(student.get("/revision").text)
    assert "Nothing here yet" in page and "Start practising" in page
