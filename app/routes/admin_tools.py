"""Admin tools: the audit log and database backups."""
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy.orm import Session

from app import audit, backup, models
from app.database import get_db
from app.web import flash, require_admin, templates

router = APIRouter(dependencies=[Depends(require_admin)])

AUDIT_PAGE_SIZE = 200


@router.get("/admin/audit")
def audit_log(
    request: Request, action: str = "", paper_id: int | None = None, page: int = 1, db: Session = Depends(get_db)
):
    query = db.query(models.AuditLog).order_by(models.AuditLog.id.desc())
    if action.strip():
        query = query.filter(models.AuditLog.action.like(action.strip() + "%"))
    if paper_id:
        query = query.filter(models.AuditLog.paper_id == paper_id)
    page = max(1, page)
    rows = query.offset((page - 1) * AUDIT_PAGE_SIZE).limit(AUDIT_PAGE_SIZE + 1).all()
    has_more = len(rows) > AUDIT_PAGE_SIZE
    entries = []
    for r in rows[:AUDIT_PAGE_SIZE]:
        try:
            detail = json.loads(r.detail_json) if r.detail_json else None
        except ValueError:
            detail = r.detail_json
        entries.append({"row": r, "detail": detail})
    return templates.TemplateResponse(
        "audit.html",
        {"request": request, "entries": entries, "action": action, "paper_id": paper_id,
         "page": page, "has_more": has_more},
    )


@router.get("/admin/backups")
def backups_page(request: Request):
    return templates.TemplateResponse(
        "backups.html",
        {"request": request, "backups": backup.list_backups(), "keep": backup.AUTO_KEEP,
         "flash": request.session.pop("flash", None)},
    )


@router.post("/admin/backups/create")
def create_backup(request: Request, db: Session = Depends(get_db)):
    name = backup.create_backup("manual")
    audit.log(db, request.state.user, "backup.create", detail={"file": name})
    db.commit()
    flash(request, f"Backup created: {name}", "notice")
    return RedirectResponse(url="/admin/backups", status_code=303)


@router.get("/admin/backups/{name}")
def download_backup(request: Request, name: str, db: Session = Depends(get_db)):
    path = backup.backup_path(name)
    if not path:
        raise HTTPException(status_code=404, detail="Backup not found")
    audit.log(db, request.state.user, "backup.download", detail={"file": name})
    db.commit()
    return FileResponse(path, media_type="application/octet-stream", filename=name)
