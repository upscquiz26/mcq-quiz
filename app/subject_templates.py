"""Saved subject-range templates ("1-30 History, 31-60 Geography"), tied to a booklet series or usable with any."""
from app import ingest, models


def all_templates(db) -> list[models.SubjectTemplate]:
    return db.query(models.SubjectTemplate).order_by(models.SubjectTemplate.series.is_(None), models.SubjectTemplate.series,
                                                     models.SubjectTemplate.name).all()


def label(t: models.SubjectTemplate) -> str:
    return f"{t.name} (series {t.series})" if t.series else f"{t.name} (any series)"


def fits(t: models.SubjectTemplate, series: str | None) -> bool:
    """A series-less template fits every paper; a series one fits a paper of that series (or a paper whose series isn't set yet)."""
    return not t.series or not series or t.series == series.strip().upper()[:1]


def get(db, template_id) -> models.SubjectTemplate | None:
    try:
        return db.get(models.SubjectTemplate, int(template_id))
    except (TypeError, ValueError):
        return None


def ranges_to_use(db, typed: str, template_id, series: str | None) -> tuple[str, models.SubjectTemplate | None]:
    """The range text to apply: what was typed if anything, otherwise the chosen template. Raises ValueError with a readable
    message if the template is gone or belongs to a different series."""
    if (typed or "").strip():
        return typed, None
    if not str(template_id or "").strip():
        return "", None
    t = get(db, template_id)
    if not t:
        raise ValueError("That saved template no longer exists.")
    if not fits(t, series):
        raise ValueError(f"“{t.name}” is for series {t.series}, but this paper is series {series}.")
    return t.ranges_text, t


def save(db, user, name: str, series: str, ranges_text: str, subject_names: list[str]) -> models.SubjectTemplate:
    """Validates and stores a template. Saving under a name+series that exists replaces its ranges. Raises ValueError."""
    name = (name or "").strip()
    series = (series or "").strip().upper()[:1] or None
    if not name:
        raise ValueError("Give the template a name.")
    if series and series not in "ABCD":
        raise ValueError("Series must be A, B, C or D — or leave it empty for any series.")
    ranges = ingest.parse_subject_ranges(ranges_text, subject_names)
    if not ranges:
        raise ValueError("Enter at least one range to save, e.g. 1-30 History.")
    text = ", ".join(f"{a}-{b} {s}" for a, b, s in ranges)
    existing = (db.query(models.SubjectTemplate)
                .filter(models.SubjectTemplate.name.ilike(name), models.SubjectTemplate.series.is_(series) if series is None
                        else models.SubjectTemplate.series == series).first())
    if existing:
        existing.ranges_text = text
        return existing
    t = models.SubjectTemplate(name=name, series=series, ranges_text=text, created_by=user.id if user else None)
    db.add(t)
    db.flush()
    return t
