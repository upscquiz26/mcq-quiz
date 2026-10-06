"""
Keyword suggestions for a question's subject.

A suggestion is only a hint: it is stored in Question.suggested_subject_id, separately from the confirmed subject_id, and is
never applied by itself. The admin sees it beside the subject box and can accept it (one question, or many at once) or ignore it.

Scoring: each distinct keyword found in the question text or options scores 1 (a multi-word phrase scores 2). A subject is suggested
only when its score is at least MIN_SCORE and clearly ahead of the runner-up — a question that could be about two subjects gets no
suggestion rather than a coin toss. "Current Affairs" and "Other" have no keywords: nothing in the wording reliably identifies them.
"""
import re

from app import models

MIN_SCORE = 2

KEYWORDS: dict[str, list[str]] = {
    "Polity": [
        "constitution", "constitutional", "article", "amendment", "parliament", "lok sabha", "rajya sabha", "president", "governor",
        "supreme court", "high court", "fundamental rights", "fundamental duties", "directive principles", "preamble", "schedule",
        "election commission", "panchayat", "municipality", "federal", "judiciary", "legislature", "ordinance", "writ", "bill",
        "speaker", "prime minister", "cabinet", "attorney general", "comptroller", "finance commission", "citizenship", "emergency",
        "union list", "state list", "concurrent list", "impeachment", "cag", "upsc", "no-confidence", "money bill", "quorum",
    ],
    "History": [
        "mughal", "maurya", "gupta", "harappan", "indus valley", "vedic", "buddhism", "jainism", "british", "east india company",
        "viceroy", "governor-general", "revolt of 1857", "freedom struggle", "congress session", "gandhi", "non-cooperation",
        "civil disobedience", "quit india", "sultanate", "vijayanagara", "maratha", "akbar", "ashoka", "chola", "pallava", "chalukya",
        "medieval", "ancient india", "colonial", "swadeshi", "moderates", "extremists", "partition", "cabinet mission", "battle of",
        "dynasty", "inscription", "monolith", "stupa", "temple architecture", "bhakti", "sufi", "renaissance", "revolution",
    ],
    "Geography": [
        "river", "mountain", "plateau", "monsoon", "climate", "soil", "latitude", "longitude", "himalaya", "western ghats", "eastern ghats",
        "delta", "ocean", "tropic", "equator", "earthquake", "volcano", "glacier", "rainfall", "peninsular", "strait",
        "lake", "gulf", "island", "cyclone", "tectonic", "sediment", "crop", "irrigation", "mineral", "coalfield", "national highway",
        "tributary", "basin", "atmosphere", "continent", "erosion", "weathering", "tides", "biome", "desert", "coast",
    ],
    "Economy": [
        "gdp", "inflation", "rbi", "reserve bank", "fiscal", "monetary", "budget", "tax", "gst", "subsidy", "repo rate", "banking",
        "npa", "sebi", "stock market", "balance of payments", "current account", "exchange rate", "poverty line", "planning commission",
        "niti aayog", "five year plan", "public debt", "deficit", "wto", "imf", "world bank", "msp", "fdi", "microfinance", "nabard",
        "insurance", "unemployment", "per capita", "national income", "bond", "liquidity", "crr", "slr",
    ],
    "Environment": [
        "biodiversity", "ecosystem", "wildlife", "national park", "sanctuary", "biosphere reserve", "climate change", "greenhouse",
        "ozone", "carbon", "pollution", "conservation", "endangered", "iucn", "ramsar", "wetland", "mangrove", "forest", "species",
        "cites", "unfccc", "kyoto", "paris agreement", "emission", "tiger reserve", "invasive", "food chain", "habitat", "coral",
        "eutrophication", "biofuel", "renewable", "sustainable", "red data book", "endemic", "flora", "fauna", "ecology",
    ],
    "Science & Tech": [
        "isro", "satellite", "vaccine", "dna", "rna", "gene", "genetically modified", "nanotechnology", "laser", "quantum", "semiconductor",
        "artificial intelligence", "blockchain", "cyber", "software", "internet", "5g", "missile", "nuclear", "atomic", "radiation",
        "enzyme", "protein", "vitamin", "bacteria", "virus", "antibiotic", "photosynthesis", "chromosome", "genome", "biotechnology",
        "space mission", "rocket", "launch vehicle", "gravitational", "electromagnetic", "optical fibre", "3d printing", "robot", "drone",
        "acid", "alloy", "isotope", "crispr", "stem cell", "mrna", "telescope", "orbit",
    ],
    "CSAT": [
        "passage", "the author", "according to the passage", "ratio", "percentage", "per cent", "average", "probability", "speed",
        "train", "hcf", "lcm", "profit", "loss", "simple interest", "compound interest", "arrangement", "seating", "syllogism",
        "sequence", "distance", "work and time", "pipe", "cistern", "boat", "stream", "permutation", "combination", "clock", "calendar",
        "blood relation", "coding", "decoding", "venn", "age of", "mixture", "data interpretation", "inference", "assumption", "conclusion",
    ],
}

_PATTERNS: dict[str, list[tuple[re.Pattern, int]]] = {
    subject: [(re.compile(r"(?<![a-z0-9])" + re.escape(word) + r"(?![a-z0-9])", re.I), 2 if " " in word else 1) for word in words]
    for subject, words in KEYWORDS.items()
}


def scores(text: str) -> dict[str, int]:
    return {subject: sum(weight for pattern, weight in patterns if pattern.search(text)) for subject, patterns in _PATTERNS.items()}


def suggest(text: str) -> tuple[str, int] | None:
    """(subject name, score) for the subject these words point to, or None when nothing is clear enough."""
    ranked = sorted(scores(text).items(), key=lambda item: item[1], reverse=True)
    (best, top), (_, second) = ranked[0], ranked[1]
    if top >= MIN_SCORE and top > second:
        return best, top
    return None


def question_text(q: models.Question) -> str:
    return " ".join(part for part in (q.text, q.option_a, q.option_b, q.option_c, q.option_d, q.option_e) if part)


def suggest_for_paper(db, paper_id: int, redo: bool = False) -> int:
    """Store a suggestion on every question of the paper that has no subject yet. Questions that already have one are left alone.
    With redo=False a suggestion already stored is kept; with redo=True every suggestion is worked out again.
    Returns the number of questions that now carry a (new or changed) suggestion."""
    ids = {s.name: s.id for s in db.query(models.Subject).all()}
    changed = 0
    for q in db.query(models.Question).filter(models.Question.paper_id == paper_id).all():
        if q.subject_id or q.status == models.QStatus.QUARANTINED:
            if q.subject_id and q.suggested_subject_id:
                q.suggested_subject_id = None                  # decided already: the hint is no longer needed
            continue
        if q.suggested_subject_id and not redo:
            continue
        found = suggest(question_text(q))
        new = ids.get(found[0]) if found else None
        if new != q.suggested_subject_id:
            q.suggested_subject_id = new
            if new:
                changed += 1
    return changed
