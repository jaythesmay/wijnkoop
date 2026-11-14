"""A copy of the database every night, in /data/backups.

Uses SQLite's VACUUM INTO: a clean, compact copy that is safe to make while runs and the web
pages use the database (it only reads). Keeps the last 7 days and one copy a week for the 4 weeks
before that. Your uploads, watch list, order list and choices are in it; the shops can always be
read again, but those cannot.
"""
import datetime as dt
import logging
import os
import re
import sqlite3
from pathlib import Path

from . import config

log = logging.getLogger("wijnkoop")

BACKUP_DIR = config.DATA_DIR / "backups"
BACKUP_HOUR = int(os.environ.get("WIJNKOOP_BACKUP_HOUR", "4"))  # 04:00 at night
KEEP_DAYS = 7
KEEP_WEEKS = 4
NAME = re.compile(r"^wijnkoop-(\d{4}-\d{2}-\d{2})\.db$")


def _dated(folder: Path) -> list[tuple[dt.date, Path]]:
    out = []
    for f in folder.glob("wijnkoop-*.db") if folder.exists() else []:
        m = NAME.match(f.name)
        if m:
            out.append((dt.date.fromisoformat(m.group(1)), f))
    return sorted(out, reverse=True)


def backup_due(now: dt.datetime, folder: Path | None = None) -> bool:
    folder = folder or BACKUP_DIR
    if now.hour < BACKUP_HOUR:
        return False
    return not (folder / f"wijnkoop-{now.date().isoformat()}.db").exists()


def make_backup(con, today: dt.date | None = None, folder: Path | None = None) -> Path:
    folder = folder or BACKUP_DIR
    folder.mkdir(parents=True, exist_ok=True)
    today = today or dt.date.today()
    target = folder / f"wijnkoop-{today.isoformat()}.db"
    tmp = target.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    con.execute("VACUUM INTO ?", (str(tmp),))
    os.replace(tmp, target)
    removed = rotate(folder, today)
    log.info("Backup saved: %s (%.0f MB)%s", target.name, target.stat().st_size / 1e6,
             f"; {len(removed)} old backups removed" if removed else "")
    return target


def rotate(folder: Path, today: dt.date) -> list[Path]:
    """Keep the copies of the last KEEP_DAYS days, and the newest copy of each of the KEEP_WEEKS
    weeks before that. Everything older goes."""
    keep, weeks = set(), set()
    for day, f in _dated(folder):
        age = (today - day).days
        if age < KEEP_DAYS:
            keep.add(f)
            continue
        week = day.isocalendar()[:2]
        if week not in weeks and len(weeks) < KEEP_WEEKS:
            weeks.add(week)
            keep.add(f)
    removed = []
    for _, f in _dated(folder):
        if f not in keep:
            f.unlink(missing_ok=True)
            removed.append(f)
    return removed


def run_if_due(con, now: dt.datetime | None = None) -> Path | None:
    now = now or dt.datetime.now()
    if not backup_due(now):
        return None
    try:
        return make_backup(con, now.date())
    except (sqlite3.Error, OSError):
        log.exception("Backup failed; trying again in the next round")
        return None
