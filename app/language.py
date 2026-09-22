"""
English and Hindi versions of a question.

Hindi only ever arrives through the JSON import (never from a PDF): `question_hi`, `option_a_hi` … `option_d_hi`, `explanation_hi`.
A question can have English only, Hindi only, or both, but never neither. A Hindi-only question keeps "" in the English columns
(those can't be null), so "has English" always means "the English text is not empty".
"""
import re

DEVANAGARI = re.compile(r"[ऀ-ॿ]")
_NOT_LETTERS = re.compile(r"[।-९]")            # danda, double danda, Devanagari digits: neither script's letters
MIN_LETTERS = 4                                          # fewer letters than this say nothing about the script
HINDI_MIN_SHARE = 0.6                                    # Hindi fields: at least this share of their letters must be Devanagari
ENGLISH_MAX_SHARE = 0.2                                  # English fields: at most this share

LETTERS = ("a", "b", "c", "d")


def devanagari_share(text: str) -> float | None:
    """Share of the letters in `text` that are Devanagari (0..1), or None when there are too few letters to tell."""
    letters = [ch for ch in text or "" if (ch.isalpha() or "ऀ" <= ch <= "ॿ") and not _NOT_LETTERS.match(ch)]
    if len(letters) < MIN_LETTERS:
        return None
    return sum(1 for ch in letters if "ऀ" <= ch <= "ॿ") / len(letters)


def english_options(q) -> list[str]:
    return [(getattr(q, f"option_{k}") or "") for k in LETTERS]


def hindi_options(q) -> list[str]:
    return [(getattr(q, f"option_{k}_hi") or "") for k in LETTERS]


def has_english(q) -> bool:
    return bool((q.text or "").strip())


def has_hindi(q) -> bool:
    return bool((q.question_hi or "").strip()) or any(o.strip() for o in hindi_options(q))


def primary(q) -> tuple[str, str, list[str]]:
    """(language, question text, four options) of the version used for matching and comparing: English when the question has it,
    otherwise Hindi. Copies of a question are only compared in the same language."""
    if has_english(q) or not has_hindi(q):
        return "en", q.text or "", english_options(q)
    return "hi", q.question_hi or "", hindi_options(q)


PREFS = ("en", "hi", "both")
PREF_LABELS = {"en": "English", "hi": "हिन्दी", "both": "Both"}


def pref_of(user) -> str:
    """The language a student reads in: 'en', 'hi' or 'both' (English for anyone without a valid setting)."""
    value = getattr(user, "language", None)
    return value if value in PREFS else "en"


def available(q) -> tuple[bool, bool]:
    """(has a usable English version, has a usable Hindi version). Usable means question text and all four options; when neither language
    is that complete, whatever text a language has counts, so a question always shows something."""
    en_full = has_english(q) and all(o.strip() for o in english_options(q))
    hi_full = bool((q.question_hi or "").strip()) and all(o.strip() for o in hindi_options(q))
    if not en_full and not hi_full:
        return has_english(q), has_hindi(q)
    return en_full, hi_full


def _visible(en: bool, hi: bool) -> dict:
    """In which display modes each element is shown. The rules:
         English mode  -> the English version; if the question has no English, the Hindi one, with a note
         Hindi mode    -> the Hindi version;   if the question has no Hindi, the English one, with a note
         Both mode     -> every version there is, English first; nothing is added for a missing one."""
    english = [m for m in PREFS if (m in ("en", "both") and en) or (m == "hi" and not hi and en)]
    hindi = [m for m in PREFS if (m in ("hi", "both") and hi) or (m == "en" and not en and hi)]
    return {
        "en": " ".join(english), "hi": " ".join(hindi),
        "note_hi": "hi" if en and not hi else "",       # Hindi was asked for and the question only has English
        "note_en": "en" if hi and not en else "",
    }


def modes(q) -> dict:
    """What a student sees for a question in each display mode, as space-separated mode lists for the page to switch on without reloading:
    text_en / text_hi (question and options), expl_en / expl_hi (explanations, which may exist in a different language set than the
    question), note_hi / note_en (the 'not available' notes) and their explanation counterparts."""
    en, hi = available(q)
    text = _visible(en, hi)
    expl = _visible(bool((q.explanation or "").strip()), bool((q.explanation_hi or "").strip()))
    return {
        "text_en": text["en"], "text_hi": text["hi"], "note_hi": text["note_hi"], "note_en": text["note_en"],
        "expl_en": expl["en"], "expl_hi": expl["hi"], "expl_note_hi": expl["note_hi"], "expl_note_en": expl["note_en"],
        "has_hi": has_hindi(q), "has_both": has_english(q) and has_hindi(q),
    }


def list_text(q, pref: str) -> str:
    """One line of question text for a list (results, revision, bookmarks): the student's language, falling back to the other one."""
    en, hi = (q.text or "").strip(), (q.question_hi or "").strip()
    if pref == "hi":
        return hi or en
    return en or hi


def explanation_label_hi(q) -> str:
    """How far to trust a Hindi explanation. It only ever comes from a JSON import, so unless an admin verified it, it is AI-supplied."""
    return "Verified" if q.explanation_hi_status == "verified" else "AI-supplied, unverified"


FLAGS = ("language_incomplete", "language_swapped", "language_mismatch")


def _stem_lines(text: str | None) -> int:
    return sum(1 for line in (text or "").splitlines() if line.strip())


def compute_flags(q) -> list[str]:
    """The language warnings a question deserves, worked out from its current text. Not a translation check: only structure and script.

      * language_incomplete — a language has its question text without any options (or options without text) while the other one is complete;
      * language_swapped    — the Hindi fields aren't mostly Devanagari, or the English fields are;
      * language_mismatch   — both languages are present but don't correspond in structure: a different number of options, or of
                              statement lines in the question."""
    flags = []
    en_text, hi_text = bool((q.text or "").strip()), bool((q.question_hi or "").strip())
    en_opts, hi_opts = [o for o in english_options(q) if o.strip()], [o for o in hindi_options(q) if o.strip()]
    complete_en, complete_hi = en_text and len(en_opts) == 4, hi_text and len(hi_opts) == 4
    for has_text, has_options, other_complete in ((en_text, bool(en_opts), complete_hi), (hi_text, bool(hi_opts), complete_en)):
        if has_text != has_options and other_complete and "language_incomplete" not in flags:
            flags.append("language_incomplete")
    hindi_share = devanagari_share(" ".join([q.question_hi or "", *hi_opts, q.explanation_hi or ""]))
    english_share = devanagari_share(" ".join([q.text or "", *en_opts, q.explanation or ""]))
    if (hindi_share is not None and hindi_share < HINDI_MIN_SHARE) or (english_share is not None and english_share > ENGLISH_MAX_SHARE):
        flags.append("language_swapped")
    if has_english(q) and has_hindi(q):
        if (en_opts and hi_opts and len(en_opts) != len(hi_opts)) or (en_text and hi_text and _stem_lines(q.text) != _stem_lines(q.question_hi)):
            flags.append("language_mismatch")
    return flags


def refresh_flags(q) -> list[str]:
    """Bring the question's language warnings up to date (other warnings are left alone). A confirmed question that newly gets one must be
    looked at again, so its warnings count as unacknowledged. Returns the language flags it now carries."""
    current = [f for f in (q.ocr_flags or "").split(",") if f]
    wanted = compute_flags(q)
    kept = [f for f in current if f not in FLAGS]
    added = [f for f in wanted if f not in current]
    q.ocr_flags = ",".join(kept + wanted) or None
    if added and q.status in ("verified", "live"):
        q.flags_acknowledged = False
    return wanted


def which(q) -> str:
    """'both', 'en', 'hi' — which languages a question has (a question with neither counts as English)."""
    en, hi = has_english(q), has_hindi(q)
    return "both" if en and hi else ("hi" if hi and not en else "en")
