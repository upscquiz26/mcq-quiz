"""
Answer key + explanations PDF -> {question_number: {"answer": "A".."D", "explanation": str}}

Works on PDFs that have a real text layer, laid out as:

    12. Ans– (c)
    Both the statements mentioned in the question are correct.
    13. Ans– (d)
    ...

No AI and no OCR: it's a plain pattern match, so every answer it returns is
exactly what the PDF says. Answers for questions it can't find are simply absent.
"""
import re

import pdfplumber

# "12. Ans– (c)" — tolerate hyphen/en dash/colon and missing brackets.
ANSWER_LINE = re.compile(r"^\s*(\d{1,3})\s*[.)]\s*Ans\w*\s*[–—\-:]*\s*\(?\s*([a-dA-D])\s*\)?", re.M)
PAGE_FOOTER = re.compile(r"^\s*(SERIES\s*:|Page\s+No\.?\s*\d+).*$", re.I)


class AnswerKeyError(RuntimeError):
    pass


def _clean_explanation(chunk: str) -> str:
    kept = []
    for line in chunk.splitlines():
        line = line.strip()
        if not line or PAGE_FOOTER.match(line):
            continue
        if not re.search(r"[A-Za-z0-9]", line):      # bullets, glyph-only lines, dashed rules
            continue
        kept.append(line)
    return "\n".join(kept)


def parse_answer_key(pdf_path: str) -> dict[int, dict]:
    with pdfplumber.open(pdf_path) as pdf:
        text = "\n".join((page.extract_text() or "") for page in pdf.pages)
    if not text.strip():
        raise AnswerKeyError(
            "The answer PDF has no text layer (it looks like a scan), so its answers can't be read automatically."
        )

    matches = list(ANSWER_LINE.finditer(text))
    if not matches:
        raise AnswerKeyError("No answers in the form '12. Ans– (c)' were found in the answer PDF.")

    key: dict[int, dict] = {}
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        n = int(m.group(1))
        if n in key:      # a repeated number is more likely stray text than a second answer
            continue
        key[n] = {
            "answer": m.group(2).upper(),
            "explanation": _clean_explanation(text[m.end():end]),
        }
    return key
