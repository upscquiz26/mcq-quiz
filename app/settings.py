"""
Admin-controlled switches, stored in the app_settings table.

    settings.get_bool(db, "leaderboard_enabled")
    settings.set_bool(db, "leaderboard_enabled", False, user)     # caller commits

Unknown keys are refused, so a typo can't silently create a setting.
"""
from datetime import datetime

from app import models

DEFAULTS = {
    "leaderboard_enabled": True,          # students can see leaderboards
    "user_performance_enabled": True,     # the admin can see students' practice results (students are told)
}


def get_bool(db, key: str) -> bool:
    if key not in DEFAULTS:
        raise KeyError(f"Unknown setting: {key}")
    row = db.get(models.AppSetting, key)
    if row is None:
        return DEFAULTS[key]
    return row.value == "1"


def set_bool(db, key: str, value: bool, user=None) -> None:
    if key not in DEFAULTS:
        raise KeyError(f"Unknown setting: {key}")
    row = db.get(models.AppSetting, key)
    if row is None:
        row = models.AppSetting(key=key, value="1" if value else "0")
        db.add(row)
    row.value = "1" if value else "0"
    row.updated_at = datetime.utcnow()
    row.updated_by = user.id if user else None
