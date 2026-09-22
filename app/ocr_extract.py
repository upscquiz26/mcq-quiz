"""
Question paper PDF -> structured questions, with no AI service involved.

Many coaching-institute papers are image-only PDFs (no text layer), so the
text has to be read with OCR. This uses Tesseract, a local open-source OCR
engine: https://github.com/UB-Mannheim/tesseract/wiki (Windows installer).
Set TESSERACT_CMD if it isn't on PATH or in the default install folder.

Layout this is tuned for: two-column bilingual pages (English on the left,
Hindi on the right), question numbers in the left margin, options "(a)".."(d)".
Only the left column is read. The constants below describe that layout.

How it works, per page:
  1. Render the page and crop the English column; threshold away the grey watermark.
  2. OCR a thin strip along the left edge on its own -> question numbers and
     their vertical positions. (Bold numbers get dropped by whole-page OCR.)
  3. OCR the column body line by line, with positions.
  4. Give each line to the question whose number sits above it. Text at the top
     of a page, above the first number, continues the previous page's question.
Then each question's text is split into stem + options a-d, sanity-checked,
and a snapshot image of the question as printed is saved for the review screen.
"""
import csv
import io
import os
import re
import shutil
import subprocess
import tempfile

import pypdfium2 as pdfium
from PIL import Image, ImageOps

RENDER_SCALE = 4.0                      # PDF points -> pixels; OCR needs the resolution
COLUMN = (0.03, 0.063, 0.495, 0.945)    # left, top, right, bottom of the English column, as page fractions
BORDER = 40                             # white padding around the column (helps Tesseract)
NUMBER_STRIP_W = 150                    # px, border included: question numbers live here, list items don't
INK_THRESHOLD = 150                     # pixels lighter than this are dropped (kills the grey watermark)
PARAGRAPH_GAP = 100                     # px between line tops that counts as a paragraph break
LINE_OWNER_TOLERANCE = 25               # px: a line this close above a number still belongs to it
MAX_QUESTION_NUMBER = 300
SNAPSHOT_SCALE = 0.5                    # snapshots are stored at half OCR resolution
MAX_OPTION_LENGTH = 300


class OcrUnavailable(RuntimeError):
    pass


def tesseract_cmd() -> str:
    candidates = [
        os.environ.get("TESSERACT_CMD"),
        shutil.which("tesseract"),
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    raise OcrUnavailable(
        "Tesseract OCR is not installed. Install it (Windows: `winget install UB-Mannheim.TesseractOCR`) "
        "or set TESSERACT_CMD to tesseract.exe, then restart the app."
    )


class _Tesseract:
    def __init__(self, tmpdir: str):
        self.cmd = tesseract_cmd()
        self.tmp = os.path.join(tmpdir, "ocr_input.png")

    def words(self, img: Image.Image, psm: int, extra=()):
        img.save(self.tmp)
        r = subprocess.run(
            [self.cmd, self.tmp, "stdout", "-l", "eng", "--psm", str(psm), *extra, "tsv"],
            capture_output=True, text=True, encoding="utf-8",
        )
        rows = list(csv.reader(io.StringIO(r.stdout), delimiter="\t", quoting=csv.QUOTE_NONE))
        if not rows:
            return []
        header = rows[0]
        out = []
        for row in rows[1:]:
            if len(row) != len(header):
                continue
            w = dict(zip(header, row))
            if w["level"] == "5" and w["text"].strip():
                out.append(w)
        return out


# --------------------------------------------------------------------------
# Page level
# --------------------------------------------------------------------------

def _page_images(page):
    """Returns (grey column for snapshots, thresholded column for OCR), both with the same border."""
    img = page.render(scale=RENDER_SCALE).to_pil().convert("L")
    w, h = img.size
    l, t, r, b = COLUMN
    col = img.crop((int(w * l), int(h * t), int(w * r), int(h * b)))
    col = ImageOps.expand(col, border=BORDER, fill=255)
    ink = col.point(lambda p: 255 if p > INK_THRESHOLD else 0)
    return col, ink


def _ocr_page(tess: _Tesseract, ink: Image.Image):
    """Returns (numbers, lines). numbers: [(top, bottom, n)], lines: [(top, bottom, text)], both sorted by top."""
    W, H = ink.size
    strip = ink.crop((0, 0, NUMBER_STRIP_W, H))
    numbers = []
    for w in tess.words(strip, 6, ("-c", "tessedit_char_whitelist=0123456789.")):
        m = re.fullmatch(r"(\d{1,3})\.?", w["text"].strip())
        if m and float(w["conf"]) > 30 and 1 <= int(m.group(1)) <= MAX_QUESTION_NUMBER:
            top = int(w["top"])
            numbers.append((top, top + int(w["height"]), int(m.group(1))))
    numbers.sort()

    grouped = {}
    for w in tess.words(ink, 6):
        grouped.setdefault((w["block_num"], w["par_num"], w["line_num"]), []).append(w)
    lines = []
    for ws in grouped.values():
        ws.sort(key=lambda w: int(w["left"]))
        # A number token in the margin is the strip pass's job; don't repeat it in the text.
        if re.fullmatch(r"\d{1,3}[.,]?", ws[0]["text"]) and int(ws[0]["left"]) < NUMBER_STRIP_W:
            ws = ws[1:]
        if ws:
            top = min(int(w["top"]) for w in ws)
            bottom = max(int(w["top"]) + int(w["height"]) for w in ws)
            lines.append((top, bottom, " ".join(w["text"] for w in ws)))
    lines.sort()
    return numbers, lines


# --------------------------------------------------------------------------
# Splitting a question's text into stem + options, and sanity checks
# --------------------------------------------------------------------------

# "(a)".."(d)", tolerating OCR noise like "(J)", "(dj)", "(ad)", "(a}", "(4)".
# Lowercase only, so "(A)" / "(R)" inside assertion-reason text aren't mistaken for markers.
OPTION_MARKER = re.compile(r"\(\s*[a-dJj4][a-z|1]?\s*[)\]}]")
_ODD_CHARS = re.compile(r"[^\w\s.,;:'\"’‘“”()\[\]/%&+\-–—?!=°$₹*<>]")


def _tidy(s: str) -> str:
    s = re.sub(r"\s+", " ", s.replace("¶", " ")).strip()
    s = re.sub(r"\bl(?=\s?and\s?\d)", "1", s)      # "land4"  -> "1 and 4"
    s = re.sub(r"(?<=\d)(and)(?=\d)", r" \1 ", s)   # "2and3"  -> "2 and 3"
    s = re.sub(r"\b1(and)\b", r"1 \1", s)
    s = re.sub(r"(?<=\d)and\b", " and", s)
    return re.sub(r"\s+", " ", s)


def split_question(lines: list[str]):
    """lines: text lines of one question ('¶' marks a paragraph break).
    Returns (stem, [a, b, c, d]) or None if four option markers can't be found."""
    block = "\n".join(lines)
    found = list(OPTION_MARKER.finditer(block))
    if len(found) < 4:
        return None
    pos = found[-4:]                       # options are always the last four markers, in a-b-c-d order
    tail = block[pos[3].end():].split("¶")[0]   # option (d) ends at the first big vertical gap
    stem = _tidy(block[: pos[0].start()])
    stem = re.sub(r"^\d{1,3}[.,]\s*", "", stem)  # stray leading number
    options = [_tidy(block[m.end(): pos[k + 1].start()]) for k, m in enumerate(pos[:3])]
    options.append(_tidy(tail))
    return stem, options


def sanity_flags(stem: str, options: list[str] | None) -> list[str]:
    if options is None:
        return ["options_not_found"]
    flags = []
    if not stem:
        flags.append("empty_stem")
    if any(not o for o in options):
        flags.append("empty_option")
    if any(len(o) > MAX_OPTION_LENGTH for o in options):
        flags.append("very_long_option")
    if _ODD_CHARS.search(stem + " " + " ".join(options)):
        flags.append("odd_characters")
    # Match-the-following code rows ("2 1 4 3") must each be a permutation of 1-4.
    rows = [o.split() for o in options]
    if all(len(r) == 4 and all(len(t) == 1 and t.isalnum() for t in r) for r in rows):
        if any(sorted(r) != ["1", "2", "3", "4"] for r in rows):
            flags.append("check_code_row")
    return flags


# --------------------------------------------------------------------------
# Whole paper
# --------------------------------------------------------------------------

def extract_questions(pdf_path: str, images_dir: str, on_progress=None):
    """
    OCR a question paper. Saves a snapshot JPEG per question into images_dir.
    Returns (questions, warnings); each question is a dict with
    question_number, text, option_a..d, source_image, flags (list of str).
    on_progress(pages_done, pages_total) is called after every page.
    """
    os.makedirs(images_dir, exist_ok=True)
    raw_lines: dict[int, list[str]] = {}       # question number -> text lines
    pieces: dict[int, list[Image.Image]] = {}  # question number -> snapshot pieces (one per page it spans)
    first_page: dict[int, int] = {}            # question number -> the page (1-based) where it starts

    pdf = pdfium.PdfDocument(pdf_path)
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            _read_pages(pdf, _Tesseract(tmpdir), raw_lines, pieces, on_progress, first_page)
    finally:
        pdf.close()   # Windows won't let the file be deleted while PDFium still holds it open
    return _build_questions(raw_lines, pieces, images_dir, first_page)


def _read_pages(pdf, tess, raw_lines, pieces, on_progress, first_page=None):
    total = len(pdf)
    current = None    # the question that may continue onto the next page
    for pi in range(total):
        grey, ink = _page_images(pdf[pi])
        numbers, lines = _ocr_page(tess, ink)

        # Cover pages, instructions and back covers have no answer options; skip them.
        if sum(len(OPTION_MARKER.findall(text)) for _, _, text in lines) < 2:
            if on_progress:
                on_progress(pi + 1, total)
            continue

        extents: dict[int, list[int]] = {}   # question -> [top, bottom] on this page
        prev_top = prev_owner = None
        for top, bottom, text in lines:
            owner = current
            for ntop, _, n in numbers:
                if ntop <= top + LINE_OWNER_TOLERANCE:
                    owner = n
            if owner is None:
                continue
            current = owner
            if first_page is not None:
                first_page.setdefault(owner, pi + 1)
            bucket = raw_lines.setdefault(owner, [])
            if bucket and prev_owner == owner and prev_top is not None and top - prev_top > PARAGRAPH_GAP:
                bucket.append("¶")
            bucket.append(text)
            prev_top, prev_owner = top, owner
            ext = extents.setdefault(owner, [top, bottom])
            ext[0], ext[1] = min(ext[0], top), max(ext[1], bottom)

        for ntop, _, n in numbers:     # the number itself belongs to the snapshot
            if n in extents:
                extents[n][0] = min(extents[n][0], ntop)
        W, H = grey.size
        for n, (top, bottom) in extents.items():
            crop = grey.crop((0, max(0, top - 25), W, min(H, bottom + 25)))
            size = (int(crop.width * SNAPSHOT_SCALE), int(crop.height * SNAPSHOT_SCALE))
            pieces.setdefault(n, []).append(crop.resize(size, Image.LANCZOS))

        if on_progress:
            on_progress(pi + 1, total)


def _build_questions(raw_lines, pieces, images_dir, first_page=None):
    questions = []
    for n in sorted(raw_lines):
        split = split_question(raw_lines[n])
        if split:
            stem, options = split
        else:
            stem, options = _tidy("\n".join(raw_lines[n])), None

        parts = pieces.get(n, [])
        image_name = None
        if parts:
            sheet = Image.new("L", (max(p.width for p in parts), sum(p.height for p in parts) + 6 * (len(parts) - 1)), 255)
            y = 0
            for p in parts:
                sheet.paste(p, (0, y))
                y += p.height + 6
            image_name = f"q{n}.jpg"
            sheet.save(os.path.join(images_dir, image_name), quality=80)

        opts = options or ["", "", "", ""]
        questions.append({
            "question_number": n,
            "text": stem,
            "option_a": opts[0], "option_b": opts[1], "option_c": opts[2], "option_d": opts[3],
            "source_image": image_name,
            "flags": sanity_flags(stem, options),
            "page_number": (first_page or {}).get(n),
        })

    warnings = []
    if not questions:
        warnings.append("No questions were found. Is this the right PDF, and is it laid out as two columns "
                        "with English on the left?")
    else:
        numbers_found = {q["question_number"] for q in questions}
        missing = sorted(set(range(1, max(numbers_found) + 1)) - numbers_found)
        if missing:
            warnings.append(f"Question numbers not detected: {', '.join(map(str, missing))}. "
                            "They may have been merged into the previous question.")
    return questions, warnings
