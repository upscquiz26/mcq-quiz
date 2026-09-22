"""
Database backups, using SQLite's online backup API (safe while the app is running).

Files go to data/backups/ as  <label>_<YYYYmmdd_HHMMSS>.db :
  auto_*    made automatically before every bulk import; only the newest AUTO_KEEP are kept
  manual_*  made from the Backups page; never deleted by the app

This backs up the database only. The uploaded PDFs and page images live under
data/pdfs and data/images — copy the whole data/ folder for a complete backup.
"""
import os
import re
import sqlite3
from datetime import datetime

from app.database import DATA_DIR, DB_PATH

BACKUP_DIR = os.path.join(DATA_DIR, "backups")
AUTO_KEEP = 20
_NAME_RE = re.compile(r"^[a-z0-9-]+_\d{8}_\d{6}(_\d+)?\.db$")


def create_backup(label: str = "manual") -> str:
    """Copies the database into data/backups/ and returns the file name."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    label = re.sub(r"[^a-z0-9-]+", "-", label.lower()).strip("-") or "backup"
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    name, n = f"{label}_{stamp}.db", 1
    while os.path.exists(os.path.join(BACKUP_DIR, name)):   # two backups within one second
        n += 1
        name = f"{label}_{stamp}_{n}.db"

    src = sqlite3.connect(DB_PATH)
    dst = sqlite3.connect(os.path.join(BACKUP_DIR, name))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    _prune_auto()
    return name


def list_backups() -> list[dict]:
    if not os.path.isdir(BACKUP_DIR):
        return []
    rows = []
    for name in os.listdir(BACKUP_DIR):
        if not _NAME_RE.match(name):
            continue
        st = os.stat(os.path.join(BACKUP_DIR, name))
        rows.append({
            "name": name,
            "size": st.st_size,
            "created": datetime.fromtimestamp(st.st_mtime),
            "kind": name.split("_", 1)[0],
        })
    return sorted(rows, key=lambda r: (r["created"], r["name"]), reverse=True)


def backup_path(name: str) -> str | None:
    """Full path for a backup file name, or None if the name isn't a real backup (blocks path tricks)."""
    if not _NAME_RE.match(name or ""):
        return None
    path = os.path.join(BACKUP_DIR, name)
    return path if os.path.isfile(path) else None


def _prune_auto():
    autos = [b for b in list_backups() if b["kind"] == "auto"]
    for old in autos[AUTO_KEEP:]:
        try:
            os.remove(os.path.join(BACKUP_DIR, old["name"]))
        except OSError:
            pass
