"""
Append-only audit trail: who changed what, and when.

    audit.log(db, request.state.user, "question.edit", "question", q.id, paper_id=q.paper_id,
              detail={"fields": ["text", "option_b"]})

The row is added to the caller's session and committed with the caller's change,
so the log and the change can never disagree. Pass user=None for background work.
Anything that looks like a secret is scrubbed before it is stored.
"""
import json
import re

from app import models

_SECRETISH = re.compile(r"pass(word|wd)?|secret|token|api[_-]?key|cookie|authorization|credential", re.I)
_MAX_DETAIL = 4000


def _scrub(value):
    if isinstance(value, dict):
        return {k: ("[hidden]" if _SECRETISH.search(str(k)) else _scrub(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub(v) for v in value]
    return value


def log(db, user, action: str, entity_type: str | None = None, entity_id: int | None = None,
        paper_id: int | None = None, detail: dict | None = None) -> models.AuditLog:
    text = None
    if detail:
        text = json.dumps(_scrub(detail), default=str, ensure_ascii=False)
        if len(text) > _MAX_DETAIL:
            text = text[:_MAX_DETAIL] + "…"
    row = models.AuditLog(
        user_id=user.id if user else None,
        username=user.username if user else None,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        paper_id=paper_id,
        detail_json=text,
    )
    db.add(row)
    return row
