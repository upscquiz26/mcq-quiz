# UPSC PYQ Practice — Milestone 1

Standalone local app. Everything (question bank, attempts, mistake history)
lives in one SQLite file at `data/upsc_pyq.db`. No AI service and no API key:
questions are read with OCR (Tesseract) on your own computer.

## What's built right now

- Accounts: an admin, plus other users who request an account and wait for the
  admin to approve it
- Upload a question paper PDF, plus (optionally) its answers-and-explanations PDF
- Questions are read from the PDF with OCR, in the background, with a progress
  indicator
- Answers and explanations come from the answer PDF exactly as printed
- Review screen: every question shows a snapshot of the printed page next to the
  OCR text, so you can check and fix it; suspicious questions carry warnings
- Subjects can be set for many questions at once ("1-30 History, 31-60 Geography")
- Every paper records its source (official PYQ or coaching test), series, expected
  question count and marking scheme (with UPSC GS/CSAT presets)
- Safety net: an audit log of who changed what, version history with undo for every
  question edit, quarantine instead of deleting, papers archived instead of deleted,
  and an automatic database backup before every import
- Data model already supports the full design we discussed: attempts (never
  overwritten, so retakes show progress), confidence-based responses
  (sure/guessed/no_idea), auto-tagged mistake reasons, negative marking flags,
  difficulty tags, and growing per-subject topic lists

## What's NOT built yet (next milestones)

The student side (practice, timed tests, results, revision, progress, admin performance view,
leaderboards, reports, daily target and streak) is complete, and so is import stage 2 (text-layer
reading with an OCR fallback), import stage 3 (answer keys) and the JSON import. Still to come from the "reliable import" plan:

- The extra import gates (sample audit, key-layout checks) — the current Publish is a
  minimal version
- JSON import, duplicate detection, optional CSV/XLSX/DOCX/image import
- Hindi from PDFs (only the English column is read); Hindi comes only through JSON import — see "Hindi through JSON" below

## Setup

Install Python 3.10+ and **Tesseract OCR**:

```powershell
winget install UB-Mannheim.TesseractOCR
```

(If it lands somewhere unusual, set `TESSERACT_CMD` to the path of `tesseract.exe`.)

```powershell
cd upsc_pyq_app
python -m venv venv
.\venv\Scripts\pip install -r requirements.txt
copy .env.example .env          # then edit .env
.\venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000

## Accounts

Set `APP_PASSWORD` (and optionally `APP_USERNAME`, `SECRET_KEY`) in `.env`. On
first start this creates the **admin** account. After that the admin's password
is stored hashed in the database and those two variables are ignored.

Other people click "Request one" on the login page, choose a username and
password, and wait. The admin approves or rejects requests under **Users**.
Only the admin can upload and review papers. A rejected username can't request
again until the admin removes it.

**Admin dashboard** (`/admin`): headline tiles (accounts waiting for approval, questions to confirm, open reports, students
active this week and timed tests finished, questions live), a "Needs your attention" list with one-click links (pending accounts, reports,
failed imports, papers awaiting review, papers ready to publish), recent activity from the audit log, and two
on/off switches — *User performance* (default on; when on, every user sees "Your admin
can view your practice results." in their profile) and *Leaderboard* (default on).

**Managing users** (Users page): approve, reject, **deactivate** (signed out at once,
history kept, can be reactivated), remove, and **reset password**. A reset shows a
temporary password to the admin **once**; the user must choose their own at next
login, all their sessions end, and any login lockout is lifted. The admin can never
see an existing password. Resets are written to the audit log (without the password).

**Users** can change their password (asking for the current one first) and set a
display name and "Show me on leaderboards" under their name in the header. Changing a
password signs out their other devices.

**Login safety:** 5 wrong passwords for a username (or 20 from one address) locks it for
15 minutes, and the message is the same whether or not the username exists. Sessions
last 14 days, are `HttpOnly` and `SameSite=Lax`, and requests whose `Origin` names
another site are refused. Set `SESSION_HTTPS_ONLY=1` if you serve the app over HTTPS.

## Practising (students)

**Practice** in the top bar opens topic practice: untimed, with the correct answer and the
explanation shown straight after each answer.

- **Choosing:** filter by source (official PYQ / coaching test), year, subject, topic, difficulty,
  and "only questions I haven't answered yet", then pick how many questions (5–50). The page shows how
  many questions match as you change the filters. Only **live** questions are ever offered.
- **Answering:** pick an option *and* how sure you are (Sure / Guessed / No idea), then Check answer.
  Nothing about the answer is sent to the browser before that. Once answered, a question is locked.
- **Resuming:** sessions are saved on the server after every answer. Close the browser and come back
  later — *Pick up where you left off* (Practice page) and *Continue where you left off* (home) take you to
  the first unanswered question, from any device.
- **Never overwritten:** practising again starts a new attempt; earlier ones stay as they were.
- **Marks:** timed full-paper tests copy the student's selected marks and penalty onto each answer. The paper's
  saved scheme is suggested by default; otherwise the form starts at 2 marks and ⅓ negative. Full-paper practice
  has no timer or negative penalty. Time per question is measured on the server, not the browser.
- **If a paper is unpublished mid-session,** its questions show "no longer available" straight away.
- **Explanations** are labelled *Verified* only if the admin ticked "I have checked this explanation" on the
  review page; otherwise *Unverified (as printed in the answer PDF)*.

## Timed tests (students)

**Tests** in the top bar. Four choices:

- **Full-length:** a whole paper in its own order. Choose the time limit, marks per question and wrong-answer
  penalty when you start; saved paper settings are defaults, and missing settings use 2 marks and ⅓ negative.
- **Full-paper practice:** the whole paper in order, untimed, with immediate feedback and no negative penalty.
- **Sectional:** one paper's whole subject section (e.g. "Test-1 · History"), 72 seconds per question.
- **Custom timed test:** a random selection on one subject with your filters. Practice only, never ranked.

**How they behave**

- **The clock is the server's.** The deadline is fixed when the test starts. Refreshing, closing the browser or
  opening it on another device never resets or pauses it. When time is up the test is submitted for you with
  whatever was saved (status *expired*). A test nobody comes back to is still marked, on the next visit or when
  the app next starts, so it can't be left open to avoid a bad score. Saves are accepted for 3 seconds past the
  deadline to allow for network delay.
- **Answers autosave** after every action (pick an option, rate confidence, mark for review, clear) and can be
  changed until the end. Nothing is marked, and nothing about correctness is sent to the browser, until the end.
- **Palette:** answered, not answered, not visited, and a ★ for marked for review. On phones it collapses.
- **Submit:** a confirmation screen shows how many are answered, unanswered, marked and unvisited.
- **Marking:** right answers earn the marks; wrong answers lose the negative fraction of them (2 marks and ⅓ →
  −0.67); skipped questions are never penalised. Each answer is marked with the marks the test *started* with, so
  editing a paper afterwards never changes a past result.
- **Ranking rules (used by the leaderboard stage):** only standard-settings whole-paper and whole-paper-subject
  sittings get a rank key, and a user's *first started* attempt at that exact question set is the one that can count —
  even if it ran out of time. Student-customized time or scoring settings are scored but not ranked. Starting the same
  timed test while one is open resumes it.
- **Confidence** is optional in a test: an answer saved without one still counts and is just left out of the
  confidence analysis.

**Admin:** on a paper's review page, **Marking scheme and time** can set expected questions and the paper's standard
marks, negative marking (`1/3` or `0.3333`) and suggested time. Students choose time and scoring for each full-length
sitting; missing paper settings do not block timed tests. Only settings matching the saved paper standard can rank.
Leave suggested time blank and the default is about 72 seconds per question. Changes affect only tests started afterwards.

## Results (students)

Finishing a session (or a test running out of time) opens its results:

- **Score and counts:** correct / wrong / skipped, accuracy on what you answered, and your **attempt rate**
  (answered ÷ total). Tests also show the score out of the maximum, marks gained and lost, and time taken.
- **Guessing report:** a table of how you did when you said Sure / Guessed / No idea, the marks your guessed and
  no-idea answers netted, and what your marks would have been if you had skipped them. It is information, not a rule.
- **Why the wrong answers went wrong:** a suggested reason for each wrong answer (below), counted up.
- **By subject** (tests): questions, attempted, right, wrong, skipped, accuracy and marks per subject.
- **Should you have attempted more?** (tests): the break-even accuracy for the paper's marking scheme (25% for the
  usual ⅓ penalty), and an *estimate* of how many skipped questions were worth attempting, using your own accuracy in
  each subject across everything you've finished (once you have 5+ answers there; otherwise your overall accuracy).
- **Question by question:** filter to Wrong / Skipped / Guessed-or-no-idea. Each question opens a review page with your
  answer, the correct answer, the explanation (labelled Verified or Unverified), time spent and marks.

**Mistake reasons** are suggested for wrong answers only, in this order:

| You said | Suggested reason |
|---|---|
| No idea | Concept gap |
| Guessed | Guess |
| Sure, answered in under a quarter of your typical time (at least 8 s) | Careless mistake |
| Sure, took more than 2.5× your typical time | Time pressure / confusion |
| Sure, otherwise | Misconception / overconfidence |
| No rating given | Not classified |

"Typical time" is the median time you spent on the questions you answered in that session. Speed is only used for
"sure" answers: a fast "no idea" is still a gap in knowledge. **You can change any reason** (and add a note to
yourself) on the question's review page; your choice is kept and *Back to suggested* undoes it.

## Revision (students)

**Revision** in the top bar. Everything here is private to the student.

- **Mistake notebook:** every live question you got wrong, or got right but only *guessed* (or had "no idea" on).
  Each entry shows how often you got it wrong, how often you guessed it right, your latest mistake reason, and
  where it is on the schedule. Filter by state (to revise / due now / guessed only / mastered), subject, topic and reason.
- **Spaced repetition.** A wrong answer puts the question on your schedule, due the next day. Wrong questions come back
  after **1, 3, 7 and then 15 days**, each review moving one step up that ladder. A question leaves the schedule
  ("mastered") after **two clean right answers in a row at reviews**. The rules, in full:
  - Only answers given when a question is *due* count as reviews. Extra practice before then changes nothing, except
    that a wrong answer still breaks the streak. (So practising early can't rush a question out of the schedule.)
  - A wrong answer at a review breaks the streak but the ladder keeps moving, so a question you keep missing reaches the
    7- and 15-day gaps. (If a wrong answer sent it back to 1 day, two-in-a-row would make 7 and 15 unreachable.)
  - A right answer only counts as clean if you were **sure** of it or didn't rate it. A lucky "guessed" / "no idea" is
    treated like a miss, because you haven't shown you know it.
  - Missed days roll over: anything due on or before today is simply due now, and the next gap counts from the day you
    actually review it.
  - Right answers never *create* a schedule entry. A guessed-right question sits in the notebook as "Guessed".
  - It works from every mode: topic practice, mistake practice, and tests (a test's answers count when it is finished).
  - A mastered question that you get wrong again goes back to the start.
- **Due now** (Revision page and home): one button revises what's due, most overdue first.
- **Practise my mistakes:** an untimed session (with instant answers, like topic practice) drawn at random from your
  mistakes, filtered by subject, topic and reason. Mastered ones are left out unless you tick "include".
- **Bookmarks and private notes:** once a question's answer is showing — after answering it in practice, or in the review
  of a finished test — you can bookmark it and keep a note (up to 2000 characters). They are not shown during a test.
  Bookmarks are listed under *My bookmarks*.
- **A question's own page** (from the notebook or bookmarks) shows the answer, explanation, your answer history and its
  schedule. It only opens for a live question you have already met, only shows the answer once you've finished with the
  question, and **never while that question is in a timed test you are still sitting**, so it can't be used to peek.

## Home and progress (students)

**Home** shows what needs attention: revision due today, your unfinished session or test (with time left), your recent tests
with scores, and — if a topic is weak — a *Where to focus* card with a **Practise these** button.

**Progress** (top bar) is your own analytics. A date-range row at the top (all time / last 30 / last 90 days) scopes
everything except the last section.

- **Key numbers:** timed tests taken, average test score, accuracy, average time per answer.
- **Score trend:** each finished timed test as a percentage of its maximum marks (a line; needs two tests).
- **Where to focus:** the topics — or the subjects, if the admin hasn't assigned topics — where you're **below 60% with at
  least 5 answers** (fewer than that is too little to judge). *Practise these* starts a 10-question session drawn from them.
- **Accuracy by subject / by topic:** bars with weak areas in blue and everything else grey, weakest first. Areas with fewer than
  5 answers appear in the table only.
- **Time per answer:** average time by subject.
- **What official PYQs ask about:** how many live official-PYQ questions each topic has (subjects if no topics are set).
  This describes the question bank, not you, so it's the same for everyone and ignores the date range.

Every chart has a **View as a table** twin, the value is labelled on the mark (never on every point), and hovering or
focusing a mark shows a tooltip. Charts are drawn on the server as SVG (`app/charts.py`), with no chart library and nothing
loaded from the internet. Colours were chosen against this app's card surface and checked in `tests/test_charts.py`
(contrast 4.2:1 and 3.1:1; accent vs grey ΔE 19 normal / 17 protanopia / 21 deuteranopia).

## User performance (admin)

**Admin dashboard → User performance → "See how users are doing"** (`/admin/performance`) lists every approved student:
last active, timed tests taken, average score, accuracy, and their weakest topics (or subjects, if topics aren't assigned),
most recently active first. Click a name for a **read-only** page (`/admin/performance/<id>`) with the same numbers, charts and
table twins the student sees on their own Progress page, plus a list of their latest 50 tests and practice sessions.

- **Read-only by construction:** neither page has a form, and neither URL accepts POST/PUT/DELETE, so an admin can't edit,
  delete or re-grade an attempt from here. Passwords and hashes never appear. The trend chart doesn't link to the student's
  result pages (those belong to the student).
- **The switch:** while *User performance* is off both pages redirect to the dashboard, and students no longer see the
  "Your admin can view your practice results." notice. Turning it on restores both.
- **Audit:** opening a student's page is recorded in the audit log (`performance.view`, who and which student); the list isn't.
- Only approved students appear — not pending, rejected or deactivated accounts, and never admins.

## Leaderboards (students)

**Leaderboard** (top bar) has two parts: **The week** and **By test**.

- **By test:** one board per paper's full-length test and per paper section (History of paper X …), the same tests that are
  ranked in *Tests*. Only your **first attempt** counts — a retake, or abandoning and retaking, can never change a rank — and
  everyone on a board sat exactly the same questions. Higher score first; equal scores are split by less time taken; equal on both
  shares a rank. A test whose clock ran out is ranked on the answers you had saved. Custom tests and untimed practice are never ranked.
- **The week:** your **average percentage of the maximum marks** over the ranked full-length tests you finished that week
  (Monday 00:00 UTC to the next Monday; "Last week" is one click away). Ties go to the lower average time per test.
- **Privacy:** a board shows the **top 10 plus your own row** — never anyone below the cut-off. Once at least 5 people are on a
  board you also see "ahead of N% of the other participants". If you untick "Show me on leaderboards" in your profile, others see
  you as **Anonymous** but you are still ranked and still see your own place.
- Only approved students take part (no admins, no deactivated accounts). Your results page shows "you are #N of M" for a ranked test.
- The admin's **Leaderboard** switch turns all of this off: the pages redirect home, the top-bar link and the results-page rank disappear.

Code: `app/practice/leaderboard.py` (rules), `app/routes/leaderboard.py`, templates `leaderboard*.html` and `_board.html`.

## Reporting a problem (students and admin)

Once a question's answer is showing — after you answer it in practice, or after a test that included it is finished — a
**Report a problem with this question** section appears under the explanation (on the practice screen and on the question's own
page). Choose what looks wrong (the marked answer, the question or an option, the explanation, or something else), add details
(up to 500 characters; required for "something else") and send. It goes to the admin only and **changes nothing** — not your
score, not the question. Rules: one open report per student per question, at most 20 reports a day, never for a question in a
test you're still sitting, and only for live questions you have already met.

**Admin → Reports** (a badge in the top bar and a card on the dashboard show the open count) lists reports grouped by question:
the question, its current answer key (and a note if the key has changed since the report), each report with who sent it, and
**Resolve** / **Dismiss** buttons that close all the open reports on that question with an optional note. To actually fix a
question, use "Open in review" (or quarantine it there), then close the report. Both decisions are written to the audit log
(`report.resolve`, `report.dismiss`). Students see whether their last report on a question is open, resolved or closed.

## Daily target and streak (students)

Set a **daily target** (a number of questions, 1-500, or blank for none) in your profile. The **Today** card on your home page shows
answers so far against it. A **streak** is the number of days in a row, ending today, on which you answered at least one question
(it stays alive through today until midnight, and breaks only when a whole day passes with no answer). The streak doesn't depend
on the target, so changing your target never rewrites your history. Days are the server's local days. Every answered question
counts once on the day it was last answered — practice, mistake practice and tests alike.

## On a phone

Pages are built to work at about 400 px wide: buttons and answer options are at least 44 px tall, the top bar is compact, dashboard
cards stack in one column, and any table wider than the screen scrolls inside itself instead of widening the page. This was checked
by rendering 19 student and admin pages in a 390 px frame and confirming none scrolls sideways.

## Publishing

Students only ever see **live** questions. On a paper's review page, **Publish paper**
moves its verified questions to live. It is refused until every non-quarantined question
is confirmed and has an answer. Questions marked "depends on an image" go live only if they
have a page snapshot. **Unpublish** (or archiving the paper) hides everything from students
at once; nothing is deleted.

## Uploading a paper

1. **Upload paper** → fill in where it came from (source type, name, test, series),
   pick a marking preset (or type your own), then choose the question PDF and, if you
   have it, the answers PDF. Optionally enter subjects by question number. Uploading
   the same file twice, or a second paper for the same test, is refused unless you
   tick "Upload anyway".
2. You land on the review page, which shows progress while the paper is read
   (about a second per page). Leave it or come back later.
3. Review: compare each question with its snapshot. Answers are pre-filled from the
   answer PDF. Warnings mean "look at this one" — typical ones are garbled
   characters and match-the-following code rows that aren't a permutation of 1–4.
4. **Confirm all N without warnings** confirms every question that has an answer and
   no warnings in one click; go through the warned ones by hand.

## What the reader expects

A question paper is read one of two ways, chosen automatically (`ingest.read_questions`):

- **PDFs with real text** (most official papers, many coaching papers — if you can select and copy text in a PDF viewer,
  it has some) are read **directly from the text layer** by `app/text_extract.py`: exact, fast, no OCR, and Tesseract doesn't
  need to be installed. It removes giant watermarks, finds one or two columns by itself (the *Page layout* option on the upload
  page overrides this), drops running headers and footers, and cuts the text into questions: a question starts at the next
  expected number and only after the previous one's options (a)–(d), so numbered statements inside a question aren't mistaken
  for questions. Options are read whether one or two to a line. Every question also gets a **snapshot of exactly what was
  printed** (question and options only, never the answer). If the text can't be turned into questions the paper is read by OCR
  instead, and the review page says so.
- **Scans** (image-only PDFs) are read by OCR (`app/ocr_extract.py`), tuned for two-column bilingual pages with English on
  the left, numbers in the margin and options "(a) … (d)".

**Answers.** If the paper prints them under each question (`Answer: (c)` or `Answer-(c)`, followed by an explanation), they are
taken from the paper itself (`answer_source = inline`). A separate answer PDF (text layer, lines like `12. Ans– (c)`, see
`app/answer_key.py`) takes precedence; where the two disagree the question is flagged `answer_conflict` and nothing is chosen
silently. A question with no printed answer is flagged, and can't go live until you set one.

**What gets flagged for a second look** (text route): lists and match-the-following tables (their reading order can be
scrambled — compare with the snapshot), Hindi text (only English is read), an answer that can't be read (e.g. text printed
over text), a question with no answer, missing options, and two questions that look merged. If you set *Expected questions*
on upload and a different number is found, the review page says so.

**Known limits of the text reader.** It assumes numbered questions ("7.") in order from 1. If an option marker is missing, the
questions after it can run together (they're flagged as merged). An explanation that contains a line starting with the next
question's number ("8. …") can be mistaken for question 8. Tables inside a question come out row by row, not always in the
printed order.

## Answer keys (admin)

Every paper's review page has an **Answer key** button (`/review/<id>/key`) and a summary of the answers it has now: how many
questions have one, where they came from (printed under each question, a key file, a pasted key, the key at the end of the paper, AI-supplied
JSON, or typed by hand), the key's source and version, and warnings.

**Adding a key** — three ways, one at a time: **paste** it, **upload** a key file (a PDF with a real text layer, or `.txt`), or tick
**look for an "ANSWER KEY" block at the end of the question PDF**. Understood: `1-b 2-d`, `1. (b)`, `Q1: B`, `1) b`, `1 b 2 d`, one row per
question, `12. Ans– (c)` lines with explanations, and keys with one column (or one "Series A / Series B" block) per booklet series — the paper's
series picks the column, and you are asked to set it if it's missing. **Only the letters a–d are answers**; a key written 1–4 is refused, a
question given two different letters is an error, and a scanned key PDF can't be read (paste its text instead).

**Preview first.** Nothing changes until you apply. The preview shows the detected format, how many answers were found against how many
questions the paper has, which numbers have no answer or don't exist in the paper, the letter split (a warning when more than half of a key of
10+ answers is one letter), which answers are new, already the same, or would change, and any explanation that states a different answer.

**Apply** needs a **key source** (e.g. "institute answer sheet", "UPSC final key") and optionally a version; both are stored on the paper and in
the audit log (`key.apply`, with counts and the file's hash). By default it only **fills** questions that have no answer; replacing an existing
answer needs an explicit tick. Changing a confirmed (verified or live) question's answer sends it back to review, and every changed question keeps
its old version in its history. Explanations in a key file only fill questions that have none and stay *unverified*. A key block found at the end of
the question PDF is only mentioned at upload ("An answer key block was found…") — it is never applied by itself.

**Explanation versus key.** If an explanation states a different answer outright ("the correct answer is (c)", "option (c) is correct") from the
key, the question is flagged `explanation_mismatch`. It only flags; it never changes an answer, and it clears when the answer and explanation agree.

## Review screen and gates (admin)

**On the review page** (`/review/<id>`):
- **Picture beside the form.** Each question's printed snapshot (or, for JSON imports, the attached PDF's page) sits to the left of the form on a wide
  screen and above it on a narrow one. Answers are pre-filled from the key; nothing is confirmed by looking — you press **Confirm question**.
- **Extracted X of Y.** Y is the "Expected questions" you set on the paper (or the highest number seen if you didn't), with the missing numbers listed.
- **Warnings first.** "To confirm" lists flagged questions first (switch to question order with the link beside it).
- **Read pages again.** Enter a page range and only those pages are re-read. Confirmed, verified, live and hand-edited questions are never touched; questions
  still waiting for review on those pages are replaced (the old text is kept in their history, and an answer they already had is kept); numbers the first
  reading missed are added. A summary appears ("N replaced, M added, K left alone"). If the re-reading fails nothing changes.

**Sample audit** (`/review/<id>/audit`). "Confirm" is a claim that you compared a question with the printed original, so a random sample of the questions
you confirmed *without editing them* is checked again: 10% of them, at least 5 (all if fewer). Mark each **Matches the original** or **Doesn't match**.
A wrong one goes straight back to review. **3 or more wrong fails the audit**: every question confirmed without an edit goes back to review for a second pass,
and a new audit is needed afterwards. Fewer than 3 wrong passes it. Confirming, reopening, or changing an answer later makes a passed audit stale.
A paper with fewer than 5 unedited confirmed questions needs no audit. (Questions with a version history — edited by hand — aren't in the pool.)

**Publish gate.** Publishing is blocked while any question is unconfirmed, lacks an answer, is confirmed but still carries warnings nobody looked at again,
or while the audit is missing, running or failed. The reasons are listed above the Publish button. Publish moves verified → live; Unpublish reverses it.
Audit and re-run steps are in the audit log (`sample_audit.*`, `paper.rerun_pages`).

## Subjects (admin)

The subject list is fixed (Polity, History, Geography, Economy, Environment, Science & Tech, Current Affairs, CSAT, Other) — nothing creates a new
label by itself. Every question has a subject dropdown; on the review page the **Subjects** box adds three faster ways:

- **Bulk assign.** Tick questions (each card has a checkbox, or "Select all N shown"), choose a subject, **Assign to ticked**. The **No subject** filter
  lists what is still unfiled. Each change keeps the old version in the question's history; a topic that belongs to another subject is cleared.
- **Range templates, saved per series.** Type `1-30 History, 31-60 Geography` (also on the upload form), and give it a name and a series (A–D, or any) to save it. Later,
  pick the template instead of typing. A series-A template is refused on a series-B paper; typed ranges always win over a chosen template. Saving under an
  existing name and series replaces it; **Delete** removes a template but never undoes subjects already set.
- **Keyword suggestions.** Every imported question with no subject gets a *suggested* subject from keywords in its wording (a phrase such as "Lok Sabha" counts double; a
  question that fits two subjects equally gets none). The suggestion is stored separately and shown as **Use Polity** beside the question; the subject box itself is
  never pre-filled. Accept one, accept the ticked ones, or **Accept all N suggestions**. **Suggest subjects from keywords** works them out again.

Filing a question (subject, topic, difficulty, bulk changes, accepted suggestions) does **not** count as an edit for the sample audit; changing its text, options, answer or
image does.

## Duplicates (admin)

The same question often turns up in more than one paper. **Duplicates** in the admin menu (`/admin/duplicates`) lists the pairs the app has found. It looks
after every PDF read, JSON import, re-read of pages and edit of a question's wording (and **Look again** does it on demand). Two kinds:

- **Identical** — the same words and the same four options, ignoring case, punctuation, spacing and the *order* of the options.
- **Very similar** — the wording is at least 90% alike (rapidfuzz) *and* the options are alike too, for a reworded or slightly mis-read copy. The same stem with different
  options is a different question and isn't raised. Very short questions are only compared exactly.

Archived papers and quarantined questions aren't compared. **Nothing is decided for you.** For each pair, side by side with its paper, status and answer (a red warning appears
if the two disagree on the answer):

- **Keep both** — they're different; the pair is remembered and never raised again.
- **Keep older, merge newer** / **Keep newer, merge older** — the copy you drop goes to **Quarantine** with a reason (never deleted) and is recorded on the kept question
  as "Also appeared in: <paper · year · Q#>", which shows on the review page. Restoring it from Quarantine undoes the merge. Answers already given to the dropped copy stay
  in the students' history; a live copy that is merged away disappears for students at once, so confirm and publish the one you kept.

On the JSON import report, tick **Skip the N questions that duplicate ones already imported** to leave the exact duplicates out; unticked, they are imported and listed for you here.
The review page shows how many questions in a paper may be duplicates.

## Suspicious answers and conflicting copies (admin)

**Suspicious answers** (`/admin/suspicious`, under Quality; the menu shows a count). Once students have taken tests, a wrong answer key shows itself: the strongest students
keep choosing a different option. **Analyse now** looks at every *live* question:

- Only finished tests and practice sessions of students count (never yours), and only each student's **first** answer to a question.
- A student counts once they have answered 20+ questions; their strength is their accuracy on *all the other* questions (so the question being judged can't decide who
  is strong). A question needs 12+ such students; the high scorers are the top 25% of them (at least 5).
- It is raised when **more than half of the high scorers chose one option that isn't the key** and it beats the key's count by at least 2 students.

Each card shows the question, the key, and the split of answers among high scorers and among everyone, plus any open student reports. **The key is right** dismisses it (remembered
against that key: it comes back only if the answer is changed and students still disagree). **Check it: send back to review** takes it away from students and re-opens it (if you
confirm it again with the same key it counts as dismissed). Nothing is edited, hidden or re-graded by the detector itself. It stays quiet on a small class — a tricky question can also
fool strong students, so treat a card as a reason to look, not proof.

**Conflicting copies.** When two copies of a question (from the Duplicates list, identical or very similar) have **different answers**, both get a `source_conflict` warning: "Another
copy of this question (in another paper) has a different answer". The comparison is by the answer's *text*, not its letter, because the same question is often printed with its options
in another order. A confirmed question that newly gets the warning must be confirmed again (it counts as unacknowledged, so it blocks publishing until then). The warning clears when
the answers agree, when a copy is merged away or quarantined, and returns if a merged copy is restored. **Keep both** does not hide a real conflict.

## Importing from a spreadsheet, document or pictures (admin)

**Import a file** in the admin menu (`/admin/import/file`) takes one of three kinds of file. As with JSON, you first get a report and nothing is saved until you press Import; everything
lands as **needs review**, with no original page to compare against, so check it against your own source. A file that was already imported is refused unless you tick "Import anyway";
a spreadsheet or document can also be added to an existing paper (only question numbers it doesn't have yet are filled).

- **CSV / XLSX** — one question per row, the first row being headers. The app guesses which column is which from the header names ("Q No", "Option A", "Correct Answer" …), shows
  the first rows, and lets you change every choice and press **Check this choice**. **Your choice is remembered** for the next file with the same headers (in any order): the page says
  "Using the choice you made last time". Answers must be the letters A–D (also `(b)`, `Option B`); **1–4 or anything else is left blank and flagged, never guessed**. Any row with a
  missing question, missing option, unreadable or repeated number is named and blocks the import. Subjects outside the fixed list are left blank; topics are used only when they already
  exist. XLSX files with several sheets let you choose the sheet. CSV can be UTF-8 or Windows-1252, comma / semicolon / tab separated.
- **DOCX** — questions typed as text: `1.` `2.` … with options `(a)`–`(d)`, optionally `Answer: (b)` under each question or an "ANSWER KEY" block at the end (read with the same rules as text PDFs).
  Tables are read row by row. **Word's automatic numbering isn't stored in the text**, so numbered questions may be missing — the report warns about it: type the numbers instead.
  Pictures inside the document aren't imported (a warning says how many).
- **Pictures** (.png .jpg .jpeg .webp .bmp .tif) — one or more page photos or screenshots, in the order chosen, are joined into a PDF and read by OCR like a scanned paper, in the
  background (Tesseract must be installed). The reader keeps a picture of each question for the review screen.

Imported questions carry their source (`csv`, `xlsx`, `docx`), answers are marked "an imported file" (`file`) and explanations "Unverified (imported from a file)". Possible duplicates and
subject hints are worked out afterwards, like any other import.

## Reading in Hindi (students)

**Profile → Question language**: English (the default, so nothing changes for anyone until they choose), Hindi, or Both. It applies to practice, timed tests, results, revision, bookmarks and a question's own page:

- **English** — the English version; a question that exists only in Hindi shows its Hindi with the note "English not available for this question".
- **Hindi** — the Hindi version; a question with no Hindi shows its English with "Hindi not available for this question".
- **Both** — English and Hindi together: the question text stacked (English above Hindi), and inside each option both languages under the one radio button. A question with only one language shows just that one, with no gap or placeholder.
- **Explanations** follow the same rules on their own (a Hindi question can have an English-only explanation, and the reverse) and each carries its own label: *Verified* only if the admin ticked that language's "I have checked this explanation", otherwise *AI-supplied, unverified*.
- Lists (results, revision, bookmarks) show one line in the student's language, falling back to the other.

**Quick switch.** Practice and test screens (and a question's own page) show an **English / हिन्दी / Both** switch whenever anything in the session has Hindi — or the student has chosen Hindi/Both, so they can always switch back. Every language is already in the page and the switch only changes which shows, in the browser: **no reload, and it never changes, clears or resubmits the answer, timer or position**; the choice is saved in the background for the next page. It is a compact segmented control (finger-sized on phones, full width under the question header), newly shown text fades in briefly, Devanagari is set a little larger, "Both" separates the two versions with a light divider, the arrow keys move between the choices, and screen readers are told "Showing Hindi" and so on. Without JavaScript the same buttons post a form and return to the page. Nothing new is revealed before the student answers, and Hindi text of a question that is no longer live disappears like any other.

**Report a problem** asks "Which language version?" (English / Hindi / Both) for a question that has Hindi, defaulting to what the student is reading; the admin's reports queue shows it. Reports on English-only questions are unchanged.

## Importing questions from JSON (admin)

For papers another AI has read for you: **Import JSON** in the top bar (`/admin/import/json`).

1. **Copy prompt** (or download it) and give it to the other AI together with the paper PDF. The prompt is the fixed text from the
   import spec; its subject list comes from the app's own subjects. It asks the AI for both languages when the paper has both and for 25 questions per reply (you say "next" for the rest — the app itself has no limit). **Download template** gives
   the format (`schema_version 2`, which only adds the optional Hindi fields to version 1, so version 1 files still import unchanged; each question needs `number` and at least one language's
   question text and `options {a,b,c,d}`; everything else — `correct_answer`, `explanation`, `subject`, `topic`, `has_image`, `page`, `uncertain`, and the Hindi fields — may be null or missing).
2. **Validate** with one file per reply, and/or pasted text, and optionally the question PDF. Nothing is saved. The report lists, with the
   question number each belongs to: invalid JSON (line, column, nearest question), options that aren't exactly a–d, bad answers, missing or
   duplicate numbers against the expected total, unknown subjects, very short questions, `[IMAGE]` without `has_image`, questions that look
   like ones already in the system, and overlaps or conflicts between parts. A closing "Covered questions X to Y." line, a ``` fence and
   `\n` written as literal text are all understood.
3. **Import** (only when there are no errors). It makes a database backup first, creates a new paper — or adds to an existing one — and
   writes an audit entry.

## Hindi through JSON (admin)

A question can be in English only, Hindi only, or both, and Hindi only ever comes from JSON (a PDF upload stays English-only, as before). The JSON fields are `question_hi`, `options_hi {a,b,c,d}` and
`explanation_hi`; in the database `question_hi`, `option_a_hi` … `option_d_hi`, `explanation_hi` and `explanation_hi_status` (its own unverified/verified, apart from the English explanation). A Hindi-only question keeps empty English
text (those columns can't be null).

What the validation report checks (still "validate only"): at least one language must have question text **and** four options; each language given must have exactly a, b, c, d (Devanagari labels क ख ग घ are
an error — the prompt asks the AI to map them to a–d); a language with its text but not its options, or the reverse, is importable but flagged **`language_incomplete`**; a script check flags **`language_swapped`**
when the Hindi fields are less than 60% Devanagari or the English fields more than 20% (only warned about, never blocked). The report counts questions in English and Hindi / English only / Hindi only. Merging parts
compares each language only when both entries have it, so a second part can add the Hindi version to an English question; English-only versus Hindi-only for one number can't be compared and is an error.

Duplicate detection uses the English text, or the Hindi text when a question has no English, keeping Devanagari letters (English hashes are unchanged). A Hindi question is only matched with another Hindi-only question.
**Reviewing Hindi.** On the review page a question that has both languages shows English and Hindi **side by side** (stacked on a narrow screen or beside a page picture), both editable; a Hindi-only question shows only the Hindi boxes with a "No English version" note; an English-only question looks as it always did. **Confirm question confirms both languages together** (one status). A question must keep text in at least one language. Each language's explanation has its **own** "I have checked this explanation" tick (English and Hindi are verified separately). Hindi edits are in the question's history and undo, count as content edits for the sample audit, and send a live question back to review like any other edit. The review header counts "N in English and Hindi, M Hindi only".

**Language warnings** are recomputed from what is stored, after every import and every edit (not a translation check — structure and script only): `language_incomplete` (one language has text without options or the reverse), `language_swapped` (script check as above) and **`language_mismatch`** (both languages present but a different number of options, or a different number of statement lines in the question). They behave like any other warning: confirming a warned question acknowledges it, and a confirmed question that an edit newly warns must be confirmed again before the paper can be published. The duplicates, quarantine, sample-audit, reports and suspicious-answer pages show Hindi text too (a Hindi-only question falls back to its Hindi).

**Parts** are merged by question number: identical repeats are merged; the same number with different text or answers is an error unless
you tick "let the later part win". **Adding to an existing paper** skips numbers it already has; questions still waiting for review can be
replaced on request (the old text stays in their history); confirmed and live questions are never touched.

**Trust.** JSON from another AI is never trusted: every question is saved as *needs review*, source `ai_json`, marked "AI-supplied,
unverified" on the review page and in the explanation label students see. Answers it supplies get an `ai_answer` warning. **Confirm all** can include answered AI-supplied questions and flagged questions after an explicit warning; questions without a valid answer remain in review. Check AI answers against the paper's real key. Nothing reaches a student until you confirm and publish. Topics in the JSON are used only if they already exist under that subject; subjects outside the fixed list are left blank.

**Attached PDF.** If you give the PDF, each question's `page` shows the original page in the review screen, for you only (`page{N}.jpg`; students
get a 404). Questions with `has_image: true` have no cropped snapshot, so — as for any image question without one — they stay out of tests
until that exists.

There is no limit on parts, questions or file size. Samples to try: `samples/json_sample_part1.json` and `json_sample_part2.json`
(they overlap on Q5, include an unknown subject, an image question, an uncertain one and two without answers).

## Question status

`draft → needs_review → verified → live`, plus `quarantined`. Imported questions start
as `needs_review` and become `verified` only when you click Confirm. Only `live`
questions may ever be used in a test (`app/practice/pool.py: live_questions()` is the one
place that decides this). Editing a live question sends it back to review.

## Safety net (Admin menu)

- **Audit log** — every import, edit, confirmation, quarantine, archive, backup and
  account decision, with who and when. Never edited or deleted.
- **History** — a copy of a question is saved before every change; open a question's
  *History* link to restore any earlier version (it then needs confirming again).
- **Quarantine** — takes a bad question out of circulation with a reason. Nothing is
  deleted; restoring sends it back to review.
- **Archive** — papers are archived, never deleted. Files and questions stay.
- **Backups** — the database is copied to `data/backups/` before every import (newest
  20 kept), and you can make and download one any time. The PDFs and page images live
  in `data/`, so copy that whole folder for a complete backup.

## Notes

- OCR is good but not perfect: expect the odd wrong character, especially in
  match-the-following tables. That's what the snapshots and warnings are for.
- A paper being read when the app restarts is marked "failed"; archive it and upload again.

## Tests

```powershell
.\venv\Scripts\pip install -r requirements-dev.txt
.\venv\Scripts\python.exe -m pytest              # fast suite, a few seconds
.\venv\Scripts\python.exe -m pytest -m slow      # reads the real paper in PYQ/test 1 with OCR (~1 min)
```

Tests run against a throwaway data folder (`UPSC_DATA_DIR`), never your real database.
