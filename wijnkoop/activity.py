"""What WijnKoop is doing right now: its processes (CPU, memory, disk reads and writes), the
database and disk, and the work it is on (shop runs, uploads, award checks, scores).

Everything is read from Linux's /proc and /sys inside the container, and from the database
without writing to it. CPU and disk speeds need two looks: the Sampler keeps the previous one, so
the page, which asks every few seconds, shows what happened since its last question.
"""
import datetime as dt
import os
import re
import shutil
import threading
import time
from pathlib import Path

from . import config

PROC = Path("/proc")
CGROUP = Path("/sys/fs/cgroup")
HZ = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100


def _read(path: Path) -> str:
    try:
        return path.read_text()
    except OSError:
        return ""


def _int(text: str) -> int | None:
    text = text.strip()
    return int(text) if text.isdigit() else None


# ---------------------------------------------------------------- processes

def what_is(cmd: list[str]) -> tuple[str, str]:
    """(group, name) for a command line: the web app, the scheduler, a shop run, the browser."""
    line = " ".join(cmd)
    if re.search(r"chrom(e|ium)|headless_shell", line, re.I):
        return "browser", "Headless browser (for shops that need one)"
    if "datasette" in line:
        return "web", "Web app (pages, uploads, score updates started from the app)"
    m = re.search(r"-m wijnkoop (\S+)(?:.*--job (\d+))?", line)
    if m and m.group(1) == "scheduler":
        return "scheduler", "Weekly scheduler (shop runs, award checks, Vivino, e-mail)"
    if m and m.group(1) == "collect":
        return "job", f"Shop run started from the app (run {m.group(2)})" if m.group(2) else "Shop run"
    if m:
        return "job", f"WijnKoop: {m.group(1)}"
    if re.search(r"\bpython", line):
        return "other", "Python"
    return "other", (Path(cmd[0]).name if cmd else "?")


def read_processes() -> dict[int, dict]:
    out = {}
    for d in PROC.iterdir():
        if not d.name.isdigit():
            continue
        pid = int(d.name)
        raw = _read(d / "cmdline")
        if not raw:
            continue  # kernel threads, or gone
        cmd = [c for c in raw.split("\0") if c]
        stat = _read(d / "stat")
        if not stat:
            continue
        fields = stat[stat.rfind(")") + 2:].split()
        status = _read(d / "status")
        rss = re.search(r"VmRSS:\s+(\d+)\s+kB", status)
        io = dict(re.findall(r"(\w+): (\d+)", _read(d / "io")))
        group, name = what_is(cmd)
        out[pid] = {
            "pid": pid, "ppid": int(fields[1]), "group": group, "name": name,
            "command": " ".join(cmd)[:200], "ticks": int(fields[11]) + int(fields[12]),
            "started_ticks": int(fields[19]), "rss": int(rss.group(1)) * 1024 if rss else 0,
            "read": int(io.get("read_bytes", 0)), "write": int(io.get("write_bytes", 0)),
            "io_ok": bool(io),
        }
    return out


def _boot_time() -> float:
    m = re.search(r"btime (\d+)", _read(PROC / "stat"))
    return float(m.group(1)) if m else time.time()


# ---------------------------------------------------------------- the machine

def machine() -> dict:
    load = _read(PROC / "loadavg").split()
    cpus = os.cpu_count() or 1
    quota = _read(CGROUP / "cpu.max").split()
    if len(quota) == 2 and quota[0].isdigit():
        cpus = min(cpus, round(int(quota[0]) / int(quota[1]), 1))
    mem_used = _int(_read(CGROUP / "memory.current"))
    mem_max = _int(_read(CGROUP / "memory.max"))
    meminfo = dict(re.findall(r"(\w+):\s+(\d+) kB", _read(PROC / "meminfo")))
    if not mem_max and meminfo.get("MemTotal"):
        mem_max = int(meminfo["MemTotal"]) * 1024
    if mem_used is None and meminfo.get("MemTotal"):
        mem_used = (int(meminfo["MemTotal"]) - int(meminfo.get("MemAvailable", 0))) * 1024
    disk = shutil.disk_usage(config.DATA_DIR) if config.DATA_DIR.exists() else None
    return {"cpus": cpus, "load": [float(x) for x in load[:3]] if load else None,
            "memory_used": mem_used, "memory_max": mem_max,
            "disk_free": disk.free if disk else None, "disk_total": disk.total if disk else None}


def files() -> dict:
    """The database and the folders WijnKoop writes to, with their sizes and last change."""
    data = config.DATA_DIR
    out = []
    for name, label in (("wijnkoop.db", "Database"), ("wijnkoop.db-wal", "Database changes not yet merged (WAL)"),
                        ("uploads", "Uploaded results files"), ("samples", "Saved sample pages")):
        p = data / name
        if not p.exists():
            continue
        if p.is_dir():
            sizes = [(f.stat().st_size, f.stat().st_mtime) for f in p.rglob("*") if f.is_file()]
            size, changed = sum(s for s, _ in sizes), max((m for _, m in sizes), default=p.stat().st_mtime)
        else:
            st = p.stat()
            size, changed = st.st_size, st.st_mtime
        out.append({"name": name, "label": label, "size": size, "changed": changed})
    return {"folder": str(data), "files": out}


# ---------------------------------------------------------------- the work, from the database

JOB_NAMES = {"awards": "Award check", "vivino": "Vivino ratings", "mail": "Weekly e-mail"}


def job_name(job: str) -> str:
    from .shops import SHOPS

    return SHOPS[job].SHOP_NAME + ": shop run" if job in SHOPS else JOB_NAMES.get(job, job)


def work(con, now: dt.datetime | None = None) -> dict:
    from .process import is_alive

    now = now or dt.datetime.now()
    running = []
    for r in con.execute("SELECT * FROM run_log WHERE finished IS NULL ORDER BY id DESC LIMIT 20"):
        if r["process"] and not is_alive(r["process"]):
            continue  # broke off earlier; not running now
        started = dt.datetime.fromisoformat(r["started"])
        if not r["process"] and (now - started).total_seconds() > 6 * 3600:
            continue
        running.append({"job": job_name(r["job"]), "started": r["started"], "for": _duration((now - started).total_seconds()),
                        "pages_ok": r["pages_ok"], "pages_failed": r["pages_failed"], "note": r["note"],
                        "pid": int(r["process"].split(":")[0]) if r["process"] else None})
    waiting = [dict(r) for r in con.execute(
        "SELECT id, shops, requested, note, process FROM app_job WHERE finished IS NULL ORDER BY id")]
    recent = []
    for r in con.execute("""SELECT job, started, finished, pages_ok, pages_failed, note FROM run_log
                            WHERE finished IS NOT NULL ORDER BY finished DESC LIMIT 12"""):
        took = (dt.datetime.fromisoformat(r["finished"]) - dt.datetime.fromisoformat(r["started"])).total_seconds()
        recent.append({**dict(r), "job": job_name(r["job"]), "took": _duration(took)})
    return {"running": running, "waiting": waiting, "recent": recent}


def queue(con, now: dt.datetime | None = None) -> list[dict]:
    """What is waiting, in the order it will start, and why it waits."""
    from . import jobs, vivino
    from .awards.importer import awards_due
    from .cli import run_is_due
    from .mail import mail_due
    from .shops import SHOPS

    now = now or dt.datetime.now()
    out = []
    lim = jobs.limits(con)
    running = jobs.running_shops(con)
    status = jobs.shop_status(con, now)
    # 1. Shops asked for in the app ("Read shops now"), in the order they were asked for
    for j in status["jobs"]:
        for key in j["shops"].split(","):
            s = next((x for x in status["shops"] if x["key"] == key), None)
            if s and s["state"] == "queued":
                out.append({"what": f"{s['name']}: shop run", "who": f"you, in the app (run {j['id']}, asked {j['requested'][11:16]})",
                            "why": s["advice"] + (f" (at most {lim['max_runs']} shops at once, "
                                                  f"{lim['max_browser_runs']} with the browser)"
                                                  if "fewer shops" in s["advice"] else "")})
    # Shops that answered "too many requests": they wait, then carry on by themselves
    for s in status.get("paused", []):
        out.append({"what": f"{s['name']}: shop run, paused", "who": "the shop asked us to slow down (HTTP 429)",
                    "why": s["advice"]})
    # 2. The weekly scheduler: one thing at a time, shops first
    due = [k for k in SHOPS if k not in running and run_is_due(con, now, k)]
    sched_busy = bool(running) or bool(due)
    for n, key in enumerate(due):
        free = jobs.slot_free(con, key)
        out.append({"what": f"{SHOPS[key].SHOP_NAME}: weekly shop run", "who": "the weekly scheduler",
                    "why": "next up" if n == 0 and free else
                           ("waiting until fewer shops are being read" if not free and n == 0 else
                            "the scheduler reads the shops one after another")})
    checks = []
    try:
        if mail_due(con, now):
            checks.append(("Weekly e-mail", "after the shop runs"))
    except Exception:
        pass
    try:
        if awards_due(con, now):
            checks.append(("Award check (competition sites)", "after the shop runs and the e-mail"))
    except Exception:
        pass
    weekly = getattr(vivino, "weekly_due", None)
    try:
        if weekly and weekly(con, now):
            checks.append(("Vivino ratings (Brave search)", "after the award check"))
    except Exception:
        pass
    for what, after in checks:
        out.append({"what": what, "who": "the weekly scheduler",
                    "why": after if sched_busy else "next time the scheduler looks (every 10 minutes)"})
    return out


def _duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds} s"
    if seconds < 3600:
        return f"{seconds // 60} min {seconds % 60:02d} s"
    return f"{seconds // 3600} h {seconds // 60 % 60:02d} min"


# ---------------------------------------------------------------- two looks: speeds

class Sampler:
    """Keeps the previous look, so CPU use and disk speed are over the time since."""

    def __init__(self):
        self.lock = threading.Lock()
        self.last: tuple[float, dict, dict] | None = None

    def look(self) -> dict:
        now = time.monotonic()
        procs = read_processes()
        db_files = {f["name"]: f for f in files()["files"]}
        with self.lock:
            prev = self.last
            self.last = (now, procs, db_files)
        if prev is None or now - prev[0] < 0.5:
            time.sleep(1.0)  # the first look: take a second one a moment later
            return self.look()
        then, before, db_before = prev
        span = now - then
        boot = _boot_time()
        rows = []
        for pid, p in procs.items():
            b = before.get(pid)
            same = b and b["started_ticks"] == p["started_ticks"]
            cpu = (p["ticks"] - b["ticks"]) / HZ / span * 100 if same else 0.0
            rows.append({**p, "cpu": round(max(cpu, 0.0), 1),
                         "read_rate": (p["read"] - b["read"]) / span if same else 0,
                         "write_rate": (p["write"] - b["write"]) / span if same else 0,
                         "since": boot + p["started_ticks"] / HZ})
        groups = {}
        for r in rows:  # the browser's many processes are one line
            key = ("browser",) if r["group"] == "browser" else (r["group"], r["pid"])
            g = groups.setdefault(key, {"group": r["group"], "name": r["name"], "pids": [], "cpu": 0.0, "rss": 0,
                                        "read_rate": 0.0, "write_rate": 0.0, "written": 0, "since": r["since"],
                                        "io_ok": r["io_ok"]})
            g["pids"].append(r["pid"])
            for k in ("cpu", "rss", "read_rate", "write_rate"):
                g[k] += r[k]
            g["written"] += r["write"]
            g["since"] = min(g["since"], r["since"])
        order = {"web": 0, "scheduler": 1, "job": 2, "browser": 3, "other": 4}
        groups = sorted(groups.values(), key=lambda g: (order[g["group"]], -g["cpu"]))
        db_now = db_files.get("wijnkoop.db-wal", {}).get("size", 0) + db_files.get("wijnkoop.db", {}).get("size", 0)
        db_then = db_before.get("wijnkoop.db-wal", {}).get("size", 0) + db_before.get("wijnkoop.db", {}).get("size", 0)
        changed = max((f["changed"] for f in db_files.values() if f["name"].startswith("wijnkoop.db")), default=0)
        return {"span": round(span, 1), "processes": groups,
                "cpu_total": round(sum(g["cpu"] for g in groups), 1),
                "write_total": sum(g["write_rate"] for g in groups),
                "read_total": sum(g["read_rate"] for g in groups),
                "database_growth": (db_now - db_then) / span, "database_changed": changed}


SAMPLER = Sampler()


def snapshot(con, extra_work: list[dict] | None = None) -> dict:
    s = SAMPLER.look()
    m = machine()
    w = work(con)
    try:
        w["queue"] = queue(con)
    except Exception as e:  # never let the dashboard fail on this
        w["queue"] = [{"what": "(the queue could not be read)", "who": "", "why": str(e)}]
    for e in extra_work or []:
        if e.get("waiting"):
            w["queue"].insert(0, {"what": e["text"], "who": "you, in the app",
                                  "why": "waiting for the database: a shop run is saving its results"})
    by_pid = {r["pid"]: r for r in w["running"] if r["pid"]}
    for g in s["processes"]:  # what each process is busy with, as far as the database says
        doing = [by_pid[p] for p in g["pids"] if p in by_pid]
        g["doing"] = "; ".join(f"{d['job']} ({d['note'] or 'starting'})" for d in doing)
    for g in s["processes"]:
        if g["group"] == "web" and extra_work:
            g["doing"] = "; ".join(x for x in [g.get("doing")] + [e["text"] for e in extra_work] if x)
    busy_pct = s["cpu_total"] / max(m["cpus"], 1)
    return {"time": dt.datetime.now().strftime("%H:%M:%S"), "sample": s, "machine": m, "files": files(),
            "work": w, "extra": extra_work or [], "busy_pct": round(busy_pct),
            "writing_now": s["write_total"] > 50_000 or abs(s["database_growth"]) > 0
                           or time.time() - s["database_changed"] < 5}
