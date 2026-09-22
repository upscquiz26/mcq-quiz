"""
Question import from JSON — usually produced by another AI that was shown the paper.

That JSON is NOT trusted: it can be misread, rephrased, or wrong about the answer. So this module

  * parses and validates it completely before anything is saved (the "validate only" report), listing every problem with the
    question number it belongs to instead of stopping at the first;
  * merges several parts ("questions 1-25", "26-50", ...) by question number, detecting overlaps and real conflicts;
  * saves every question as `needs_review`, source `ai_json`, so it goes through the same review screen and publish gates as a
    PDF import and can't reach a student until an admin confirms it.

Format (schema_version 2; version 1 files still import unchanged) — see TEMPLATE_TEXT. Required per question: number, and at least one
language with its question text and options {a,b,c,d}: English (question, options) and/or Hindi (question_hi, options_hi).
Everything else may be null or missing. A question with only one language leaves the other language's fields null.
"""
import hashlib
import json
import re
from dataclasses import dataclass, field

from app import language, models, versions

SCHEMA_VERSION = 2                       # what the template asks for
SCHEMA_VERSIONS = (1, 2)                 # what is accepted: 2 only adds the optional Hindi fields to 1
# There is deliberately no limit on how many parts, questions or bytes an import may have: only the admin can import.
MIN_QUESTION_CHARS = 15
LONG_OPTION_CHARS = 300

COVERED = re.compile(r"Covered\s+questions?\s+(\d+)\s*(?:to|-|–|—)\s*(\d+)", re.I)
NUMBER_KEY = re.compile(r'"number"\s*:\s*(\d+)')

# The template shown to the user and embedded in the AI prompt. Kept as text so it is exactly what the spec shows.
TEMPLATE_TEXT = '''{
  "schema_version": 2,
  "paper": {
    "title": "Prelims GS Paper I 2023",
    "source_type": "official_pyq",
    "source_name": "UPSC",
    "year": 2023,
    "series": "A",
    "expected_total": 100
  },
  "questions": [
    {
      "number": 1,
      "question": "Consider the following statements:\\n1. Statement one\\n2. Statement two\\nWhich of the above is/are correct?",
      "options": { "a": "1 only", "b": "2 only", "c": "Both 1 and 2", "d": "Neither 1 nor 2" },
      "question_hi": "निम्नलिखित कथनों पर विचार कीजिए:\\n1. कथन एक\\n2. कथन दो\\nउपर्युक्त में से कौन-सा/से सही है/हैं?",
      "options_hi": { "a": "केवल 1", "b": "केवल 2", "c": "1 और 2 दोनों", "d": "न तो 1 और न ही 2" },
      "correct_answer": "c",
      "explanation": null,
      "explanation_hi": null,
      "subject": "Polity",
      "topic": null,
      "has_image": false,
      "page": 3,
      "uncertain": false
    }
  ]
}'''

PROMPT_TEXT = '''Convert the attached question paper into JSON exactly in the format below.

RULES
- Copy question and option text exactly as written. Do not rephrase, fix, or improve.
- Extract BOTH languages when the document has both. Put English in question and
  options. Put Hindi in question_hi and options_hi, in Devanagari (Unicode).
- Never translate. Never transliterate. If a question exists in only one
  language in the document, leave the other language's fields null. Do not create
  a Hindi or English version yourself.
- Do not correct Hindi spelling, matras or grammar. Copy it as printed.
- Never guess an answer. Use correct_answer only if the answer key or the
  document itself states it. Otherwise set it to null.
- Same for explanation and explanation_hi: copy if present, else null.
  Do not write your own.
- Keep statement lists inside the question text, one per line, using \\n.
- Each language given for a question has exactly 4 options with keys a, b, c, d.
  If Hindi options are labelled क, ख, ग, घ, map them in order to a, b, c, d.
- If a question depends on an image, map, table or chart, set has_image to true
  and put [IMAGE] where it appears. Do not describe or invent it.
- If you are unsure about any question or any Hindi word (unclear text, missing
  option), set "uncertain": true. Never silently skip a question.
- subject must be one of: {subjects}. Use null if unsure.
- page = the PDF page number where the question starts.
- Output ONLY valid JSON. No commentary, no markdown.

PROCESS
- Do 25 questions per reply, then stop and wait for me to say "next".
- At the end of each reply, add one line outside the JSON:
  "Covered questions X to Y."

FORMAT
{template}
'''


def prompt_text(subject_names: list[str] | None = None) -> str:
    """The prompt for the other AI. The subject list is the app's own, so the two can't drift apart."""
    names = list(subject_names or models.STANDARD_SUBJECTS)
    # Wrapped after the fifth name, exactly where the spec's text wraps.
    subjects = ", ".join(names[:5]) + (",\n  " + ", ".join(names[5:]) if len(names) > 5 else "")
    return PROMPT_TEXT.replace("{subjects}", subjects).replace("{template}", TEMPLATE_TEXT)


# --------------------------------------------------------------------------- report objects

@dataclass
class Issue:
    level: str                      # "error" | "warning" | "info"
    message: str
    part: str | None = None
    number: int | None = None

    def where(self) -> str:
        bits = []
        if self.part:
            bits.append(self.part)
        if self.number is not None:
            bits.append(f"Q{self.number}")
        return " · ".join(bits)


@dataclass
class Report:
    parts: list[dict] = field(default_factory=list)
    questions: dict[int, dict] = field(default_factory=dict)
    paper: dict = field(default_factory=dict)
    issues: list[Issue] = field(default_factory=list)
    overlaps: list[int] = field(default_factory=list)        # same number, same content, in more than one place
    conflicts: list[int] = field(default_factory=list)       # same number, different content
    covered: list[tuple[str, int, int]] = field(default_factory=list)   # "Covered questions X to Y." lines
    existing: dict[int, str] = field(default_factory=dict)   # numbers already in the target paper -> their status
    duplicates: list[dict] = field(default_factory=list)     # look like questions already in the system

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "error"]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "warning"]

    @property
    def infos(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "info"]

    @property
    def ok(self) -> bool:
        return not self.errors and bool(self.questions)

    def add(self, level, message, part=None, number=None):
        self.issues.append(Issue(level, message, part, number))


# --------------------------------------------------------------------------- reading one part

def _clean_text(value: str) -> str:
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", value)
    # An AI that double-escaped its newlines writes a backslash and an "n" instead of a line break.
    if "\n" not in value and "\\n" in value:
        value = value.replace("\\n", "\n")
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in value.split("\n")]
    return "\n".join(lines).strip()


def _load_json(name: str, raw: bytes | str, report: Report):
    """Finds and decodes the JSON in a pasted/uploaded part, tolerating a ``` fence and a trailing 'Covered questions X to Y.'
    line (the prompt asks the AI for one). Returns the decoded value or None (with an error recorded)."""
    if isinstance(raw, bytes):
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            report.add("error", "This part isn't UTF-8 text.", name)
            return None
    else:
        text = raw.lstrip("\ufeff")
    starts = [i for i in (text.find("{"), text.find("[")) if i != -1]
    if not starts:
        report.add("error", "No JSON was found in this part (no { or [ at all).", name)
        return None
    start = min(starts)
    before = text[:start].strip()
    if before and not set(before) <= set("`json \n\t"):
        report.add("warning", "Text before the JSON was ignored.", name)
    try:
        value, end = json.JSONDecoder().raw_decode(text, start)
    except json.JSONDecodeError as e:
        numbers = NUMBER_KEY.findall(text[:e.pos])
        near = f" (near question {numbers[-1]})" if numbers else ""
        report.add("error", f"Invalid JSON: {e.msg} at line {e.lineno}, column {e.colno}{near}.", name,
                   int(numbers[-1]) if numbers else None)
        return None
    trailing = text[end:].replace("```", "").strip()
    if trailing:
        found = COVERED.search(trailing)
        if found:
            report.covered.append((name, int(found.group(1)), int(found.group(2))))
        else:
            report.add("warning", "Text after the JSON was ignored: " + trailing[:80] + ("…" if len(trailing) > 80 else ""), name)
    return value


def _as_int(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _as_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    return None


def _check_question(item, index: int, part: str, report: Report, subject_names: dict[str, str]):
    """Validates one question object. Returns its normalised dict, or None if it has errors."""
    where_number = None
    if not isinstance(item, dict):
        report.add("error", f"Question #{index} in the list isn't an object.", part)
        return None
    number = _as_int(item.get("number"))
    if number is None or number < 1:
        report.add("error", f"Question #{index} in the list has no valid \"number\" (a whole number from 1).", part)
        return None
    where_number = number
    errors_before = len(report.errors)

    def err(message):
        report.add("error", message, part, where_number)

    def warn(message):
        report.add("warning", message, part, where_number)

    # --- the two language versions -------------------------------------------------------------------------------------------
    def read_text(value):
        return _clean_text(value) if isinstance(value, str) else ""

    def read_options(raw, key, who):
        """The four cleaned options of one language ({} if the language has none). Structural problems are errors naming the key."""
        if raw is None:
            return {}
        if not isinstance(raw, dict):
            err(f"\"{key}\" must be an object with the keys a, b, c and d.")
            return {}
        keys = {str(k).strip().lower(): v for k, v in raw.items()}
        if all(v is None or (isinstance(v, str) and not v.strip()) for v in keys.values()):
            return {}                                          # all four empty: the language is simply not there
        if len(keys) != len(raw):
            err(f"\"{key}\" has two keys that differ only by case or spacing.")
        missing = [k for k in "abcd" if k not in keys]
        extra = sorted(set(keys) - set("abcd"))
        if missing:
            err(f"Missing {who}option" + ("s " if len(missing) > 1 else " ") + ", ".join(missing) + " — each language needs exactly a, b, c and d.")
        if extra:
            err(f"Unexpected {who}option key" + ("s " if len(extra) > 1 else " ") + ", ".join(extra) + " — only a, b, c and d are allowed.")
        found = {}
        for k in "abcd":
            if k in keys:
                value = keys[k]
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    value = str(value)
                if not isinstance(value, str) or not _clean_text(value):
                    err(f"{(who + 'option').capitalize()} {k} is empty.")
                else:
                    found[k] = _clean_text(value)
                    if len(found[k]) > LONG_OPTION_CHARS:
                        warn(f"{(who + 'option').capitalize()} {k} is very long ({len(found[k])} characters) — two options may have merged.")
        return found

    text = read_text(item.get("question"))
    text_hi = read_text(item.get("question_hi"))
    for key in ("question", "question_hi"):
        if item.get(key) is not None and not isinstance(item.get(key), str):
            err(f"\"{key}\" must be text or null.")
    errors_before_options = len(report.errors)
    options = read_options(item.get("options"), "options", "")
    options_hi = read_options(item.get("options_hi"), "options_hi", "Hindi ")
    complete_en, complete_hi = len(options) == 4, len(options_hi) == 4
    lang_flags: list[str] = []

    if not text and not text_hi:
        err("\"question\" is missing or empty.")
    if not complete_en and not complete_hi and len(report.errors) == errors_before_options:
        err("\"options\" must be an object with the keys a, b, c and d.")
    # A language given only in part (its text without its options, or the other way round) is importable but flagged, as long as the
    # other language is complete enough to use.
    for label, has_text, has_options, other_complete in (("English", bool(text), bool(options), complete_hi), ("Hindi", bool(text_hi), bool(options_hi), complete_en)):
        if has_text != has_options and (has_text or has_options) and other_complete:
            what = "question text without its options" if has_text else "options without a question text"
            warn(f"{label} {what} — the {label} version is incomplete and was flagged.")
            if "language_incomplete" not in lang_flags:
                lang_flags.append("language_incomplete")

    answer = item.get("correct_answer")
    if answer is not None and answer != "":
        if isinstance(answer, str) and re.fullmatch(r"\(?\s*[a-dA-D]\s*[).]?", answer.strip()):
            answer = re.sub(r"[^a-dA-D]", "", answer).upper()
        else:
            err(f"\"correct_answer\" must be a, b, c, d or null (got {json.dumps(answer, ensure_ascii=False)[:30]}).")
            answer = None
    else:
        answer = None

    explanation = item.get("explanation")
    if explanation is not None and not isinstance(explanation, str):
        err("\"explanation\" must be text or null.")
        explanation = None
    explanation = _clean_text(explanation) if isinstance(explanation, str) else None
    explanation = explanation or None
    explanation_hi = item.get("explanation_hi")
    if explanation_hi is not None and not isinstance(explanation_hi, str):
        err("\"explanation_hi\" must be text or null.")
        explanation_hi = None
    explanation_hi = (_clean_text(explanation_hi) if isinstance(explanation_hi, str) else None) or None

    subject = item.get("subject")
    canonical = None
    if subject not in (None, ""):
        if isinstance(subject, str) and subject.strip().lower() in subject_names:
            canonical = subject_names[subject.strip().lower()]
        else:
            warn(f"Subject {json.dumps(subject, ensure_ascii=False)[:40]} isn't in the fixed list — left blank. "
                 f"Known: {', '.join(subject_names.values())}.")
    topic = item.get("topic")
    topic = _clean_text(topic) if isinstance(topic, str) and topic.strip() else None

    has_image = _as_bool(item.get("has_image")) if item.get("has_image") is not None else False
    if has_image is None:
        warn("\"has_image\" should be true or false — treated as false.")
        has_image = False
    uncertain = _as_bool(item.get("uncertain")) if item.get("uncertain") is not None else False
    if uncertain is None:
        warn("\"uncertain\" should be true or false — treated as false.")
        uncertain = False
    page = item.get("page")
    if page is not None and (_as_int(page) is None or _as_int(page) < 1):
        warn("\"page\" should be a page number from 1 — ignored.")
        page = None
    page = _as_int(page) if page is not None else None

    for label, shown in (("", text), ("Hindi ", text_hi)):
        if shown and len(shown) < MIN_QUESTION_CHARS:
            warn(f"The {label}question is very short ({len(shown)} characters): “{shown}”.")
    if ("[IMAGE]" in text or "[IMAGE]" in text_hi) and not has_image:
        warn("The text has [IMAGE] but \"has_image\" isn't true.")

    # A cheap script check, not a translation check: Hindi fields should be mostly Devanagari, English fields mostly not.
    hindi_share = language.devanagari_share(" ".join([text_hi, *options_hi.values(), explanation_hi or ""]))
    english_share = language.devanagari_share(" ".join([text, *options.values(), explanation or ""]))
    if hindi_share is not None and hindi_share < language.HINDI_MIN_SHARE:
        warn("The Hindi fields (question_hi / options_hi) are mostly not Devanagari — the languages may be swapped or the Hindi may not be Hindi.")
        lang_flags.append("language_swapped")
    if english_share is not None and english_share > language.ENGLISH_MAX_SHARE:
        warn("The English fields (question / options) are mostly Devanagari — the languages may be swapped.")
        if "language_swapped" not in lang_flags:
            lang_flags.append("language_swapped")
    if len(report.errors) > errors_before:
        return None
    return {"number": number, "text": text, "options": options, "text_hi": text_hi, "options_hi": options_hi,
            "answer": answer, "explanation": explanation, "explanation_hi": explanation_hi, "lang_flags": lang_flags,
            "subject": canonical, "topic": topic, "has_image": has_image, "uncertain": uncertain, "page": page, "part": part}


# --------------------------------------------------------------------------- merging parts

def _same_words(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())


def _has_en(q: dict) -> bool:
    return bool(q["text"])


def _has_hi(q: dict) -> bool:
    return bool(q["text_hi"]) or bool(q["options_hi"])


def _same_question(a: dict, b: dict) -> bool | None:
    """Do these two entries for one number say the same thing? Each language is compared only when BOTH entries have it (an entry with
    English only and one with English and Hindi are the same question if the English matches). None when they share no language at all."""
    both_en, both_hi = _has_en(a) and _has_en(b), _has_hi(a) and _has_hi(b)
    if not both_en and not both_hi:
        return None
    if both_en and not (_same_words(a["text"]) == _same_words(b["text"])
                        and all(_same_words(a["options"].get(k, "")) == _same_words(b["options"].get(k, "")) for k in "abcd")):
        return False
    if both_hi and not (_same_words(a["text_hi"]) == _same_words(b["text_hi"])
                        and all(_same_words(a["options_hi"].get(k, "")) == _same_words(b["options_hi"].get(k, "")) for k in "abcd")):
        return False
    return True


def _merge(existing: dict, new: dict, report: Report, later_wins: bool) -> None:
    number = new["number"]
    same = _same_question(existing, new)
    if same is None:
        report.conflicts.append(number)
        if later_wins:
            report.add("warning", f"Q{number} in {new['part']} replaces Q{number} from {existing['part']} (they share no language to compare).",
                       new["part"], number)
            existing.clear()
            existing.update(new)
        else:
            report.add("error", f"Q{number} appears in {existing['part']} (English or Hindi only) and in {new['part']} (the other language only), "
                                "so they can't be checked to be the same question.", new["part"], number)
        return
    if same:
        fields = ("answer", "explanation", "explanation_hi", "subject", "topic", "page")
        conflicting = [f for f in fields
                       if existing[f] is not None and new[f] is not None and _same_words(str(existing[f])) != _same_words(str(new[f]))]
        if conflicting and not later_wins:
            report.conflicts.append(number)
            report.add("error", f"Q{number} appears in {existing['part']} and {new['part']} with the same text but different "
                                f"{', '.join(conflicting)}.", new["part"], number)
            return
        for f in fields:
            if new[f] is not None and (existing[f] is None or later_wins):
                existing[f] = new[f]
        if not _has_en(existing) and _has_en(new):                 # a language the first entry lacked is filled in from the second
            existing["text"], existing["options"] = new["text"], new["options"]
        if not _has_hi(existing) and _has_hi(new):
            existing["text_hi"], existing["options_hi"] = new["text_hi"], new["options_hi"]
        existing["lang_flags"] = sorted(set(existing["lang_flags"]) | set(new["lang_flags"]))
        existing["has_image"] = existing["has_image"] or new["has_image"]
        existing["uncertain"] = existing["uncertain"] or new["uncertain"]
        report.overlaps.append(number)
        report.add("info", f"Q{number} is in both {existing['part']} and {new['part']} (same text) — merged.", new["part"], number)
        return
    if later_wins:
        report.conflicts.append(number)
        report.add("warning", f"Q{number} in {new['part']} replaces the different Q{number} from {existing['part']}.", new["part"], number)
        existing.clear()
        existing.update(new)
        return
    report.conflicts.append(number)
    report.add("error", f"Q{number} appears in {existing['part']} and in {new['part']} with different text or options.",
               new["part"], number)


# --------------------------------------------------------------------------- the whole report

def norm_hash(text: str, options) -> str:
    """The same question, whatever its case, punctuation, spacing or option order, gets the same hash. Latin letters, digits and
    Devanagari letters are kept, so a Hindi question doesn't collapse to an empty string."""
    def n(s):
        return re.sub(r"[^a-z0-9ऀ-ॣ॰-ॿ]+", "", (s or "").lower())
    values = list(options.values()) if isinstance(options, dict) else list(options)
    return hashlib.sha1((n(text) + "|" + "|".join(sorted(n(o) for o in values))).encode()).hexdigest()[:16]


def q_hash(q: dict) -> str:
    """Hash of a parsed question: of its English version when it has one, otherwise of its Hindi version."""
    if q["text"]:
        return norm_hash(q["text"], q["options"])
    return norm_hash(q["text_hi"], q["options_hi"])


def build_report(parts: list[tuple[str, bytes | str]], subject_names: list[str], *, expected_total: int | None = None,
                 later_wins: bool = False, topics: dict[str, set[str]] | None = None,
                 existing_numbers: dict[int, str] | None = None, known_hashes: dict[str, str] | None = None) -> Report:
    """Validate and merge the parts. Nothing is saved.

    topics: {subject name: {lower-case topic names}} — a topic in the JSON is used only if it already exists.
    existing_numbers: numbers already in the paper being added to (-> their status).
    known_hashes: {norm_hash: "Paper title Q7"} of questions already in the system, for the duplicate warning."""
    report = Report()
    by_lower = {s.lower(): s for s in subject_names}
    if not parts:
        report.add("error", "Add at least one JSON part (upload a file or paste it).")
        return report
    merged: dict[int, dict] = {}
    for name, raw in parts:
        value = _load_json(name, raw, report)
        if value is None:
            report.parts.append({"name": name, "questions": 0, "first": None, "last": None})
            continue
        paper_block, items = {}, None
        if isinstance(value, list):
            items = value
            report.add("warning", "This part is a bare list of questions (no \"paper\" block).", name)
        elif isinstance(value, dict):
            version = value.get("schema_version")
            if version is None:
                report.add("warning", "No \"schema_version\" — assuming version 1.", name)
            elif version not in SCHEMA_VERSIONS or isinstance(version, bool):
                report.add("error", f"schema_version {json.dumps(version)[:20]} isn't supported (expected 1 or {SCHEMA_VERSION}).", name)
                report.parts.append({"name": name, "questions": 0, "first": None, "last": None})
                continue
            if isinstance(value.get("paper"), dict):
                paper_block = value["paper"]
            items = value.get("questions")
        if not isinstance(items, list):
            report.add("error", "\"questions\" must be a list.", name)
            report.parts.append({"name": name, "questions": 0, "first": None, "last": None})
            continue
        for key, val in paper_block.items():
            if val in (None, ""):
                continue
            if key in report.paper and report.paper[key] != val:
                report.add("warning", f"\"paper.{key}\" differs between parts ({json.dumps(report.paper[key], ensure_ascii=False)[:40]} "
                                      f"vs {json.dumps(val, ensure_ascii=False)[:40]}) — the first part's value is used.", name)
            else:
                report.paper.setdefault(key, val)

        seen_here = []
        for index, item in enumerate(items, start=1):
            q = _check_question(item, index, name, report, by_lower)
            if q is None:
                continue
            seen_here.append(q["number"])
            if q["number"] in merged:
                _merge(merged[q["number"]], q, report, later_wins)
            else:
                merged[q["number"]] = q
        report.parts.append({"name": name, "questions": len(seen_here), "first": min(seen_here, default=None),
                             "last": max(seen_here, default=None)})

    report.questions = dict(sorted(merged.items()))
    # Every part is expected to say what it covered; if the AI did and the parts disagree, that's worth showing.
    for name, first, last in report.covered:
        got = [q for q in report.questions.values() if q["part"] == name]
        actual = sorted(q["number"] for q in got)
        wanted = list(range(first, last + 1))
        missing = [n for n in wanted if n not in actual and n not in merged]
        if missing:
            report.add("warning", f"{name} says it covered questions {first} to {last}, but {', '.join(map(str, missing[:10]))} "
                                  f"{'are' if len(missing) > 1 else 'is'} missing.", name)

    # Numbers: gaps, extras and the expected total.
    expected = expected_total or _as_int(report.paper.get("expected_total"))
    numbers = sorted(report.questions)
    if numbers:
        top = expected or numbers[-1]
        missing = [n for n in range(1, top + 1) if n not in report.questions]
        if missing:
            shown = ", ".join(map(str, missing[:20])) + (" …" if len(missing) > 20 else "")
            report.add("warning", f"{len(missing)} question number{'s' if len(missing) != 1 else ''} missing"
                                  f"{' from 1–' + str(top)}: {shown}.")
        beyond = [n for n in numbers if expected and n > expected]
        if beyond:
            report.add("warning", f"Question numbers beyond the expected {expected}: {', '.join(map(str, beyond[:10]))}.")
        if expected and len(numbers) != expected:
            report.add("warning", f"{len(numbers)} questions found but {expected} expected.")

    # Topics may only be reused, never invented.
    dropped = 0
    for q in report.questions.values():
        if q["topic"]:
            known = (topics or {}).get(q["subject"] or "", set())
            if q["topic"].lower() not in known:
                dropped += 1
                q["topic"] = None
    if dropped:
        report.add("warning", f"{dropped} topic{'s' if dropped != 1 else ''} in the JSON don't exist under that subject "
                              f"and were ignored (topics are never created from an import).")

    if existing_numbers:
        report.existing = {n: s for n, s in existing_numbers.items() if n in report.questions}

    # Possible duplicates: inside this import, and against what's already in the system.
    seen: dict[str, int] = {}
    for n, q in report.questions.items():
        q["hash"] = q_hash(q)
        if q["hash"] in seen:
            report.duplicates.append({"number": n, "of": f"Q{seen[q['hash']]} in this import"})
        else:
            seen[q["hash"]] = n
        if known_hashes and q["hash"] in known_hashes and n not in report.existing:
            report.duplicates.append({"number": n, "of": known_hashes[q["hash"]]})
    for d in report.duplicates:
        report.add("warning", f"Q{d['number']} looks like {d['of']} (same text and options, ignoring case and punctuation).",
                   number=d["number"])

    if not report.questions and not report.errors:
        report.add("error", "No questions were found in the JSON.")
    return report


# --------------------------------------------------------------------------- saving

AI_FLAGS = {
    "ai_answer": "The answer was supplied by an AI — check it against the printed key before confirming",
    "ai_uncertain": "The AI marked this question as uncertain — check it against the paper",
    "image_needed": "Depends on an image or table — needs a snapshot before it can go live",
}


LANGUAGE_FLAGS = {
    "language_incomplete": "One language is incomplete (its question text or its options are missing) — check both versions",
    "language_swapped": "The English and Hindi text look swapped, or one isn't in the expected script — check both versions",
    "language_mismatch": "The English and Hindi versions don't obviously correspond (a different number of options or of statement lines) — compare them",
}


def flags_for(q: dict) -> list[str]:
    flags = []
    if q["uncertain"]:
        flags.append("ai_uncertain")
    if q["answer"]:
        flags.append("ai_answer")
    if q["has_image"]:
        flags.append("image_needed")
    flags.extend(f for f in q.get("lang_flags", []) if f not in flags)
    return flags


def save_questions(db, user, report: Report, paper: models.Paper, *, overwrite_needs_review: bool = False) -> dict:
    """Writes the merged questions into `paper` (the caller commits). Existing numbers are skipped, except that a question
    still waiting for review may be overwritten on request (its old content is kept in the version history). Returns counts."""
    subjects = {s.name: s.id for s in db.query(models.Subject).all()}
    topic_ids = {(t.subject_id, t.name.lower()): t.id for t in db.query(models.Topic).all()}
    present = {q.question_number: q for q in db.query(models.Question).filter_by(paper_id=paper.id).all()}
    counts = {"created": 0, "overwritten": 0, "skipped_existing": 0}
    saved: list[models.Question] = []
    for number, q in report.questions.items():
        subject_id = subjects.get(q["subject"]) if q["subject"] else None
        topic_id = topic_ids.get((subject_id, q["topic"].lower())) if q["topic"] and subject_id else None
        en, hi = q["options"], q["options_hi"]                  # a language the question lacks stays "" (English) or null (Hindi)
        values = dict(
            text=q["text"], option_a=en.get("a", ""), option_b=en.get("b", ""), option_c=en.get("c", ""), option_d=en.get("d", ""),
            question_hi=q["text_hi"] or None, option_a_hi=hi.get("a"), option_b_hi=hi.get("b"), option_c_hi=hi.get("c"), option_d_hi=hi.get("d"),
            correct_answer=q["answer"], subject_id=subject_id, topic_id=topic_id,
            explanation=q["explanation"], has_image=q["has_image"], explanation_status="unverified" if q["explanation"] else None,
            explanation_hi=q["explanation_hi"], explanation_hi_status="unverified" if q["explanation_hi"] else None,
        )
        extras = dict(
            status=models.QStatus.NEEDS_REVIEW, source="ai_json", extraction_method="json",
            answer_source="json" if q["answer"] else None, uncertain=q["uncertain"], page_number=q["page"],
            ocr_flags=",".join(flags_for(q)) or None, norm_hash=q["hash"], source_image_path=None,
            reviewed_by=None, reviewed_at=None, flags_acknowledged=False,
        )
        old = present.get(number)
        if old is None:
            fresh = models.Question(paper_id=paper.id, question_number=number, **values, **extras)
            db.add(fresh)
            saved.append(fresh)
            counts["created"] += 1
        elif overwrite_needs_review and old.status in (models.QStatus.NEEDS_REVIEW, models.QStatus.DRAFT):
            versions.snapshot(db, old, user, "replaced by JSON import")
            for k, v in {**values, **extras}.items():
                setattr(old, k, v)
            saved.append(old)
            counts["overwritten"] += 1
        else:
            counts["skipped_existing"] += 1
    for row in saved:
        language.refresh_flags(row)                          # incomplete / swapped / mismatch, from what was actually stored
    db.flush()
    from app import subject_hints
    subject_hints.suggest_for_paper(db, paper.id)          # hints for questions the JSON left without a subject
    from app import duplicates
    counts["duplicates"] = duplicates.scan(db, paper.id)   # possible duplicates are listed for the admin
    return counts


def render_pages(pdf_path: str, images_dir: str, pages) -> tuple[int, str | None]:
    """Saves an admin-only picture of each referenced PDF page (page{N}.jpg). Returns (saved, problem or None)."""
    import os
    import pypdfium2 as pdfium
    wanted = sorted({p for p in pages if p})
    if not wanted:
        return 0, None
    os.makedirs(images_dir, exist_ok=True)
    saved = 0
    try:
        pdf = pdfium.PdfDocument(pdf_path)
        try:
            for n in wanted:
                if 1 <= n <= len(pdf):
                    pdf[n - 1].render(scale=1.5).to_pil().convert("L").save(os.path.join(images_dir, f"page{n}.jpg"), quality=75)
                    saved += 1
        finally:
            pdf.close()
    except Exception as e:                                   # a page picture is a convenience, never a reason to fail the import
        return saved, f"{type(e).__name__}: {e}"
    return saved, None
