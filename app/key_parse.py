"""
Answer keys: text (pasted, a key file, or a block at the end of a question PDF) -> {question number: 'A'..'D'}.

Nothing here touches the database. The result is shown to the admin as a preview first (routes/keys.py); only "Apply" writes.

Formats understood (detected in this order):
  1. labelled lines, with explanations           12. Ans– (c)  <explanation until the next label>
  2. a series table                              1  b  c  a  d      (one column per booklet series A B C D; the paper's series picks the column)
  3. series blocks                               Series A  1-b 2-d ...   Series B  1-c 2-a ...
  4. pairs anywhere                              1-b 2-d 3-a   |   1. (b)   |   Q1: B   |   1) b   |   1 b 2 d   |   1b 2d

Only the letters a-e are answers (any case). A key written with digits (1-4) is refused rather than guessed at, and a question number
given two different letters is an error. Every problem is listed; nothing is silently skipped.
"""
import re
from dataclasses import dataclass, field

from app.answer_key import ANSWER_LINE, _clean_explanation

MAX_NUMBER = 999
SERIES_LETTERS = "ABCD"

PAIR = re.compile(r"(?<![\w/])(?:Q(?:uestions?|s)?\.?\s*)?(\d{1,3})(?:\s*[-–—:.)=|,]\s*|\s+|(?=[A-Ea-e(]))\(?\s*([A-Ea-e])\s*\)?(?![A-Za-z0-9])")
DIGIT_PAIR = re.compile(r"(?<![\w/])(\d{1,3})\s*[-–—:.)=|,]\s*\(?\s*([1-4])\s*\)?(?![A-Za-z0-9])")
SERIES_ROW = re.compile(
    r"^[ \t]*(?:Q\.?[ \t]*)?(\d{1,3})[\s|,;:.)\-]+([A-Da-d])[\s|,;:/\-]+([A-Da-d])[\s|,;:/\-]+([A-Da-d])[\s|,;:/\-]+([A-Da-d])[ \t]*$", re.M)
SERIES_HEADING = re.compile(r"^[ \t]*(?:Series|Set|Code|Booklet)[ \t]*[:\-]?[ \t]*([A-Da-d])\b.*$", re.I | re.M)


@dataclass
class KeyIssue:
    level: str                  # "error" | "warning" | "info"
    message: str
    number: int | None = None


@dataclass
class KeyParse:
    format: str = ""
    answers: dict[int, str] = field(default_factory=dict)
    explanations: dict[int, str] = field(default_factory=dict)
    issues: list[KeyIssue] = field(default_factory=list)
    series: str | None = None            # the series column/block that was used, if the key had several

    @property
    def errors(self) -> list[KeyIssue]:
        return [i for i in self.issues if i.level == "error"]

    @property
    def warnings(self) -> list[KeyIssue]:
        return [i for i in self.issues if i.level == "warning"]

    @property
    def ok(self) -> bool:
        return bool(self.answers) and not self.errors

    def add(self, level: str, message: str, number: int | None = None) -> None:
        self.issues.append(KeyIssue(level, message, number))


def _normalise(text: str) -> str:
    return (text or "").replace(" ", " ").replace("\r\n", "\n").replace("\r", "\n")


def _collect(pairs, result: KeyParse) -> dict[int, str]:
    """[(number, letter)] -> {number: LETTER}. A number that gets two different letters is an error (the first is kept)."""
    found: dict[int, str] = {}
    conflicts: dict[int, set] = {}
    for number, letter in pairs:
        number, letter = int(number), letter.upper()
        if not 1 <= number <= MAX_NUMBER:
            continue
        if number in found and found[number] != letter:
            conflicts.setdefault(number, {found[number]}).add(letter)
        found.setdefault(number, letter)
    for number, letters in sorted(conflicts.items()):
        result.add("error", f"Question {number} is given more than one answer ({', '.join(sorted(letters))}).", number)
    return found


def _parse_pairs(text: str, result: KeyParse) -> dict[int, str]:
    letters = _collect(((m.group(1), m.group(2)) for m in PAIR.finditer(text)), result)
    if not letters:
        digits = DIGIT_PAIR.findall(text)
        if len(digits) >= 3:
            result.add("error", "This key uses numbers (1–4) as answers. Only the letters a, b, c and d are accepted — "
                                "convert it (1=a, 2=b, 3=c, 4=d) and try again.")
    return letters


def parse_key_text(text: str, series: str | None = None) -> KeyParse:
    """Parse a key. `series` is the paper's booklet series (A-D), used only when the key has one column or block per series."""
    result = KeyParse()
    text = _normalise(text)
    series = (series or "").strip().upper()[:1] or None
    if not text.strip():
        result.add("error", "The key is empty.")
        return result

    # 1. labelled lines with explanations
    labelled = list(ANSWER_LINE.finditer(text))
    if len(labelled) >= 3:
        result.format = "labelled lines with explanations (12. Ans– (c))"
        pairs = []
        for i, m in enumerate(labelled):
            end = labelled[i + 1].start() if i + 1 < len(labelled) else len(text)
            n = int(m.group(1))
            if n not in result.explanations:
                explanation = _clean_explanation(text[m.end():end])
                if explanation:
                    result.explanations[n] = explanation
            pairs.append((n, m.group(2)))
        result.answers = _collect(pairs, result)
        _finish(result)
        return result

    # 2. a table with one column per series
    rows = SERIES_ROW.findall(text)
    if len(rows) >= 3:
        result.format = "series table (one column each for series A, B, C and D)"
        if not series:
            result.add("error", "This key has separate columns for booklet series A–D, but this paper has no series set. "
                                "Set the paper's series and try again.")
            return result
        column = SERIES_LETTERS.index(series)
        result.series = series
        result.answers = _collect(((row[0], row[1 + column]) for row in rows), result)
        result.add("info", f"Used the column for series {series}.")
        _finish(result)
        return result

    # 3. blocks headed "Series A", "Series B" ...
    headings = list(SERIES_HEADING.finditer(text))
    if len(headings) >= 2:
        result.format = "series blocks (Series A … Series B …)"
        if not series:
            result.add("error", "This key has a separate block for each booklet series, but this paper has no series set. "
                                "Set the paper's series and try again.")
            return result
        blocks = {}
        for i, m in enumerate(headings):
            end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
            blocks[m.group(1).upper()] = text[m.end():end]
        if series not in blocks:
            result.add("error", f"This key has blocks for series {', '.join(sorted(blocks))} but not for this paper's series {series}.")
            return result
        result.series = series
        result.answers = _parse_pairs(blocks[series], result)
        result.add("info", f"Used the block for series {series}.")
        _finish(result)
        return result

    # 4. plain pairs
    result.format = "answer pairs (1-b 2-d …)"
    result.answers = _parse_pairs(text, result)
    _finish(result)
    return result


def _finish(result: KeyParse) -> None:
    if not result.answers and not result.errors:
        result.add("error", "No answers were found. Expected something like “1-b 2-d 3-a”, “1. (b)” or “Q1: B”.")


def parse_key_pdf(path: str, series: str | None = None) -> KeyParse:
    """A key PDF with a real text layer. A scan has none — paste its text instead."""
    import pdfplumber
    with pdfplumber.open(path) as pdf:
        text = "\n".join((page.extract_text() or "") for page in pdf.pages)
    if not text.strip():
        result = KeyParse()
        result.add("error", "This PDF has no text layer (it looks like a scan), so its answers can't be read. "
                            "Type or paste the key instead.")
        return result
    return parse_key_text(text, series)


def parse_key_bytes(data: bytes, filename: str, series: str | None = None, workdir: str | None = None) -> KeyParse:
    """An uploaded key: a PDF (needs a text layer) or a text file."""
    import os
    import tempfile
    if data[:5] == b"%PDF-":
        with tempfile.TemporaryDirectory(dir=workdir) as tmp:
            path = os.path.join(tmp, "key.pdf")
            with open(path, "wb") as f:
                f.write(data)
            return parse_key_pdf(path, series)
    for encoding in ("utf-8-sig", "utf-16", "latin-1"):
        try:
            return parse_key_text(data.decode(encoding), series)
        except (UnicodeDecodeError, UnicodeError):
            continue
    result = KeyParse()
    result.add("error", f"“{filename}” isn't a PDF or a text file.")
    return result


# --------------------------------------------------------------------------- what an explanation says about the answer

# Only unambiguous phrasings, and only a letter in brackets: "the correct answer is (c)", "option (c) is correct",
# "Hence, option (b) is correct". Prose like "A is true" never counts.
_SAYS = [
    re.compile(r"(?:correct|right|desired|final)\s+(?:answer|option)\s*(?:is|:|=|-)?\s*(?:option\s*)?\(\s*([a-eA-E])\s*\)", re.I),
    re.compile(r"(?:answer|option)\s*(?:is|:|=)?\s*\(\s*([a-eA-E])\s*\)\s*(?:is\s+)?(?:the\s+)?(?:correct|right)", re.I),
    re.compile(r"\boption\s*\(\s*([a-eA-E])\s*\)\s+is\s+(?:the\s+)?(?:correct|right)", re.I),
]


def explanation_says(explanation: str | None) -> str | None:
    """The answer letter an explanation states outright, or None if it states none — or contradicts itself."""
    if not explanation:
        return None
    said = {m.group(1).upper() for pattern in _SAYS for m in pattern.finditer(explanation)}
    return said.pop() if len(said) == 1 else None


def refresh_mismatch_flag(q) -> None:
    """Keeps a question's `explanation_mismatch` warning true to its current answer and explanation."""
    flags = [f for f in (q.ocr_flags or "").split(",") if f and f != "explanation_mismatch"]
    if q.correct_answer and q.explanation_says and q.explanation_says != q.correct_answer:
        flags.append("explanation_mismatch")
    q.ocr_flags = ",".join(flags) or None
