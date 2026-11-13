"""Reading shops on request from the app: what each shop's last run was, whether a new one makes
sense, which runs are going on, and starting one in the background.

A run started from the app is a separate process ("python -m wijnkoop collect --job N"), so the web
pages stay quick and the run goes on when the page is closed. The weekly scheduler and the app
never read the same shop at the same time: every shop run notes which process does it, and a shop
whose process is still alive counts as running.
"""
import datetime as dt
import os
import signal
import subprocess
import sys
import threading

from . import config
from .process import is_alive, process_token
from .shops import SHOPS

FRESH_HOURS = 48  # read less than two days ago: a new run finds little that is new
SOON_HOURS = 12  # the weekly run starts within this many hours: better to wait for it

# --- status ----------------------------------------------------------------------------------

def _next_weekly(now: dt.datetime) -> dt.datetime:
    days = (config.RUN_WEEKDAY - now.weekday()) % 7
    slot = (now + dt.timedelta(days=days)).replace(hour=config.RUN_HOUR, minute=0, second=0, microsecond=0)
    return slot if slot > now else slot + dt.timedelta(days=7)


def _ago(then: dt.datetime, now: dt.datetime) -> str:
    hours = (now - then).total_seconds() / 3600
    if hours < 1:
        return "less than an hour ago"
    if hours < 36:
        return f"{round(hours)} hour{'s' if round(hours) != 1 else ''} ago"
    days = round(hours / 24)
    return f"{days} days ago"


def live_jobs(con) -> list[dict]:
    """Runs started from the app that are still going, or were asked for a moment ago."""
    recent = (dt.datetime.now() - dt.timedelta(minutes=2)).isoformat(timespec="seconds")
    return [dict(r) for r in con.execute("SELECT * FROM app_job WHERE finished IS NULL ORDER BY id")
            if is_alive(r["process"]) or (not r["process"] and r["requested"] >= recent)]


def close_dead_jobs(con):
    """Runs that ended without saying so (a container restart, the stop button)."""
    alive = {j["id"] for j in live_jobs(con)}
    for r in con.execute("SELECT id FROM app_job WHERE finished IS NULL").fetchall():
        if r["id"] not in alive:
            con.execute("UPDATE app_job SET finished=?, note=COALESCE(note, 'stopped') WHERE id=?",
                        (dt.datetime.now().isoformat(timespec="seconds"), r["id"]))


def running_shops(con) -> dict[str, dict]:
    """{shop key: its unfinished run} for every shop a living process is reading now."""
    out = {}
    for r in con.execute("SELECT * FROM run_log WHERE finished IS NULL AND process IS NOT NULL ORDER BY id"):
        if r["job"] in SHOPS and is_alive(r["process"]):
            out[r["job"]] = dict(r)
    return out


def shop_status(con, now: dt.datetime | None = None) -> dict:
    now = now or dt.datetime.now()
    jobs = live_jobs(con)
    running = running_shops(con)
    queued = {}
    for j in jobs:
        for key in j["shops"].split(","):
            done = con.execute("SELECT 1 FROM run_log WHERE job=? AND finished >= ? LIMIT 1",
                               (key, j["requested"])).fetchone()
            if key not in running and not done:
                queued.setdefault(key, j["id"])
    nxt = _next_weekly(now)
    soon = (nxt - now).total_seconds() < SOON_HOURS * 3600
    shops = []
    for key, module in SHOPS.items():
        last = con.execute("""SELECT * FROM run_log WHERE job=? AND finished IS NOT NULL
                              ORDER BY finished DESC LIMIT 1""", (key,)).fetchone()
        broke_off = con.execute("""SELECT * FROM run_log WHERE job=? AND finished IS NULL
                                   ORDER BY id DESC LIMIT 1""", (key,)).fetchone()
        wines = con.execute("""SELECT COUNT(*) FROM offer o JOIN shop s ON s.id = o.shop_id
                               WHERE s.name=? AND o.is_current=1 AND o.gone_since IS NULL""",
                            (module.SHOP_NAME,)).fetchone()[0]
        s = {"key": key, "name": module.SHOP_NAME, "wines": wines, "running": running.get(key),
             "queued": key in queued, "last": dict(last) if last else None,
             "broke_off": dict(broke_off) if broke_off and key not in running else None,
             "browser": bool(getattr(module, "NEEDS_BROWSER", False))}
        finished = dt.datetime.fromisoformat(last["finished"]) if last else None
        if s["running"]:
            s["state"], s["advice"] = "running", s["running"]["note"] or "starting"
        elif s["queued"]:
            s["state"], s["advice"] = "queued", "waiting for the shop before it in the same run"
        elif s["broke_off"] and s["broke_off"]["started"] > (finished.isoformat() if finished else ""):
            s["state"] = "useful"
            s["advice"] = (f"the last run stopped ({s['broke_off']['note'] or 'before the first page'}); "
                           "a new run carries on from there")
        elif not finished:
            s["state"], s["advice"] = "useful", "a first run reads all its wines"
        elif (now - finished).total_seconds() < FRESH_HOURS * 3600:
            s["state"] = "fresh"
            s["advice"] = f"read {_ago(finished, now)}: a new run now will find little that is new"
        elif soon:
            s["state"] = "soon"
            s["advice"] = f"read {_ago(finished, now)}; the weekly run starts {nxt:%A %H:%M} anyway"
        else:
            s["state"], s["advice"] = "useful", f"read {_ago(finished, now)}"
        s["last_text"] = f"{finished:%a %d %b %H:%M}" if finished else "never"
        shops.append(s)
    return {"shops": shops, "jobs": jobs, "next_weekly": nxt, "weekly_soon": soon,
            "useful": [s["key"] for s in shops if s["state"] == "useful"]}


# --- starting and stopping -------------------------------------------------------------------

def request_run(con, keys: list[str], force: bool = False) -> dict:
    """Note a run of these shops and say which ones it will read. Shops that are running or waiting
    in another run are left out; so are shops for which a run makes no sense, unless forced."""
    close_dead_jobs(con)
    status = {s["key"]: s for s in shop_status(con)["shops"]}
    keys = [k for k in dict.fromkeys(keys) if k in status]
    busy = [k for k in keys if status[k]["state"] in ("running", "queued")]
    skipped = [k for k in keys if k not in busy and status[k]["state"] != "useful" and not force]
    todo = [k for k in keys if k not in busy and k not in skipped]
    job_id = None
    if todo:
        job_id = con.execute("INSERT INTO app_job (shops, requested) VALUES (?, ?)",
                             (",".join(todo), dt.datetime.now().isoformat(timespec="seconds"))).lastrowid
    con.commit()
    return {"job_id": job_id, "shops": todo, "busy": busy, "skipped": skipped}


def launch(job_id: int) -> subprocess.Popen:
    """Start the run of an app job as its own process; its log goes to the container's log."""
    proc = subprocess.Popen([sys.executable, "-m", "wijnkoop", "collect", "--job", str(job_id)],
                            start_new_session=True, stdin=subprocess.DEVNULL)
    threading.Thread(target=proc.wait, daemon=True).start()  # clear it away when it ends
    return proc


def stop_job(con, job_id: int) -> bool:
    """Stop a run started from the app. The shop's run stays unfinished, so the next run carries on."""
    r = con.execute("SELECT process FROM app_job WHERE id=? AND finished IS NULL", (job_id,)).fetchone()
    if not r or not is_alive(r["process"]):
        return False
    pid = int(r["process"].split(":")[0])
    try:
        os.killpg(pid, signal.SIGTERM)  # the run and its headless browser
    except (ProcessLookupError, PermissionError):
        return False
    con.execute("UPDATE app_job SET finished=?, note='stopped' WHERE id=?",
                (dt.datetime.now().isoformat(timespec="seconds"), job_id))
    con.execute("UPDATE run_log SET process = NULL WHERE finished IS NULL AND process = ?", (r["process"],))
    con.commit()
    return True


def run_job(con, job_id: int, collect) -> list[str]:
    """Inside the job's own process: read its shops one after the other. A shop that another
    process started reading in the meantime is left to that process."""
    from .awards.importer import match_awards

    me = process_token()
    con.execute("UPDATE app_job SET process=? WHERE id=?", (me, job_id))
    con.commit()
    keys = con.execute("SELECT shops FROM app_job WHERE id=?", (job_id,)).fetchone()["shops"].split(",")
    done, notes = [], []
    for key in keys:
        if key in running_shops(con):
            notes.append(f"{key} already running elsewhere")
            continue
        try:
            collect(SHOPS[key], con=con)
            done.append(key)
        except Exception as exc:  # one shop that fails must not stop the others
            con.rollback()
            notes.append(f"{key} failed: {exc}")
    if done:
        match_awards(con)
    con.execute("UPDATE app_job SET finished=?, note=? WHERE id=?",
                (dt.datetime.now().isoformat(timespec="seconds"),
                 "; ".join(notes) or f"{len(done)} shop{'s' if len(done) != 1 else ''} read", job_id))
    con.commit()
    return done
