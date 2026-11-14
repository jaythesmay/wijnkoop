"""Reading shops on request from the app: what each shop's last run was, whether a new one makes
sense, which runs are going on, and starting one in the background.

A run started from the app is a separate process ("python -m wijnkoop collect --job N"), so the web
pages stay quick and the run goes on when the page is closed. The weekly scheduler and the app
never read the same shop at the same time: every shop run notes which process does it, and a shop
whose process is still alive counts as running.
"""
import datetime as dt
import json
import os
import signal
import random
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import config, pace
from .process import is_alive, process_token
from .shops import SHOPS

FRESH_HOURS = 48  # read less than two days ago: a new run finds little that is new
SOON_HOURS = 12  # the weekly run starts within this many hours: better to wait for it
# A NAS has little power: by default at most 2 shops are read at the same time, and at most 1 of
# them with the headless browser (Wijnvoordeel, Wijnbeurs), which is the heavy part. The others wait.
DEFAULT_MAX_RUNS = 2
DEFAULT_MAX_BROWSER_RUNS = 1
NICE = int(os.environ.get("WIJNKOOP_NICE", "10"))  # runs give way to the web pages


def _setting(con, key, default):
    try:
        row = con.execute("SELECT value FROM setting WHERE key=?", (key,)).fetchone()
        return int(row[0]) if row and str(row[0]).isdigit() else default
    except Exception:
        return default


def _limits_file(con):
    """limits.json next to the database. The limits are kept in a file, not in the database, so
    "Save" is instant even while a shop run is saving its scores and holds the database."""
    try:
        path = con.execute("PRAGMA database_list").fetchone()[2]
    except Exception:
        return None
    return Path(path).with_name("limits.json") if path else None


def limits(con) -> dict:
    out = {"max_runs": _setting(con, "max_runs", DEFAULT_MAX_RUNS),
           "max_browser_runs": _setting(con, "max_browser_runs", DEFAULT_MAX_BROWSER_RUNS)}
    path = _limits_file(con)
    try:
        saved = json.loads(path.read_text()) if path and path.exists() else {}
        out.update({k: int(v) for k, v in saved.items() if k in out and str(v).isdigit()})
    except (OSError, ValueError):
        pass
    return {k: max(1, v) for k, v in out.items()}


def set_limits(con, max_runs, max_browser_runs) -> dict:
    """Save the limits. Waiting runs pick them up within 30 seconds. Touches no database table
    when the database is a file, so it never waits for a run that is saving."""
    new = limits(con)
    for key, value in (("max_runs", max_runs), ("max_browser_runs", max_browser_runs)):
        if str(value).isdigit() and 1 <= int(value) <= 10:
            new[key] = int(value)
    path = _limits_file(con)
    if path:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(new))
        os.replace(tmp, path)
    else:  # a database in memory (tests)
        for key, value in new.items():
            con.execute("INSERT OR REPLACE INTO setting (key, value) VALUES (?, ?)", (key, str(value)))
        con.commit()
    return new


def needs_browser(key: str) -> bool:
    return bool(getattr(SHOPS[key], "NEEDS_BROWSER", False))


def slot_free(con, key: str) -> bool:
    """Whether this shop may start now without going over the limits."""
    lim = limits(con)
    others = [k for k in running_shops(con) if k != key]
    if len(others) >= lim["max_runs"]:
        return False
    return not needs_browser(key) or sum(map(needs_browser, others)) < lim["max_browser_runs"]


def wait_for_slot(con, key: str, job_id: int | None = None, poll: float = 30, say=None) -> None:
    """Wait until this shop may start. Two runs that wait both look again after a random moment,
    so they do not start together."""
    waited = False
    while True:
        if slot_free(con, key):
            if not waited:
                return
            time.sleep(random.uniform(1, 8))
            if slot_free(con, key):
                break
        if not waited:
            waited = True
            if job_id:
                con.execute("UPDATE app_job SET note='waiting for a free place' WHERE id=?", (job_id,))
                con.commit()
            if say:
                say(f"{SHOPS[key].SHOP_NAME}: waiting until fewer shops are being read")
        time.sleep(poll)
    if job_id:
        con.execute("UPDATE app_job SET note=NULL WHERE id=?", (job_id,))
        con.commit()


def be_nice():
    """Runs get a lower priority than the web pages, so the app stays quick while they work."""
    try:
        os.nice(NICE)
    except (AttributeError, OSError):
        pass

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


RESUME_DAYS = 8  # a run that broke off longer ago is not carried on: a new run reads everything


def close_dead_jobs(con):
    """Runs asked for that never started. A run whose process died (a container restart or an
    update) is not closed: the scheduler starts it again (resume_dead_jobs)."""
    alive = {j["id"] for j in live_jobs(con)}
    for r in con.execute("SELECT id, process FROM app_job WHERE finished IS NULL").fetchall():
        if r["id"] not in alive and not r["process"]:
            con.execute("UPDATE app_job SET finished=?, note=COALESCE(note, 'did not start') WHERE id=?",
                        (dt.datetime.now().isoformat(timespec="seconds"), r["id"]))


def dead_jobs(con) -> list[dict]:
    """Runs started from the app whose process died before they were done."""
    return [dict(r) for r in con.execute("SELECT * FROM app_job WHERE finished IS NULL AND process IS NOT NULL")
            if not is_alive(r["process"])]


def resume_dead_jobs(con, start=None) -> list[int]:
    """Start the runs from the app again that a restart broke off. Each carries on where it stopped
    and skips the shops it had already read. Called by the scheduler."""
    start = start or launch
    ids = []
    for j in dead_jobs(con):
        if j["requested"] < (dt.datetime.now() - dt.timedelta(days=RESUME_DAYS)).isoformat(timespec="seconds"):
            con.execute("UPDATE app_job SET finished=?, note='broke off too long ago' WHERE id=?",
                        (dt.datetime.now().isoformat(timespec="seconds"), j["id"]))
            continue
        con.execute("UPDATE app_job SET process=NULL, note='carrying on after a restart' WHERE id=?", (j["id"],))
        con.commit()
        start(j["id"])
        ids.append(j["id"])
    con.commit()
    return ids


def paused_run(con, key: str, now: dt.datetime | None = None) -> dict | None:
    """The shop's unfinished run that waits because the shop answered "too many requests"."""
    now = now or dt.datetime.now()
    r = con.execute("""SELECT * FROM run_log WHERE job=? AND finished IS NULL AND wait_until > ?
                       ORDER BY id DESC LIMIT 1""", (key, now.isoformat(timespec="seconds"))).fetchone()
    return dict(r) if r and not is_alive(r["process"]) else None


def resumable_run(con, key: str, now: dt.datetime | None = None) -> dict | None:
    """The shop's run that broke off (its process is gone) and will be carried on by itself:
    newer than its last finished run, less than RESUME_DAYS old, and not stopped by you."""
    now = now or dt.datetime.now()
    last = con.execute("SELECT MAX(finished) FROM run_log WHERE job=? AND finished IS NOT NULL", (key,)).fetchone()[0]
    since = (now - dt.timedelta(days=RESUME_DAYS)).isoformat(timespec="seconds")
    for r in con.execute("""SELECT * FROM run_log WHERE job=? AND finished IS NULL AND started >= ?
                            ORDER BY started DESC""", (key, since)):
        if is_alive(r["process"]):
            return None  # running
        if (r["note"] or "").startswith("stopped by you"):
            return None
        if r["wait_until"] and r["wait_until"] > now.isoformat(timespec="seconds"):
            return None  # paused: the shop asked us to wait
        if last and r["started"] < last:
            continue  # a later run read the shop
        return dict(r)
    return None


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
        shown = s["broke_off"] or s["last"]
        s["failures"] = [dict(r) for r in con.execute(
            """SELECT reason, COUNT(*) AS n, MIN(url) AS example FROM failed_page WHERE run_id=?
               GROUP BY reason ORDER BY n DESC LIMIT 3""", (shown["id"],))] if shown else []
        finished = dt.datetime.fromisoformat(last["finished"]) if last else None
        resumable = resumable_run(con, key, now) if not s["running"] else None
        paused = paused_run(con, key, now) if not s["running"] else None
        s["pace"] = pace.describe(con, module)
        from .health import warning

        s["warning"] = warning(con, key)
        if s["running"]:
            s["state"], s["advice"] = "running", s["running"]["note"] or "starting"
        elif paused:  # also inside a run of several shops: that run went on with the next shop
            until = dt.datetime.fromisoformat(paused["wait_until"])
            s["state"] = "paused"
            s["advice"] = (f"the shop answered “too many requests” after {paused['pages_ok'] or 0} pages; "
                           f"carries on by itself {until:%a %d %b %H:%M}, one page every {s['pace']['now']:.0f} s")
        elif resumable and not s["queued"]:
            s["state"] = "resuming"
            s["advice"] = (f"broke off {resumable['started'][:16].replace('T', ' ')} after {resumable['pages_ok'] or 0} "
                           "pages (a restart or update); carries on by itself within 10 minutes")
        elif s["queued"]:
            note = next((j["note"] for j in jobs if j["id"] == queued[key]), None)
            s["state"] = "queued"
            s["advice"] = ("waiting until fewer shops are being read" if note == "waiting for a free place"
                           else "waiting for the shop before it in the same run")
        elif s["broke_off"] and s["broke_off"]["started"] > (finished.isoformat() if finished else ""):
            s["state"] = "useful"
            stopped = (s["broke_off"]["note"] or "").startswith("stopped by you")
            s["advice"] = (f"{'you stopped the last run' if stopped else 'the last run broke off'} after "
                           f"{s['broke_off']['pages_ok'] or 0} pages; "
                           + ("“Carry on” goes on from there" if s["broke_off"]["started"] >=
                              (now - dt.timedelta(days=RESUME_DAYS)).isoformat(timespec="seconds")
                              else "too long ago to carry on: a new run reads everything"))
            s["carry_on"] = True
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
    in_jobs = {k for j in jobs for k in j["shops"].split(",")}
    scheduler_runs = [s for s in shops if s["state"] == "running" and s["key"] not in in_jobs]
    return {"shops": shops, "jobs": jobs, "next_weekly": nxt, "weekly_soon": soon, "limits": limits(con),
            "useful": [s["key"] for s in shops if s["state"] == "useful"],
            "scheduler_runs": scheduler_runs, "resuming": [s for s in shops if s["state"] == "resuming"],
            "paused": [s for s in shops if s["state"] == "paused"],
            "paused_alone": [s for s in shops if s["state"] == "paused" and s["key"] not in in_jobs],
            "dead_jobs": dead_jobs(con)}


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
    con.execute("UPDATE run_log SET process = NULL, note = 'stopped by you' WHERE finished IS NULL AND process = ?",
                (r["process"],))
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
    requested = con.execute("SELECT requested FROM app_job WHERE id=?", (job_id,)).fetchone()["requested"]
    done, notes = [], []
    for key in keys:
        if con.execute("SELECT 1 FROM run_log WHERE job=? AND finished >= ? LIMIT 1", (key, requested)).fetchone():
            continue  # read already, before a restart broke this run off
        if key in running_shops(con):
            notes.append(f"{key} already running elsewhere")
            continue
        wait_for_slot(con, key, job_id)
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
