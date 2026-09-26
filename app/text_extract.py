"""
Question paper PDF -> structured questions by reading the PDF's own text layer (no OCR).

Most official papers and many coaching papers are real text PDFs. Reading the text is exact where OCR guesses, so
ingest.process_paper prefers this path and falls back to OCR (app/ocr_extract.py) only for scans.

Per page:
  1. Drop oversized characters — the giant diagonal watermarks ("UPPCS ONE" ...) that would otherwise be
     sprinkled through every line.
  2. Find the gutter between two columns from the widest empty vertical strip in the body of the page (or, on bilingual
     papers, from where Hindi and English cluster), and read each column on its own. A Hindi column next to an English
     column is dropped — this app reads English from PDFs. Pages that don't have a gutter are one column.
  3. Drop running headers and footers: lines in the top/bottom band of the page that repeat on many pages.
Across pages the lines are joined into one stream and cut into questions:
  * a question starts at the line that begins with the NEXT expected number ("7." after question 6), and only once
    the previous question's options (a)-(d) have been seen — so numbered statements inside a question ("1. 2. 3.")
    are never mistaken for questions;
  * if the paper prints "Answer: (c)" under each question, the letter and the explanation that follows are captured too;
  * options are the last (a)(b)(c)(d) markers in the block, whether one or two to a line.
Every question gets a snapshot image of exactly what was printed (question and options, never the answer), for the
review screen.
"""
import os
import re
import statistics

import pdfplumber
from PIL import Image

from app.ocr_extract import sanity_flags

MIN_TEXT_CHARS = 200            # a page with fewer characters than this has no usable text layer
TEXT_PAGE_SHARE = 0.5           # this share of pages must have text before the paper is read as text (covers and
                                # "space for rough work" pages have almost none, so this can't be too strict)
WATERMARK_MIN_SIZE = 30.0       # pt: characters bigger than both this ...
WATERMARK_FACTOR = 3.0          # ... and this many times the page's median size are watermark, not text
TINY_SIZE_FACTOR = 0.65         # characters smaller than this share of the page's median size are overlay, not text
BAND = 0.10                     # top/bottom share of the page searched for running headers and footers
SCRIPT_MIN_WORDS = 12           # each script needs this many words before a bilingual gutter is trusted
SCRIPT_SPLIT_MIN = 0.8          # share of Hindi/English words that must fall on opposite sides of the gutter
REPEAT_SHARE = 0.4              # a header/footer line appears on at least this share of pages ...
REPEAT_MIN_PAGES = 3            # ... and on at least this many
GUTTER_RANGE = (0.35, 0.65)     # where, as a share of page width, a column gutter may sit
MIN_GUTTER = 6.0                # pt: the empty strip between columns is at least this wide
MIN_BODY_WORDS = 40             # fewer words than this on a page and its layout is decided by the other pages
MIN_SIDE_SHARE = 0.2            # each column holds at least this share of the page's words
GUTTER_SNAP = 15.0              # pt: a page's gutter this close to the paper's median is moved onto it
SNAPSHOT_SCALE = 2.0            # render at 144 dpi
SNAPSHOT_PAD = 4.0              # pt of white around a snapshot
INLINE_ANSWERS_MIN = 3          # this many "Answer:" lines means the paper prints its answers under each question
MAX_QUESTION_NUMBER = 300

LAYOUTS = {"auto": "Auto-detect", "one": "One column", "two": "Two columns"}

# A heading on a line of its own, followed by the answers: "ANSWER KEY" / "Answers:" at the end of the paper.
KEY_HEADING = re.compile(r"^(?:final\s+|official\s+)?(?:answer\s*keys?|answers?(?:\s*sheet)?|key|solutions?)\s*[:\-–—]?\s*$", re.I)
KEY_MIN_PAIRS = 5                # the text after the heading must hold at least this many "number letter" pairs


class QuestionList(list):
    """The questions, plus the text of an answer-key block that was found after them (None if there wasn't one)."""
    key_block: str | None = None

START = re.compile(r"^(\d{1,3})[.)](?:\s+(.*))?$")
# "Answer: (c)", "Answer-(c)", "Ans– c". The letter must stand alone: "Answer: (db)" is text printed over text, and is
# reported as unreadable rather than guessed at.
ANSWER_MARK = re.compile(r"^Ans(?:wer)?\s*(?:[-–—:.]|\()", re.I)
ANSWER = re.compile(r"^Ans(?:wer)?\s*[-–—:.]*\s*(?:\(\s*([a-dA-D])\s*\)|([a-dA-D])\b)\s*[-–—:.]*\s*(.*)$", re.I)
OPTION = re.compile(r"\(\s*([a-dA-D])\s*\)")                     # any marker, either case: "is there an option here?"
# Options are (a)-(d) — or (A)-(D) in some papers. Never mixed: "(A)" and "(R)" in an Assertion-Reason question are labels.
OPTION_STYLES = (re.compile(r"\(\s*([a-d])\s*\)"), re.compile(r"\(\s*([A-D])\s*\)"))
PARAGRAPH_START = re.compile(
    r"^(?:(?:\d{1,2}|[A-Ea-e]|[ivxIVX]{1,4})[.)]\s|(?:List|Code|Codes|Statement|Statements|Assertion|Reason|Select|Which|"
    r"Consider|Options?|Pairs?|Note)\b)")
DEVANAGARI = re.compile(r"[ऀ-ॿ]")
TABLE_HINT = re.compile(r"\bList\s*-?\s*(?:I|II|1|2)\b", re.I)


# --------------------------------------------------------------------------- is there a text layer?

def _size_limit(sizes: list[float]) -> float:
    return max(WATERMARK_MIN_SIZE, WATERMARK_FACTOR * statistics.median(sizes)) if sizes else WATERMARK_MIN_SIZE


def _faint_color(color) -> bool:
    """True for grey overlay ink (emails, institute URLs drawn over the page), not black body text."""
    if color is None:
        return False
    if isinstance(color, (int, float)):
        return 0.05 < float(color) < 0.95
    if isinstance(color, (list, tuple)):
        if len(color) == 1:
            return 0.05 < float(color[0]) < 0.95
        if len(color) >= 3:
            r, g, b = (float(x) for x in color[:3])
            avg = (r + g + b) / 3
            return avg > 0.08 and (max(r, g, b) - min(r, g, b) < 0.15)
    return False


def _cleaned(page):
    """The page without watermarks: giant diagonal stamps, and the small grey overlays coaching PDFs print on top."""
    sizes = [c["size"] for c in page.chars]
    limit = _size_limit(sizes)
    tiny = (statistics.median(sizes) if sizes else 11.0) * TINY_SIZE_FACTOR

    def drop(o):
        if o.get("object_type") != "char":
            return False
        size = o.get("size", 0)
        return size > limit or size < tiny or _faint_color(o.get("non_stroking_color"))

    return page.filter(lambda o: not drop(o))


def _script_counts(text: str) -> tuple[int, int]:
    """(latin letters, Devanagari letters) in text — used to tell the English column from the Hindi one."""
    hi = len(DEVANAGARI.findall(text))
    en = sum(1 for ch in text if ch.isascii() and ch.isalpha())
    return en, hi


def script_gutter_from_words(words: list[dict], page_width: float) -> float | None:
    """x that splits a bilingual page (Hindi in one column, English in the other), or None if it isn't one.

    Geometric empty-strip gutters fail when a title or watermark sits in the gap; the two scripts still cluster."""
    hi_mids, en_mids = [], []
    for w in words:
        text = w.get("text") or ""
        mid = (w["x0"] + w["x1"]) / 2
        if DEVANAGARI.search(text):
            hi_mids.append(mid)
        elif any(ch.isascii() and ch.isalpha() for ch in text):
            en_mids.append(mid)
    if len(hi_mids) < SCRIPT_MIN_WORDS or len(en_mids) < SCRIPT_MIN_WORDS:
        return None
    lo, hi = int(min(hi_mids + en_mids)), int(max(hi_mids + en_mids))
    if hi - lo < MIN_GUTTER:
        return None
    best, winners = 0, []
    n = len(hi_mids) + len(en_mids)
    for x in range(lo, hi + 1, 2):
        en_right = sum(1 for m in en_mids if m >= x) + sum(1 for m in hi_mids if m < x)
        en_left = sum(1 for m in en_mids if m < x) + sum(1 for m in hi_mids if m >= x)
        score = max(en_right, en_left)
        if score > best:
            best, winners = score, [x]
        elif score == best:
            winners.append(x)
    if not winners or best / n < SCRIPT_SPLIT_MIN:
        return None
    best_x = winners[len(winners) // 2]          # middle of the gap, not the first x that already separates
    if not (0.25 * page_width < best_x < 0.75 * page_width):
        return None
    return float(best_x)


def has_text_layer(pdf_path: str) -> bool:
    """True if most pages carry real, readable text (a scan has none, or only a page number)."""
    with pdfplumber.open(pdf_path) as pdf:
        total = len(pdf.pages)
        if not total:
            return False
        with_text = 0
        for page in pdf.pages:
            limit = _size_limit([c["size"] for c in page.chars])
            if sum(1 for c in page.chars if c["size"] <= limit and c["text"].strip()) >= MIN_TEXT_CHARS:
                with_text += 1
        return with_text / total >= TEXT_PAGE_SHARE


# --------------------------------------------------------------------------- columns

def find_gutter(page, layout: str = "auto") -> float | None:
    """x of the middle of the gutter between two columns, or None if the page is read as one column.
    Uses the body of the page only, so a title or running header that spans both columns doesn't hide the gap."""
    if layout == "one":
        return None
    x0, top, x1, bottom = page.bbox
    height, width = bottom - top, x1 - x0
    words = [w for w in page.extract_words()
             if top + BAND * height <= (w["top"] + w["bottom"]) / 2 <= bottom - BAND * height]
    bilingual = script_gutter_from_words(words, width)
    if bilingual is not None:
        return bilingual
    if len(words) < MIN_BODY_WORDS:
        return None if layout == "auto" else x0 + width / 2
    lo, hi = int(x0 + GUTTER_RANGE[0] * width), int(x0 + GUTTER_RANGE[1] * width)
    covered = [False] * (hi - lo + 1)
    for w in words:
        for x in range(max(lo, int(w["x0"])), min(hi, int(w["x1"]) + 1) + 1):
            covered[x - lo] = True
    best, run_start = None, None
    for i, taken in enumerate(covered + [True]):
        if not taken and run_start is None:
            run_start = i
        elif taken and run_start is not None:
            if best is None or i - run_start > best[1] - best[0]:
                best = (run_start, i)
            run_start = None
    if best is None or best[1] - best[0] < MIN_GUTTER:
        return None if layout == "auto" else x0 + width / 2
    gutter = lo + (best[0] + best[1]) / 2
    left = sum(1 for w in words if (w["x0"] + w["x1"]) / 2 < gutter)
    if layout == "auto" and min(left, len(words) - left) < MIN_SIDE_SHARE * len(words):
        return None                                     # a margin note or a narrow table, not a second column
    return gutter


# --------------------------------------------------------------------------- reading the pages

def _read_lines(pdf, layout: str, on_progress=None):
    """All body lines of the paper in reading order: dicts with text, page (0-based), col, x0, x1, top, bottom.
    Also returns per-page geometry (bbox, gutter) for cropping snapshots."""
    pages = pdf.pages
    cleaned = [_cleaned(p) for p in pages]
    gutters = [find_gutter(c, layout) for c in cleaned]
    decided = [g for c, g in zip(cleaned, gutters) if len(c.extract_words()) >= MIN_BODY_WORDS]
    found = [g for g in gutters if g is not None]
    if found:
        # Pages of one paper share a gutter; snap each to it (so a header cut in two by the gutter is cut the same way on
        # every page and can be recognised as a repeat), and let sparse pages (a last page, a chapter end) borrow it.
        shared = statistics.median(found)
        two_column_paper = layout == "two" or len(found) / max(1, len(decided)) >= 0.5
        gutters = [shared if (g is not None and abs(g - shared) <= GUTTER_SNAP)
                   else g if g is not None
                   else shared if two_column_paper and len(c.extract_words()) < MIN_BODY_WORDS
                   else None
                   for c, g in zip(cleaned, gutters)]

    lines, geometry = [], []
    for pi, (page, gutter) in enumerate(zip(cleaned, gutters)):
        x0, top, x1, bottom = page.bbox
        geometry.append({"bbox": page.bbox, "gutter": gutter})
        columns = [(x0, x1)] if gutter is None else [(x0, gutter), (gutter, x1)]
        for ci, (cx0, cx1) in enumerate(columns):
            area = page.crop((cx0, top, cx1, bottom), strict=False)
            for line in area.extract_text_lines(strip=True, return_chars=False):
                text = re.sub(r"\s+", " ", line["text"].replace(" ", " ")).strip()
                if text:
                    lines.append({"text": text, "page": pi, "col": ci, "x0": line["x0"], "x1": line["x1"],
                                  "top": line["top"], "bottom": line["bottom"], "colx": (cx0, cx1)})
        if on_progress:
            on_progress(pi + 1, len(pages) * 2)
    return _prefer_english_columns(_drop_running_lines(lines, geometry)), geometry


def _prefer_english_columns(lines: list[dict]) -> list[dict]:
    """Keep only the English column on bilingual pages (Hindi left / English right, or the reverse).

    This app reads English from PDFs; Hindi arrives through JSON. Reading both columns concatenates the same
    question numbers and merges Hindi + English into one absurd block."""
    by_page: dict[int, list] = {}
    for ln in lines:
        by_page.setdefault(ln["page"], []).append(ln)
    kept: list[dict] = []
    for group in by_page.values():
        cols: dict[int, list] = {}
        for ln in group:
            cols.setdefault(ln["col"], []).append(ln)
        if len(cols) < 2:
            kept.extend(group)
            continue
        hindi_cols, english_cols = [], []
        for ci, lns in cols.items():
            en, hi = _script_counts(" ".join(ln["text"] for ln in lns))
            if hi > en and hi >= 20:
                hindi_cols.append(ci)
            elif en > hi and en >= 20:
                english_cols.append(ci)
        if hindi_cols and english_cols:
            keep_cols = set(english_cols)
            kept.extend(ln for ln in group if ln["col"] in keep_cols)
        else:
            kept.extend(group)
    return kept


def _drop_running_lines(lines: list[dict], geometry: list[dict]) -> list[dict]:
    """Remove page headers and footers: lines in the top/bottom band that repeat (ignoring digits) on many pages."""
    pages = len(geometry)
    if pages < REPEAT_MIN_PAGES:
        return lines

    def key(line):
        # Only a page NUMBER may differ between pages ("Page | 11", "- 4 -", or a bare "12"): a trailing or lone number.
        # Digits elsewhere stay, so "3. Consider ... item 3:" and "4. Consider ... item 4:" are not "the same line".
        return (line["col"], re.sub(r"\d+\s*[-–|]?\s*$", "#", line["text"]), round(line["top"] / 8))

    def in_band(line):
        _, top, _, bottom = geometry[line["page"]]["bbox"]
        return line["top"] < top + BAND * (bottom - top) or line["bottom"] > bottom - BAND * (bottom - top)

    seen: dict = {}
    for line in lines:
        if in_band(line):
            seen.setdefault(key(line), set()).add(line["page"])
    running = {k for k, on in seen.items() if len(on) >= max(REPEAT_MIN_PAGES, REPEAT_SHARE * pages)}

    def is_content(line):                     # never a header: a question start, an option line or an answer line
        return bool(START.match(line["text"]) or OPTION.search(line["text"]) or ANSWER_MARK.match(line["text"]))

    def overlay_text(text: str) -> bool:
        t = text.lower()
        return "@" in t or ".com" in t or "www." in t or bool(re.search(r"\d{8,}", text))

    overlay_seen: dict = {}
    for line in lines:
        if overlay_text(line["text"]):
            overlay_seen.setdefault(re.sub(r"\d+", "#", line["text"].lower()), set()).add(line["page"])
    overlays = {k for k, on in overlay_seen.items() if len(on) >= REPEAT_MIN_PAGES}

    def drop(ln):
        if is_content(ln):
            return False
        if in_band(ln) and key(ln) in running:
            return True
        if overlay_text(ln["text"]) and re.sub(r"\d+", "#", ln["text"].lower()) in overlays:
            return True
        return False

    return [ln for ln in lines if not drop(ln)]


# --------------------------------------------------------------------------- an answer key at the end of the paper

def split_key_block(lines: list[dict]):
    """(question lines, key text or None). A key block is the last standalone heading ("ANSWER KEY", "Answers:") that is followed
    by at least KEY_MIN_PAIRS pairs like "1-b" / "2. (d)". It is cut off so it can't leak into the last question's option (d)."""
    from app.key_parse import PAIR
    for i in range(len(lines) - 1, 0, -1):
        if KEY_HEADING.match(lines[i]["text"]):
            tail = "\n".join(ln["text"] for ln in lines[i + 1:])
            if len(PAIR.findall(tail)) >= KEY_MIN_PAIRS:
                return lines[:i], tail
    return lines, None


def find_key_block(pdf_path: str, layout: str = "auto") -> str | None:
    """The answer-key block at the end of a text-layer question paper, or None."""
    with pdfplumber.open(pdf_path) as pdf:
        lines, _ = _read_lines(pdf, layout if layout in LAYOUTS else "auto")
    return split_key_block(lines)[1]


# --------------------------------------------------------------------------- lines -> questions

def _paragraphs(lines: list[str]) -> str:
    """Wrapped lines are joined with spaces; a line that starts a list item, a 'Code:' row, 'Which ...' and the like
    begins a new paragraph, so statements and match-the-following rows stay one per line."""
    out: list[str] = []
    for text in lines:
        if out and not PARAGRAPH_START.match(text):
            out[-1] += " " + text
        else:
            out.append(text)
    return "\n".join(re.sub(r"[ \t]+", " ", p).strip() for p in out).strip()


def split_options(block: str):
    """(stem_text, [a, b, c, d]) using the LAST (a)(b)(c)(d) markers in order, or None if they aren't all there.
    Lower-case markers are tried first, then upper-case, so "(A)" / "(R)" labels inside an option are just text."""
    for style in OPTION_STYLES:
        marks = [(m.start(), m.end(), m.group(1).lower()) for m in style.finditer(block)]
        chosen, want = [], "d"
        for mark in reversed(marks):
            if mark[2] == want:
                chosen.append(mark)
                if want == "a":
                    break
                want = chr(ord(want) - 1)
        if len(chosen) < 4:
            continue
        chosen.reverse()
        options = []
        for k, (_, end, _) in enumerate(chosen):
            stop = chosen[k + 1][0] if k < 3 else len(block)
            options.append(re.sub(r"\s+", " ", block[end:stop]).strip())
        return block[: chosen[0][0]], options
    return None


def _looks_merged(lines: list[str]) -> bool:
    """True if two lines start with "(a)" — two sets of options, so two questions run together. Only line STARTS count:
    "Article 56 (1) (c)" in the middle of a sentence is not an option."""
    return sum(1 for text in lines if re.match(r"\(\s*a\s*\)|\(\s*A\s*\)", text)) > 1


def _has_options(lines: list[dict]) -> bool:
    block = "\n".join(ln["text"] for ln in lines)
    return any({m.group(1).lower() for m in style.finditer(block)} >= {"a", "b", "c", "d"} for style in OPTION_STYLES)


def cut_questions(lines: list[dict]):
    """Group the body lines into questions. Returns (questions, inline_mode) where each question is a dict with
    number, body (line dicts, before any answer), answer (letter or None), explanation (list of text lines)."""
    # Skip the cover page and instructions: begin at the first page that has answer options, at its first "1.".
    first_page = next((ln["page"] for ln in lines if OPTION.search(ln["text"])), None)
    if first_page is None:
        return [], False
    start = next((i for i, ln in enumerate(lines)
                  if ln["page"] >= first_page and (m := START.match(ln["text"])) and int(m.group(1)) == 1), None)
    if start is None:
        return [], False
    lines = lines[start:]
    inline = sum(1 for ln in lines if ANSWER_MARK.match(ln["text"])) >= INLINE_ANSWERS_MIN

    questions, current, expected = [], None, 1
    for ln in lines:
        text = ln["text"]
        m = START.match(text)
        n = int(m.group(1)) if m else None
        # After (a)–(d) the next numbered stem starts a question even if the printed number is wrong
        # ("25." when 27 was expected — a bilingual crop or a typesetting slip). Numbers 1–4 after a
        # finished question are still treated as statements, so match-the-following rows stay inside it.
        starts_question = (
            m is not None
            and (current is None or _has_options(current["body"]))
            and (
                n == expected
                or (current is not None and n > 4)
            )
        )
        if starts_question:
            if expected > MAX_QUESTION_NUMBER:
                break
            current = {"number": expected, "body": [], "answer": None, "answer_seen": False, "explanation": []}
            questions.append(current)
            expected += 1
            first = m.group(2)
            if first:
                current["body"].append({**ln, "text": first})
            continue
        if current is None:
            continue
        if inline and not current["answer_seen"] and ANSWER_MARK.match(text) and _has_options(current["body"]):
            current["answer_seen"] = True
            a = ANSWER.match(text)
            if a:
                current["answer"] = (a.group(1) or a.group(2)).upper()
                rest = a.group(3)
            else:                                           # the label is there but the letter isn't readable
                rest = re.sub(r"^Ans(?:wer)?\s*[-–—:.]*", "", text, count=1, flags=re.I).strip()
            if rest:
                current["explanation"].append(rest)
            continue
        if current["answer_seen"]:
            current["explanation"].append(text)
        else:
            current["body"].append(ln)
    return questions, inline


def _question_from(raw: dict, image_name: str | None) -> dict:
    block = "\n".join(ln["text"] for ln in raw["body"])
    split = split_options(block)
    if split:
        stem_text, options = split
        stem = _paragraphs([t for t in stem_text.split("\n") if t.strip()])
    else:
        stem, options = _paragraphs([ln["text"] for ln in raw["body"]]), None
    flags = sanity_flags(stem, options)
    combined = stem + " " + " ".join(options or [])
    if DEVANAGARI.search(combined):
        flags.append("hindi_text")
    if TABLE_HINT.search(stem):
        flags.append("check_table")
    if _looks_merged([ln["text"] for ln in raw["body"]]):
        flags.append("maybe_merged")
    opts = options or ["", "", "", ""]
    return {
        "question_number": raw["number"], "text": stem,
        "option_a": opts[0], "option_b": opts[1], "option_c": opts[2], "option_d": opts[3],
        "source_image": image_name, "flags": flags,
        "page_number": raw["body"][0]["page"] + 1 if raw["body"] else None,
        "answer": raw["answer"], "answer_seen": raw["answer_seen"],
        "explanation": _paragraphs(raw["explanation"]) or None,
    }


# --------------------------------------------------------------------------- snapshots

def _snapshot(pdf, geometry, raw: dict, images_dir: str, cache: dict) -> str | None:
    """A picture of the question and its options as printed — the answer and explanation are never included."""
    if not raw["body"]:
        return None
    parts = []
    by_area: dict = {}
    for ln in raw["body"]:
        by_area.setdefault((ln["page"], ln["col"]), []).append(ln)
    for (pi, _), group in by_area.items():
        if pi not in cache:
            if len(cache) > 3:
                cache.clear()
            cache[pi] = pdf.pages[pi].to_image(resolution=72 * SNAPSHOT_SCALE).original.convert("L")
        image = cache[pi]
        bx0, btop, _, _ = geometry[pi]["bbox"]
        cx0, cx1 = group[0]["colx"]
        box = (
            max(0, int((cx0 - bx0 - SNAPSHOT_PAD) * SNAPSHOT_SCALE)),
            max(0, int((min(g["top"] for g in group) - btop - SNAPSHOT_PAD) * SNAPSHOT_SCALE)),
            min(image.width, int((cx1 - bx0 + SNAPSHOT_PAD) * SNAPSHOT_SCALE)),
            min(image.height, int((max(g["bottom"] for g in group) - btop + SNAPSHOT_PAD) * SNAPSHOT_SCALE)),
        )
        if box[2] > box[0] and box[3] > box[1]:
            parts.append(image.crop(box))
    if not parts:
        return None
    sheet = Image.new("L", (max(p.width for p in parts), sum(p.height for p in parts) + 6 * (len(parts) - 1)), 255)
    y = 0
    for p in parts:
        sheet.paste(p, (0, y))
        y += p.height + 6
    name = f"q{raw['number']}.jpg"
    sheet.save(os.path.join(images_dir, name), quality=80)
    return name


# --------------------------------------------------------------------------- whole paper

def extract_questions(pdf_path: str, images_dir: str, on_progress=None, layout: str = "auto"):
    """Read a text-layer question paper. Returns (questions, warnings) like ocr_extract.extract_questions(), with two
    extra keys per question when the paper prints its answers inline: `answer` ('A'-'D' or None) and `explanation`.
    on_progress(pages_done, pages_total) is called as pages are read and again as snapshots are made."""
    if layout not in LAYOUTS:
        layout = "auto"
    os.makedirs(images_dir, exist_ok=True)
    with pdfplumber.open(pdf_path) as pdf:
        lines, geometry = _read_lines(pdf, layout, on_progress)
        lines, key_block = split_key_block(lines)
        raw_questions, inline = cut_questions(lines)
        questions, cache = [], {}
        for i, raw in enumerate(raw_questions):
            questions.append(_question_from(raw, _snapshot(pdf, geometry, raw, images_dir, cache)))
            if on_progress and (i % 10 == 9 or i == len(raw_questions) - 1):
                on_progress(len(pdf.pages) + round(len(pdf.pages) * (i + 1) / len(raw_questions)), len(pdf.pages) * 2)

    warnings = []
    questions = QuestionList(questions)
    questions.key_block = key_block
    if not questions:
        warnings.append("No questions were found in the text of this PDF. Try the layout option (one or two columns), "
                        "or check that this is the right file.")
        return questions, warnings
    if inline:
        for q in questions:
            if not q["answer_seen"]:
                q["flags"].append("no_answer_found")
            elif q["answer"] is None:
                q["flags"].append("answer_unclear")
    numbers = {q["question_number"] for q in questions}
    missing = sorted(set(range(1, max(numbers) + 1)) - numbers)
    if missing:
        warnings.append(f"Question numbers not detected: {', '.join(map(str, missing))}.")
    merged = [q["question_number"] for q in questions if "maybe_merged" in q["flags"]]
    if merged:
        warnings.append("Question(s) " + ", ".join(map(str, merged[:15])) + " look like two questions merged into one — "
                        "split them on the review page.")
    incomplete = [q["question_number"] for q in questions if "options_not_found" in q["flags"]]
    if incomplete:
        warnings.append("Options (a)-(d) weren't found for question(s) " + ", ".join(map(str, incomplete[:15])) +
                        (" …" if len(incomplete) > 15 else "") + ". Later questions may have been merged into them.")
    return questions, warnings
