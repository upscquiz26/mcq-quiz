"""
Data model for the whole app (built now so later milestones — test-taking,
mistake dashboard, spaced-repetition sectional pulls — just add screens on
top of this, instead of needing a schema rewrite).

Subject      -> standard GS Paper 1 subjects (Polity, History, ...), seeded on first run.
Topic        -> grows organically. AI suggests one during review; accepting it adds
                it to this subject's list for next time.
Paper        -> one uploaded PDF worth of questions (a full-length paper, or a
                sectional set).
Question     -> one MCQ: text, 4 options, correct answer, tags, optional image.
Attempt      -> one time you sat a paper (full-length or sectional). Attempts are
                NEVER overwritten — retaking a paper creates a new Attempt, so you
                can see "62% in June -> 78% in September" on the same paper.
Response     -> your answer to one question within one Attempt. Carries the
                confidence rating (sure/guessed/no_idea) that everything else
                (mistake categorization, dashboard) is built on.
"""
from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Text, Boolean, Float, Date, DateTime, ForeignKey, Enum, UniqueConstraint
)
from sqlalchemy.orm import relationship
import enum

from app.database import Base


class ExamType(str, enum.Enum):
    full_length = "full_length"
    sectional = "sectional"


class Confidence(str, enum.Enum):
    sure = "sure"
    guessed = "guessed"
    no_idea = "no_idea"
    skipped = "skipped"  # not attempted at all


class MistakeReason(str, enum.Enum):
    conceptual_confusion = "conceptual_confusion"  # auto: sure + wrong
    knowledge_gap = "knowledge_gap"                # auto: no_idea + wrong
    guess_miss = "guess_miss"                       # auto: guessed + wrong
    careless = "careless"                            # auto: sure + wrong + answered very quickly; or manual
    time_pressure = "time_pressure"                  # auto: sure + wrong + took very long
    unset = "unset"


class QStatus:
    """Lifecycle of a question. Tests may only ever use LIVE questions.

    draft -> needs_review -> verified -> live      (quarantined can be entered from any state
                                                    and is only left by an explicit restore)
    """
    DRAFT = "draft"
    NEEDS_REVIEW = "needs_review"
    VERIFIED = "verified"
    LIVE = "live"
    QUARANTINED = "quarantined"
    ALL = (DRAFT, NEEDS_REVIEW, VERIFIED, LIVE, QUARANTINED)


class SourceType:
    OFFICIAL_PYQ = "official_pyq"
    COACHING_TEST = "coaching_test"
    ALL = (OFFICIAL_PYQ, COACHING_TEST)
    LABELS = {OFFICIAL_PYQ: "Official PYQ", COACHING_TEST: "Coaching test"}


class UserStatus(str, enum.Enum):
    pending = "pending"      # requested an account, waiting for the admin
    approved = "approved"
    rejected = "rejected"
    deactivated = "deactivated"   # was approved; the admin switched the account off (can be switched back on)


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    username = Column(String, unique=True, nullable=False)  # always stored lowercase
    password_hash = Column(String, nullable=False)
    is_admin = Column(Boolean, default=False, nullable=False)
    status = Column(Enum(UserStatus), nullable=False, default=UserStatus.pending)
    created_at = Column(DateTime, default=datetime.utcnow)
    decided_at = Column(DateTime, nullable=True)
    decided_by = Column(Integer, ForeignKey("users.id"), nullable=True)

    display_name = Column(String, nullable=True)                 # shown on leaderboards; defaults to the username
    show_on_leaderboard = Column(Boolean, default=True, nullable=False)
    must_change_password = Column(Boolean, default=False, nullable=False)  # set by an admin reset
    password_changed_at = Column(DateTime, nullable=True)
    # Stored in the session cookie and compared on every request. Bumping it signs the user out everywhere.
    session_version = Column(Integer, default=0, nullable=False)
    last_active_at = Column(DateTime, nullable=True)
    daily_target = Column(Integer, nullable=True)
    language = Column(String, nullable=False, default="en", server_default="en")   # which language a student reads questions in: en | hi | both


class AppSetting(Base):
    """Small admin-controlled switches, e.g. leaderboard_enabled. See app/settings.py."""
    __tablename__ = "app_settings"
    key = Column(String, primary_key=True)
    value = Column(String, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow)
    updated_by = Column(Integer, ForeignKey("users.id"), nullable=True)


class LoginFailure(Base):
    """One row per failed login (or failed current-password check). Drives the temporary lockout.
    The typed username is stored only as a short hash, because people sometimes type a password there."""
    __tablename__ = "login_failures"
    id = Column(Integer, primary_key=True)
    key = Column(String, nullable=False, index=True)   # hash of the lowercased username (or "pw:<user id>")
    ip = Column(String, nullable=True, index=True)
    at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)


class Subject(Base):
    __tablename__ = "subjects"
    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True, nullable=False)
    is_active = Column(Boolean, default=True)

    topics = relationship("Topic", back_populates="subject")
    # Question also points here via suggested_subject_id, so say which foreign key this relationship uses.
    questions = relationship("Question", back_populates="subject", foreign_keys="Question.subject_id")


class Topic(Base):
    __tablename__ = "topics"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    subject_id = Column(Integer, ForeignKey("subjects.id"), nullable=False)

    subject = relationship("Subject", back_populates="topics")
    questions = relationship("Question", back_populates="topic")


class SubjectTemplate(Base):
    """A saved set of subject ranges ("1-30 History, 31-60 Geography"), so the same booklet layout isn't retyped for every paper.
    `series` (A-D) ties it to a booklet series; empty means it fits any series."""
    __tablename__ = "subject_templates"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    series = Column(String, nullable=True)
    ranges_text = Column(Text, nullable=False)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class ImportMapping(Base):
    """Which spreadsheet column holds which question field, remembered for the next file with the same headers (CSV/XLSX import)."""
    __tablename__ = "import_mappings"
    id = Column(Integer, primary_key=True)
    signature = Column(String, nullable=False, unique=True, index=True)   # the file's normalised header names, hashed
    headers_json = Column(Text, nullable=False)                           # the headers as they were, for showing to the admin
    mapping_json = Column(Text, nullable=False)                           # {field: header name}
    uses = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class Paper(Base):
    __tablename__ = "papers"
    id = Column(Integer, primary_key=True)
    title = Column(String, nullable=False)          # e.g. "UPPCS Prelims 2023 GS Paper 1"
    year = Column(Integer, nullable=True)
    exam_type = Column(Enum(ExamType), nullable=False)
    source_pdf_path = Column(String, nullable=True)
    answer_pdf_path = Column(String, nullable=True)  # optional answer key + explanations PDF
    is_current_affairs = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Extraction runs in the background: "processing" -> "ready" | "failed"
    status = Column(String, default="ready")
    status_message = Column(Text, nullable=True)
    pages_done = Column(Integer, default=0)
    pages_total = Column(Integer, default=0)

    # Where the paper came from
    source_type = Column(String, nullable=True)      # SourceType.*
    source_name = Column(String, nullable=True)      # "UPSC", or the institute's name
    test_name = Column(String, nullable=True)
    test_number = Column(String, nullable=True)
    series = Column(String, nullable=True)           # booklet series A-D for official papers
    layout = Column(String, nullable=True)           # page layout the reader should assume

    # How it is marked (defaults come from a preset, always editable)
    expected_total = Column(Integer, nullable=True)  # number of questions the paper should have
    duration_minutes = Column(Integer, nullable=True)  # time allowed for a full-length test on this paper
    marks_per_question = Column(Float, nullable=True)
    negative_fraction = Column(Float, nullable=True)  # e.g. 0.3333 = one third of the marks

    # Answer key provenance
    key_source = Column(String, nullable=True)
    key_version = Column(String, nullable=True)

    # Duplicate-upload detection
    file_hash = Column(String, nullable=True)        # sha256 of the question PDF
    answer_file_hash = Column(String, nullable=True)

    # Publishing and archiving (papers are archived, never deleted)
    publish_status = Column(String, default="draft")  # draft | published
    published_at = Column(DateTime, nullable=True)
    published_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    archived_at = Column(DateTime, nullable=True)
    audit_state = Column(String, default="none")     # none | pending | passed | failed
    audit_round = Column(Integer, default=0)

    questions = relationship("Question", back_populates="paper", cascade="all, delete-orphan")
    attempts = relationship("Attempt", back_populates="paper", cascade="all, delete-orphan")


class Question(Base):
    __tablename__ = "questions"
    id = Column(Integer, primary_key=True)
    paper_id = Column(Integer, ForeignKey("papers.id"), nullable=False)
    question_number = Column(Integer, nullable=True)
    text = Column(Text, nullable=False)
    option_a = Column(Text, nullable=False)
    option_b = Column(Text, nullable=False)
    option_c = Column(Text, nullable=False)
    option_d = Column(Text, nullable=False)
    correct_answer = Column(String, nullable=True)  # "A" / "B" / "C" / "D", filled during review

    subject_id = Column(Integer, ForeignKey("subjects.id"), nullable=True)
    topic_id = Column(Integer, ForeignKey("topics.id"), nullable=True)
    suggested_topic = Column(String, nullable=True)  # optional prefill for the topic box on review

    explanation = Column(Text, nullable=True)         # from the answer-key PDF, if one was uploaded
    source_image_path = Column(String, nullable=True)  # snapshot of the question as printed, for checking OCR
    ocr_flags = Column(String, nullable=True)         # comma-separated reasons this question needs a careful look

    # Hindi version, only ever from a JSON import (never read from a PDF). Any of the two languages may be missing on a question, but not both:
    # a Hindi-only question keeps "" in text / option_a-d above (those columns can't be null).
    question_hi = Column(Text, nullable=True)
    option_a_hi = Column(Text, nullable=True)
    option_b_hi = Column(Text, nullable=True)
    option_c_hi = Column(Text, nullable=True)
    option_d_hi = Column(Text, nullable=True)
    explanation_hi = Column(Text, nullable=True)
    explanation_hi_status = Column(String, nullable=True)  # unverified | verified, kept apart from the English explanation's status

    has_image = Column(Boolean, default=False)
    image_path = Column(String, nullable=True)

    difficulty = Column(String, nullable=True)  # "easy" / "medium" / "hard" / "tricky" — self-tagged later
    needs_review = Column(Boolean, default=True)  # LEGACY: superseded by `status`; kept in the table, no longer read

    status = Column(String, nullable=False, default=QStatus.NEEDS_REVIEW, index=True)  # QStatus.*
    source = Column(String, nullable=True)            # pdf_text | pdf_ocr | ai_json | manual
    page_number = Column(Integer, nullable=True)      # PDF page where the question starts
    extraction_method = Column(String, nullable=True)  # text | ocr
    ocr_quality = Column(Float, nullable=True)        # 0..1, lower means more garbled
    norm_hash = Column(String, nullable=True, index=True)  # normalised-text hash for duplicate detection
    answer_source = Column(String, nullable=True)     # key_pdf | pasted | json | inline | manual
    explanation_status = Column(String, nullable=True)  # unverified | verified
    explanation_says = Column(String, nullable=True)  # answer letter the explanation itself states, if any
    uncertain = Column(Boolean, default=False)        # extractor/AI said it wasn't sure about this one
    suggested_subject_id = Column(Integer, ForeignKey("subjects.id"), nullable=True)  # a suggestion, not a decision

    quarantine_reason = Column(Text, nullable=True)
    quarantined_at = Column(DateTime, nullable=True)
    reviewed_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    reviewed_at = Column(DateTime, nullable=True)
    flags_acknowledged = Column(Boolean, default=False)  # the reviewer confirmed it despite its warnings
    audit_pick = Column(Boolean, default=False)       # selected for the post-review sample audit
    audit_result = Column(String, nullable=True)      # ok | wrong

    paper = relationship("Paper", back_populates="questions")
    subject = relationship("Subject", back_populates="questions", foreign_keys=[subject_id])
    topic = relationship("Topic", back_populates="questions")
    responses = relationship("Response", back_populates="question")


class QuestionDuplicate(Base):
    """Two questions that look like the same question. `question_id` is the newer one, `other_id` the older.
    kind: exact (same normalised text and options) | near (very similar). status: open | kept_both | merged."""
    __tablename__ = "question_duplicates"
    id = Column(Integer, primary_key=True)
    question_id = Column(Integer, ForeignKey("questions.id"), nullable=False, index=True)
    other_id = Column(Integer, ForeignKey("questions.id"), nullable=False, index=True)
    kind = Column(String, nullable=False)
    score = Column(Integer, nullable=False, default=100)
    status = Column(String, nullable=False, default="open", index=True)
    merged_into = Column(Integer, ForeignKey("questions.id"), nullable=True)
    decided_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    decided_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    __table_args__ = (UniqueConstraint("question_id", "other_id", name="uq_question_duplicate_pair"),)


class QuestionSource(Base):
    """Where else a question appeared. When a duplicate is merged, the question that was dropped is recorded here on the one kept,
    so 'also asked in …' survives even though the second copy is taken out of circulation."""
    __tablename__ = "question_sources"
    id = Column(Integer, primary_key=True)
    question_id = Column(Integer, ForeignKey("questions.id"), nullable=False, index=True)       # the question that was kept
    from_question_id = Column(Integer, ForeignKey("questions.id"), nullable=True, index=True)   # the copy that was merged into it
    paper_id = Column(Integer, ForeignKey("papers.id"), nullable=True)
    question_number = Column(Integer, nullable=True)
    label = Column(String, nullable=False)                                                     # "Paper title · 2019 · Q23", frozen when merged
    added_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    added_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class AttemptKind:
    TOPIC = "topic"            # untimed practice with immediate feedback
    SECTIONAL = "sectional"    # timed, one subject
    FULL = "full"              # timed, a whole paper
    MISTAKE = "mistake"        # untimed, questions the user got wrong or guessed
    ALL = (TOPIC, SECTIONAL, FULL, MISTAKE)
    TIMED = (SECTIONAL, FULL)
    LABELS = {TOPIC: "Topic practice", SECTIONAL: "Sectional test", FULL: "Full-length test",
              MISTAKE: "Mistake practice"}


class AttemptStatus:
    IN_PROGRESS = "in_progress"
    SUBMITTED = "submitted"
    EXPIRED = "expired"        # a timed test whose clock ran out; graded automatically


class Attempt(Base):
    """One sitting of a practice session or test, by one user. Never overwritten: a retake is a new Attempt."""
    __tablename__ = "attempts"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    paper_id = Column(Integer, ForeignKey("papers.id"), nullable=True)   # null when the questions come from several papers
    mode = Column(Enum(ExamType), nullable=True)                         # legacy; `kind` is what matters now
    kind = Column(String, nullable=False, default=AttemptKind.TOPIC)
    status = Column(String, nullable=False, default=AttemptStatus.IN_PROGRESS, index=True)
    started_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)                       # when it was submitted (or expired)

    subject_id = Column(Integer, ForeignKey("subjects.id"), nullable=True)
    filters_json = Column(Text, nullable=True)                           # what the user chose to practise
    # Only whole paper / whole paper-subject sittings can be ranked; e.g. "paper:12:full", "paper:12:subject:3".
    rank_key = Column(String, nullable=True, index=True)
    counts_for_rank = Column(Boolean, default=False, nullable=False)

    timer_strict = Column(Boolean, default=False)   # full-length: strict no-pause timer
    negative_marking = Column(Boolean, default=True)
    time_limit_minutes = Column(Integer, nullable=True)
    deadline_at = Column(DateTime, nullable=True)   # server-side end time for timed tests

    current_response_id = Column(Integer, nullable=True)   # the question on screen, for measuring time spent
    last_event_at = Column(DateTime, nullable=True)

    score = Column(Float, nullable=True)
    max_marks = Column(Float, nullable=True)
    total_questions = Column(Integer, nullable=True)
    correct_count = Column(Integer, nullable=True)
    wrong_count = Column(Integer, nullable=True)
    skipped_count = Column(Integer, nullable=True)
    time_taken_seconds = Column(Integer, nullable=True)

    paper = relationship("Paper", back_populates="attempts")
    responses = relationship("Response", back_populates="attempt", cascade="all, delete-orphan",
                             order_by="Response.position")


class Response(Base):
    __tablename__ = "responses"
    __table_args__ = (UniqueConstraint("attempt_id", "question_id", name="uq_response_attempt_question"),)
    id = Column(Integer, primary_key=True)
    attempt_id = Column(Integer, ForeignKey("attempts.id"), nullable=False)
    question_id = Column(Integer, ForeignKey("questions.id"), nullable=False)
    position = Column(Integer, nullable=False, default=1)   # 1-based order within the attempt

    selected_answer = Column(String, nullable=True)  # null if skipped
    confidence = Column(Enum(Confidence), nullable=False, default=Confidence.skipped)
    is_correct = Column(Boolean, nullable=True)
    time_spent_seconds = Column(Integer, nullable=True)
    visited = Column(Boolean, default=False, nullable=False)
    marked_for_review = Column(Boolean, default=False, nullable=False)
    answered_at = Column(DateTime, nullable=True)

    # Copied from the paper when the attempt starts, so editing a paper later never changes past results.
    marks_if_correct = Column(Float, nullable=True)
    penalty_if_wrong = Column(Float, nullable=True)
    marks_awarded = Column(Float, nullable=True)

    # Auto-inferred from confidence + is_correct at grading time; you can override.
    mistake_reason = Column(Enum(MistakeReason), default=MistakeReason.unset)
    reason_overridden = Column(Boolean, default=False)
    note = Column(Text, nullable=True)  # e.g. "confused Article 32 with Article 226" — for sure+wrong only

    attempt = relationship("Attempt", back_populates="responses")
    question = relationship("Question", back_populates="responses")


class RevisionItem(Base):
    """A question on one user's spaced-repetition schedule. See app/practice/revision.py for the rules."""
    __tablename__ = "revision_items"
    __table_args__ = (UniqueConstraint("user_id", "question_id", name="uq_revision_user_question"),)
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    question_id = Column(Integer, ForeignKey("questions.id"), nullable=False, index=True)
    stage = Column(Integer, nullable=False, default=0)          # how many scheduled reviews it has had
    correct_streak = Column(Integer, nullable=False, default=0)  # clean right answers in a row at reviews
    due_date = Column(Date, nullable=True)                       # local date; on or before today = due now
    status = Column(String, nullable=False, default="active")    # active | done
    last_result = Column(String, nullable=True)                  # wrong | right | shaky
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)
    done_at = Column(DateTime, nullable=True)


class QuestionBookmark(Base):
    __tablename__ = "question_bookmarks"
    __table_args__ = (UniqueConstraint("user_id", "question_id", name="uq_bookmark_user_question"),)
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    question_id = Column(Integer, ForeignKey("questions.id"), nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class QuestionNote(Base):
    """A student's private note on a question. Nobody else can read it (the admin's tools don't show it)."""
    __tablename__ = "question_notes"
    __table_args__ = (UniqueConstraint("user_id", "question_id", name="uq_note_user_question"),)
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    question_id = Column(Integer, ForeignKey("questions.id"), nullable=False, index=True)
    text = Column(Text, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow)


class QuestionReport(Base):
    """A student's 'this looks wrong' report on a question, waiting in the admin's queue. See app/practice/reports.py."""
    __tablename__ = "question_reports"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    question_id = Column(Integer, ForeignKey("questions.id"), nullable=False, index=True)
    kind = Column(String, nullable=False)                          # wrong_answer | wrong_text | wrong_explanation | other
    note = Column(Text, nullable=True)
    answer_at_report = Column(String, nullable=True)               # the answer key as it stood, so a later fix is visible
    language = Column(String, nullable=True)                       # which language version the report is about: en | hi | both
    status = Column(String, nullable=False, default="open", index=True)   # open | resolved | dismissed
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    resolved_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    resolved_at = Column(DateTime, nullable=True)
    resolution_note = Column(Text, nullable=True)


class AnswerSuspicion(Base):
    """A live question whose answer key most of the strongest students disagree with. See app/practice/suspicion.py.
    status: open | dismissed (the admin says the key is right) | sent_back (the admin sent it back to review)."""
    __tablename__ = "answer_suspicions"
    id = Column(Integer, primary_key=True)
    question_id = Column(Integer, ForeignKey("questions.id"), nullable=False, unique=True, index=True)
    key_answer = Column(String, nullable=False)              # the key when this was worked out
    popular_answer = Column(String, nullable=False)          # what most high scorers chose instead
    high_n = Column(Integer, nullable=False)                 # how many high scorers answered it
    high_counts_json = Column(Text, nullable=False)          # {"A": n, ...} among high scorers
    all_n = Column(Integer, nullable=False)
    all_counts_json = Column(Text, nullable=False)           # {"A": n, ...} among everyone counted
    status = Column(String, nullable=False, default="open", index=True)
    handled_key = Column(String, nullable=True)              # the key at the time the admin dismissed it / sent it back
    handled_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    handled_at = Column(DateTime, nullable=True)
    first_seen_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    computed_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class AuditLog(Base):
    """Who did what, when. Append-only; rows are never edited or deleted by the app."""
    __tablename__ = "audit_log"
    id = Column(Integer, primary_key=True)
    at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)   # null for background work
    username = Column(String, nullable=True)  # copied, so the log still reads well if the user is removed
    action = Column(String, nullable=False, index=True)                # e.g. question.edit, paper.archive
    entity_type = Column(String, nullable=True)
    entity_id = Column(Integer, nullable=True)
    paper_id = Column(Integer, nullable=True, index=True)
    detail_json = Column(Text, nullable=True)                          # scrubbed of anything secret-looking


class QuestionVersion(Base):
    """A snapshot of a question taken just BEFORE a change, so any edit can be undone."""
    __tablename__ = "question_versions"
    id = Column(Integer, primary_key=True)
    question_id = Column(Integer, ForeignKey("questions.id"), nullable=False, index=True)
    version_no = Column(Integer, nullable=False)
    snapshot_json = Column(Text, nullable=False)
    reason = Column(String, nullable=True)
    changed_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    changed_at = Column(DateTime, default=datetime.utcnow, nullable=False)


STANDARD_SUBJECTS = [
    "Polity", "History", "Geography", "Economy",
    "Environment", "Science & Tech", "Current Affairs", "CSAT", "Other",
]


def seed_subjects(db):
    """Run on every startup: make sure the fixed subject list exists. Never creates any other label."""
    existing = {s.name for s in db.query(Subject).all()}
    for name in STANDARD_SUBJECTS:
        if name not in existing:
            db.add(Subject(name=name))
    db.commit()
