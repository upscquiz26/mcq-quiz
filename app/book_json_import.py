"""Validation and normalization for chapter-wise book JSON imports."""
from __future__ import annotations

import json
from dataclasses import dataclass, field

LETTERS = "abcde"


@dataclass
class BookImportReport:
    subject: str = ""
    chapters: list[dict] = field(default_factory=list)
    questions: list[dict] = field(default_factory=list)
    issues: list[dict] = field(default_factory=list)

    @property
    def errors(self) -> list[dict]:
        return [issue for issue in self.issues if issue["level"] == "error"]

    @property
    def warnings(self) -> list[dict]:
        return [issue for issue in self.issues if issue["level"] == "warning"]

    @property
    def ok(self) -> bool:
        return bool(self.questions) and not self.errors

    def add(self, level: str, message: str, *, file: str = "", chapter: str = "", number=None) -> None:
        self.issues.append({"level": level, "message": message, "file": file,
                            "chapter": chapter, "number": number})

    def as_dict(self) -> dict:
        return {"subject": self.subject, "chapters": self.chapters, "questions": self.questions,
                "issues": self.issues}


def build_report(parts: list[tuple[str, bytes]], subject_override: str = "") -> BookImportReport:
    """Validate independent chapter JSON files and flatten them in chapter order.

    `number` in the normalized questions is a unique book-wide number. `chapter_number`
    preserves the number printed in the original chapter.
    """
    report = BookImportReport()
    chapters_seen: set[int] = set()
    files = []
    override = (subject_override or "").strip()
    report.subject = override

    for filename, raw in parts:
        try:
            document = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            report.add("error", f"Invalid UTF-8 JSON: {exc}", file=filename)
            continue
        if not isinstance(document, dict) or not isinstance(document.get("questions"), list):
            report.add("error", "Expected a chapter object with a questions array.", file=filename)
            continue

        try:
            chapter_no = int(document.get("chapter_no"))
            if chapter_no < 1:
                raise ValueError
        except (ValueError, TypeError):
            report.add("error", "chapter_no must be a positive integer.", file=filename)
            continue
        chapter_name = document.get("chapter")
        if not isinstance(chapter_name, str) or not chapter_name.strip():
            report.add("error", "chapter must be a non-empty string.", file=filename, chapter=str(chapter_no))
            continue
        chapter_name = chapter_name.strip()
        if chapter_no in chapters_seen:
            report.add("error", f"Duplicate chapter_no {chapter_no}; don't include phy_ALL.json with chapter files.",
                       file=filename, chapter=chapter_name)
            continue
        chapters_seen.add(chapter_no)

        input_subject = document.get("subject")
        input_subject = input_subject.strip() if isinstance(input_subject, str) else ""
        if not report.subject:
            report.subject = input_subject
        elif input_subject and report.subject and input_subject.casefold() != report.subject.casefold():
            report.add("error", f"Subject {input_subject!r} doesn't match the book subject {report.subject!r}.",
                       file=filename, chapter=chapter_name)

        expected = document.get("question_count")
        if isinstance(expected, int) and not isinstance(expected, bool) and expected != len(document["questions"]):
            report.add("warning", f"question_count says {expected}, but this file contains {len(document['questions'])} questions.",
                       file=filename, chapter=chapter_name)

        local_seen: set[int] = set()
        chapter_questions = []
        for index, item in enumerate(document["questions"], start=1):
            if not isinstance(item, dict):
                report.add("error", f"Question entry #{index} must be an object.", file=filename, chapter=chapter_name)
                continue
            try:
                local_number = int(item.get("number"))
                if local_number < 1:
                    raise ValueError
            except (ValueError, TypeError):
                report.add("error", f"Question entry #{index} has no positive integer number.", file=filename, chapter=chapter_name)
                continue
            before = len(report.errors)
            if local_number in local_seen:
                report.add("error", f"Duplicate question number {local_number} in chapter.", file=filename,
                           chapter=chapter_name, number=local_number)
            local_seen.add(local_number)

            text = item.get("question")
            if not isinstance(text, str) or not text.strip():
                report.add("error", "question must be non-empty text.", file=filename, chapter=chapter_name, number=local_number)
                text = ""
            raw_options = item.get("options")
            options = {}
            if not isinstance(raw_options, dict):
                report.add("error", "options must be an object with a, b, c, d and optionally e.",
                           file=filename, chapter=chapter_name, number=local_number)
            else:
                for key, value in raw_options.items():
                    key = str(key).strip().lower()
                    if key not in LETTERS:
                        report.add("error", f"Unsupported option key {key!r}; use a through e.",
                                   file=filename, chapter=chapter_name, number=local_number)
                        continue
                    if not isinstance(value, str) or not value.strip():
                        report.add("error", f"Option {key.upper()} is empty.", file=filename,
                                   chapter=chapter_name, number=local_number)
                        continue
                    options[key] = value.strip()
                expected_keys = set("abcd") if "e" not in options else set("abcde")
                if set(options) != expected_keys:
                    report.add("error", "Options must be consecutive a–d, or a–e when option e is present.",
                               file=filename, chapter=chapter_name, number=local_number)

            answer = item.get("answer")
            if answer not in (None, ""):
                answer = str(answer).strip().lower()
                if answer not in LETTERS:
                    report.add("error", f"Answer {answer!r} must be a–e or null.", file=filename,
                               chapter=chapter_name, number=local_number)
                elif answer not in options:
                    report.add("error", f"Answer {answer.upper()} has no matching option.", file=filename,
                               chapter=chapter_name, number=local_number)
            else:
                answer = None
            explanation = item.get("explanation")
            if explanation is not None and not isinstance(explanation, str):
                report.add("error", "explanation must be text or null.", file=filename,
                           chapter=chapter_name, number=local_number)
                explanation = None

            flags = item.get("needs_review") or []
            if not isinstance(flags, list):
                flags = [str(flags)]
            flags = [str(flag).strip() for flag in flags if str(flag).strip()]
            if answer is None:
                flags.append("no_official_answer")
                report.add("warning", "No answer is supplied; question will remain out of student practice until reviewed.",
                           file=filename, chapter=chapter_name, number=local_number)
            if flags and not (answer is None and flags == ["no_official_answer"]):
                report.add("warning", "Source marks this question for review: " + ", ".join(flags),
                           file=filename, chapter=chapter_name, number=local_number)

            if len(report.errors) == before:
                chapter_questions.append({
                    "chapter_no": chapter_no,
                    "chapter": chapter_name,
                    "chapter_number": local_number,
                    "external_id": str(item.get("id") or ""),
                    "text": text.strip(),
                    "options": options,
                    "answer": answer.upper() if answer else None,
                    "explanation": explanation.strip() if isinstance(explanation, str) and explanation.strip() else None,
                    "page_label": str(item.get("page") or ""),
                    "sources": item.get("source") if isinstance(item.get("source"), list) else [],
                    "source_flags": flags,
                })

        files.append({"number": chapter_no, "name": chapter_name, "filename": filename,
                      "questions": chapter_questions})

    if override:
        report.subject = override
    if not report.subject:
        report.add("error", "Provide a book subject or ensure chapter files contain a subject.")
    if not files:
        report.add("error", "No valid chapter files were found.")

    files.sort(key=lambda chapter: chapter["number"])
    report.chapters = [{"number": c["number"], "name": c["name"], "filename": c["filename"],
                        "questions": len(c["questions"])} for c in files]
    next_number = 1
    for chapter in files:
        for question in sorted(chapter["questions"], key=lambda q: q["chapter_number"]):
            question["number"] = next_number
            report.questions.append(question)
            next_number += 1
    return report
