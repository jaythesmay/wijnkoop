"""The everyday WijnKoop screens, served by Datasette next to the expert tables.

Pages: / (home), /find, /search, /wine/<id>, /week, /watchlist, /order, /how, /awards, /upload,
/update (read shops now).
The data comes from wijnkoop/ui.py; the HTML from datasette/templates/wk_*.html.
"""
import asyncio
import csv
import io
import os
import re
import time
from urllib.parse import parse_qs, urlencode

from datasette import hookimpl
from datasette.utils.asgi import Response
from markupsafe import Markup

from wijnkoop import award_wines, jobs, personal, quality, ui
from wijnkoop.bottles import bottle_size
from wijnkoop.awards import upload as award_upload
from wijnkoop.db import BUSY_TIMEOUT

DB = "wijnkoop"
MAX_UPLOAD = int(os.environ.get("WIJNKOOP_MAX_UPLOAD_MB", "25")) * 1_000_000  # bytes


async def _read(datasette, fn, *args):
    return await datasette.get_database(DB).execute_fn(lambda conn: fn(conn, *args))


async def _write(datasette, fn, *args):
    return await datasette.get_database(DB).execute_write_fn(lambda conn: fn(conn, *args), block=True)


async def _background(datasette, fn, *args):
    """Long work that writes (an upload, deleting one): in a thread of its own with its own connection,
    not on Datasette's single write thread, so Save, Watch and Read now stay quick meanwhile."""
    from pathlib import Path

    from wijnkoop import db as wdb

    path = Path(datasette.get_database(DB).path)

    def run():
        con = wdb.connect(path)
        try:
            return fn(con, *args)
        finally:
            con.close()

    return await asyncio.to_thread(run)


async def _page(datasette, request, template, title, **ctx):
    ctx.update({
        "title": title,
        "nav": await _read(datasette, ui.nav),
        "path": request.path,
        "here": request.full_path,
        "version": f"{os.environ.get('BUILD_DATE', 'dev')} (code {os.environ.get('GIT_SHA', 'unknown')[:7]})",
    })
    return Response.html(await datasette.render_template(template, ctx, request=request))


def _next(post, default="/"):
    nxt = post.get("next") or default
    # only go back to a page of this app
    return nxt if nxt.startswith("/") and not nxt.startswith("//") else default


def _wants_json(request):
    return request.headers.get("x-wk-quick") == "1"


async def home(datasette, request):
    data = await _read(datasette, ui.home)
    return await _page(datasette, request, "wk_home.html", "WijnKoop", **data)


async def find(datasette, request):
    args = {k: request.args.get(k) for k in ("max_price", "min_price", "colour", "country", "grape", "gold",
                                              "min_score", "deal", "in_stock", "unscored", "sort",
                                              "award", "award_result", "award_year")}
    data = await _read(datasette, ui.find, args)
    opts = await _read(datasette, ui.options)
    active = {k: v for k, v in args.items() if v}
    return await _page(datasette, request, "wk_find.html", "Find a wine", args=args, active=active,
                       opts=opts, query=urlencode(active), **data)


async def search(datasette, request):
    q = (request.args.get("q") or "").strip()
    cards = await _read(datasette, ui.search, q) if q else []
    return await _page(datasette, request, "wk_search.html", f"Search: {q}" if q else "Search",
                       q=q, cards=cards)


async def wine(datasette, request):
    wine_id = int(request.url_vars["wine_id"])
    data = await _read(datasette, ui.wine, wine_id)
    if not data:
        return Response.html("<p>This wine is not known. <a href='/'>Home</a></p>", status=404)
    w = data["wine"]
    data["mine"] = await _read(datasette, personal.mine, wine_id)
    data["in_cellar"] = await _read(datasette, personal.in_cellar, wine_id)
    data["target"] = await _read(datasette, lambda c, i: (c.execute(
        "SELECT target_price FROM watch WHERE wine_id=?", (i,)).fetchone() or [None])[0], wine_id)
    return await _page(datasette, request, "wk_wine.html", f"{w['name']} {w['vintage'] or ''}".strip(), **data)


async def week(datasette, request):
    sections = await _read(datasette, ui.week)
    return await _page(datasette, request, "wk_week.html", "This week", sections=sections)


async def watchlist(datasette, request):
    cards = await _read(datasette, ui.watchlist)
    targets = await _read(datasette, personal.watched)
    alerts = await _read(datasette, personal.recent_alerts, 30)
    return await _page(datasette, request, "wk_watchlist.html", "Watchlist", cards=cards, targets=targets,
                       alerts=alerts, push=bool(os.environ.get("WIJNKOOP_NTFY_URL")))


async def watch_target(datasette, request):
    if request.method != "POST":
        return Response.redirect("/watchlist")
    post = await request.post_vars()
    await _write(datasette, personal.set_target, int(post["wine_id"]), post.get("target"))
    return Response.redirect(_next(post, "/watchlist"))


async def mine(datasette, request):
    """Your own rating and note for a wine."""
    if request.method != "POST":
        return Response.redirect("/")
    post = await request.post_vars()
    wine_id = int(post["wine_id"])
    await _write(datasette, personal.set_mine, wine_id, post.get("rating"), post.get("note"))
    return Response.redirect(_next(post, f"/wine/{wine_id}"))


async def cellar(datasette, request):
    if request.method == "POST":
        post = await request.post_vars()
        if post.get("cellar_id"):
            await _write(datasette, personal.set_bottles, int(post["cellar_id"]), post.get("bottles"))
        elif post.get("wine_id"):
            await _write(datasette, personal.add_to_cellar, int(post["wine_id"]), post.get("bottles"),
                         post.get("price"), post.get("drink_until"), post.get("note"))
        return Response.redirect(_next(post, "/cellar"))
    data = await _read(datasette, personal.cellar)
    return await _page(datasette, request, "wk_cellar.html", "Cellar", **data)


async def awards(datasette, request):
    rows = await _read(datasette, ui.competitions)
    return await _page(datasette, request, "wk_awards.html", "Awards and critics", awards=rows)


def _competitions(conn):
    return [dict(r) for r in conn.execute("SELECT short, name FROM competition ORDER BY name")]


# What each upload that is running is doing, by the code its page sent along ("job"). The page
# asks for it every second or two while it waits. Kept in memory: it only matters while it runs.
UPLOAD_PROGRESS: dict[str, dict] = {}


def _progress_text(p: dict) -> dict:
    """The steps for the page: which are done, which runs now (with numbers), how long it has taken."""
    step = p.get("step")
    keys = list(award_upload.STEPS)
    now_i = keys.index(step) if step in keys else -1
    out = []
    for i, key in enumerate(keys):
        detail = ""
        if i == now_i:
            if key == "read" and p.get("size"):
                detail = f"{p['size'] / 1e6:.1f} MB"
            elif key == "find" and p.get("lines") is not None:
                detail = f"{p['lines']:,} lines"
            elif key == "link" and p.get("total"):
                done, total = p.get("done", 0), p["total"]
                detail = f"{done:,} of {total:,} · {p.get('linked', 0):,} linked, {p.get('review', 0):,} probable"
                if done and p.get("step_seconds", 0) > 3:
                    left = p["step_seconds"] / done * (total - done)
                    detail += f" · about {_duration(left)} left"
        out.append({"label": award_upload.STEPS[key], "state": "done" if i < now_i else "now" if i == now_i else "todo",
                    "detail": detail})
    return {"step": step, "steps": out, "seconds": round(p.get("seconds", 0)), "waiting": step == "wait",
            "elapsed": _duration(time.time() - p.get("since", time.time()))}


def _duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} s"
    if seconds < 3600:
        return f"{seconds // 60} min {seconds % 60:02d} s"
    return f"{seconds // 3600} h {seconds // 60 % 60:02d} min"


async def upload_progress(datasette, request):
    job = request.args.get("job") or ""
    p = UPLOAD_PROGRESS.get(job)
    return Response.json(_progress_text(p) if p else {"step": None})


async def upload(datasette, request):
    """Load an official results file. The browser sends the file itself as the request body,
    with its name, competition and year in the address."""
    if request.method == "POST":
        filename = (request.args.get("filename") or "upload").strip()[:200]
        competition = (request.args.get("competition") or "").strip() or None
        year = request.args.get("year") or ""
        job = request.args.get("job") or ""
        job = job if re.fullmatch(r"[a-z0-9]{6,40}", job) else None
        for old, p in list(UPLOAD_PROGRESS.items()):
            if time.time() - p.get("since", 0) > 6 * 3600:
                UPLOAD_PROGRESS.pop(old, None)
        since = time.time()
        if job:  # until the database is free (a shop run may be saving)
            UPLOAD_PROGRESS[job] = {"step": "wait", "since": since, "filename": filename}

        def progress(step, **details):
            if job:
                UPLOAD_PROGRESS[job] = {"step": step, "since": since, "filename": filename, **details}

        size = request.headers.get("content-length") or ""
        if size.isdigit() and int(size) > MAX_UPLOAD:
            UPLOAD_PROGRESS.pop(job, None)
            return Response.json({"error": f"This file is larger than {MAX_UPLOAD // 1_000_000} MB. "
                                           "Save only the results table, or split the file."}, status=413)
        data = await request.post_body()
        if len(data) > MAX_UPLOAD:
            UPLOAD_PROGRESS.pop(job, None)
            return Response.json({"error": f"This file is larger than {MAX_UPLOAD // 1_000_000} MB."}, status=413)
        try:
            report = await _background(datasette, award_upload.import_upload, filename, data, competition,
                                  int(year) if year.isdigit() else None, None, progress)
        except Exception as e:  # unreadable file: say so on the page instead of a crash
            UPLOAD_PROGRESS.pop(job, None)
            if _busy(e):
                return Response.json({"error": "WijnKoop is busy saving a shop run. Nothing was loaded; "
                                               "try again in a few minutes."}, status=503)
            return Response.json({"error": str(e) or e.__class__.__name__}, status=400)
        UPLOAD_PROGRESS.pop(job, None)
        return Response.json({"id": report["upload"]["id"], "awards": report["upload"]["awards"]})
    done = await _read(datasette, award_upload.uploads)
    comps = await _read(datasette, _competitions)
    return await _page(datasette, request, "wk_upload.html", "Check awards with a results file",
                       uploads=done, competitions=comps)


async def upload_report(datasette, request):
    upload_id = int(request.url_vars["upload_id"])
    if request.method == "POST":
        post = await request.post_vars()
        if post.get("verdict") in ("same", "other") and post.get("award", "").isdigit() and post.get("wine", "").isdigit():
            # Saved right away; the scores follow in the background, so the page answers at once.
            hit = await _background(datasette, award_upload.judge_match, upload_id, int(post["award"]), int(post["wine"]),
                               post["verdict"] == "same", False)
            if hit:
                _rescore_soon()
            if _wants_json(request):
                return Response.json({"saved": hit, "verdict": post["verdict"]})
            return Response.redirect(f"/upload/{upload_id}#a{post['award']}")
        if post.get("delete"):
            await _background(datasette, award_upload.delete_upload, upload_id)
        return Response.redirect("/upload")
    report = await _read(datasette, award_upload.upload_report, upload_id)
    if not report:
        return Response.html("<p>This upload is not known. <a href='/upload'>Uploads</a></p>", status=404)
    return await _page(datasette, request, "wk_upload_report.html", f"Upload: {report['upload']['filename']}",
                       **report)


_RESCORE = {"timer": None}


def _rescore_soon(wait: float = 20):
    """Work the scores out again a little after the last Yes/No, once for a series of clicks,
    in its own process so the pages stay quick."""
    import subprocess
    import sys
    import threading

    def go():
        proc = subprocess.Popen([sys.executable, "-m", "wijnkoop", "scores"], start_new_session=True,
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
        threading.Thread(target=proc.wait, daemon=True).start()

    if _RESCORE["timer"]:
        _RESCORE["timer"].cancel()
    _RESCORE["timer"] = threading.Timer(wait, go)
    _RESCORE["timer"].daemon = True
    _RESCORE["timer"].start()


async def upload_check(datasette, request):
    """Every award link from your results files that waits for your Yes or No, until you answer."""
    if request.method == "POST":
        post = await request.post_vars()
        hit = False
        if post.get("verdict") in ("same", "other") and post.get("award", "").isdigit() and post.get("wine", "").isdigit():
            hit = await _background(datasette, award_upload.judge_match, None, int(post["award"]), int(post["wine"]),
                               post["verdict"] == "same", False)
            if hit:
                _rescore_soon()
        if _wants_json(request):
            return Response.json({"saved": hit, "verdict": post.get("verdict"),
                                  "left": (await _read(datasette, ui.nav))["to_check"]})
        return Response.redirect(f"/upload/check#a{post.get('award', '')}")
    rows = await _read(datasette, award_upload.to_check)
    return await _page(datasette, request, "wk_upload_check.html", "Award links to check", links=rows)


async def upload_csv(datasette, request):
    """The detailed report: every award in the file and what happened to it, for Excel."""
    upload_id = int(request.url_vars["upload_id"])
    rows = await _read(datasette, award_upload.report_rows, upload_id)
    names = await _read(datasette, lambda conn: [r[0] for r in conn.execute(
        "SELECT filename FROM award_upload WHERE id=?", (upload_id,))])
    if not names:
        return Response.text("This upload is not known.", status=404)
    buf = io.StringIO()
    out = csv.writer(buf, delimiter=";")  # Dutch Excel opens a semicolon list as columns
    out.writerow(award_upload.REPORT_COLUMNS)
    out.writerows(rows)
    name = re.sub(r"[^\w.-]+", "_", names[0].rsplit(".", 1)[0]) + "-report.csv"
    return Response("\ufeff" + buf.getvalue(), content_type="text/csv; charset=utf-8",
                    headers={"content-disposition": f'attachment; filename="{name}"'})


def _uploads_running() -> list[dict]:
    out = []
    for p in list(UPLOAD_PROGRESS.values()):
        t = _progress_text(p)
        now = next((x for x in t["steps"] if x["state"] == "now"), None)
        step = "waiting for the database" if t["waiting"] else (now["label"].lower() if now else p.get("step"))
        detail = f" · {now['detail']}" if now and now["detail"] else ""
        out.append({"text": f"Upload {p.get('filename', '')}: {step}{detail} ({t['elapsed']})", "waiting": t["waiting"]})
    return out


async def activity_page(datasette, request):
    """What the app is doing now: processes, disk and database, running and waiting work."""
    return await _page(datasette, request, "wk_activity.html", "What WijnKoop is doing")


async def activity_json(datasette, request):
    from wijnkoop import activity

    data = await _read(datasette, activity.snapshot, _uploads_running())
    return Response.json(data, default=str)


async def upload_file(datasette, request):
    """The uploaded results file itself, as it was loaded."""
    upload_id = int(request.url_vars["upload_id"])
    rows = await _read(datasette, lambda conn: [dict(r) for r in conn.execute(
        "SELECT filename FROM award_upload WHERE id=?", (upload_id,))])
    path = award_upload.stored_file(upload_id, rows[0]["filename"]) if rows else None
    if not path or not path.exists():
        return Response.text("This file was not kept.", status=404)
    return Response(path.read_bytes(), content_type="application/octet-stream",
                    headers={"content-disposition": f'attachment; filename="{path.name.split("-", 1)[1]}"'})


async def how(datasette, request):
    if request.method == "POST":
        post = await request.post_vars()
        try:
            await _write(datasette, ui.set_rate, post.get("rate_pct"))
        except ValueError:
            pass
        return Response.redirect("/how#rate")
    rule = await _read(datasette, ui.price_rule)
    return await _page(datasette, request, "wk_how.html", "How the scores work", rule=rule)


async def order(datasette, request):
    if request.method == "POST":
        post = await request.post_vars()
        if post.get("clear"):
            await _write(datasette, ui.clear_order)
        elif post.get("offer_id"):
            await _write(datasette, ui.set_order, int(post["offer_id"]), int(post.get("qty") or 0))
        return Response.redirect("/order")
    data = await _read(datasette, ui.order)
    return await _page(datasette, request, "wk_order.html", "Order list", **data)


async def order_add(datasette, request):
    if request.method != "POST":
        return Response.redirect("/order")
    post = await request.post_vars()
    offer_id = int(post["offer_id"])
    qty = await _write(datasette, ui.add_to_order, offer_id, int(post.get("qty") or 6))
    if _wants_json(request):
        return Response.json({"qty": qty, "nav": await _read(datasette, ui.nav)})
    return Response.redirect(_next(post))


async def watch(datasette, request):
    if request.method != "POST":
        return Response.redirect("/watchlist")
    post = await request.post_vars()
    watched = await _write(datasette, ui.toggle_watch, int(post["wine_id"]))
    if _wants_json(request):
        return Response.json({"watched": watched, "nav": await _read(datasette, ui.nav)})
    return Response.redirect(_next(post))


async def update(datasette, request):
    """Read shops now: one shop or every shop for which it makes sense, never twice at once."""
    if request.method == "POST":
        post = await request.post_vars()
        if post.get("stop"):
            await _write(datasette, jobs.stop_job, int(post["stop"]))
            return Response.redirect("/update")
        if post.get("limits"):
            # A file next to the database, not a table: no waiting for a run that is saving.
            saved = await _read(datasette, jobs.set_limits, post.get("max_runs"), post.get("max_browser_runs"))
            if _wants_json(request):
                return Response.json(saved)
            return Response.redirect("/update?saved=limits#limits")
        if post.get("shop") == "all":
            status = await _read(datasette, jobs.shop_status)
            keys = status["useful"]
        else:
            keys = [post.get("shop") or ""]
        r = await _write(datasette, jobs.request_run, keys, bool(post.get("force")))
        if r["job_id"]:
            jobs.launch(r["job_id"])
        return Response.redirect("/update?" + urlencode(
            {"started": ",".join(r["shops"]), "busy": ",".join(r["busy"]), "skipped": ",".join(r["skipped"])}))
    status = await _read(datasette, jobs.shop_status)
    names = {s["key"]: s["name"] for s in status["shops"]}
    said = {k: [names.get(x, x) for x in (request.args.get(k) or "").split(",") if x]
            for k in ("started", "busy", "skipped")}
    return await _page(datasette, request, "wk_update.html", "Read shops now", said=said,
                       saved=request.args.get("saved"), **status)


async def quality_page(datasette, request):
    """Data quality: what the checks found, and what you want done about it."""
    if request.method == "POST":
        form = parse_qs((await request.post_body()).decode("utf-8", "replace"), keep_blank_values=True)
        one = {k: v[-1] for k, v in form.items()}
        ids = [int(i) for v in form.get("ids", []) for i in v.split(",") if i.strip().isdigit()]
        back = {k: one[k] for k in ("check", "state", "page") if one.get(k)}
        if one.get("run"):
            running = await _read(datasette, quality.running)
            if not running:
                quality.start_in_background()
            said = {"said": "running" if not running else "already"}
        elif one.get("reopen"):
            n = await _write(datasette, quality.reopen, ids)
            said = {"said": f"reopened {n}"}
        else:
            act = one.get("act") or None
            r = await _write(datasette, quality.resolve, ids, None if act == "suggested" else act,
                             one.get("all_of") or None)
            if r["rescore"]:
                quality.recompute_in_background()
            said = {"said": f"done {r['done']}" + (f", skipped {r['skipped']}" if r["skipped"] else "")}
            if _wants_json(request):
                return Response.json(r)
        return Response.redirect("/quality?" + urlencode({**back, **said}))
    page = request.args.get("page") or "1"
    data = await _read(datasette, quality.overview, request.args.get("check"), request.args.get("state") or "open",
                       int(page) if page.isdigit() else 1)
    return await _page(datasette, request, "wk_quality.html", "Data quality", said=request.args.get("said"), **data)


async def awards_not_sold(datasette, request):
    a = request.args
    page = a.get("page") or "1"
    data = await _read(datasette, award_wines.not_sold, a.get("q") or "", a.get("colour") or "",
                       a.get("competition") or "", a.get("gold") or "", int(page) if page.isdigit() else 1)
    return await _page(datasette, request, "wk_award_wines.html", "Award wines not in your shops", **data)


@hookimpl
def asgi_wrapper(datasette):
    """An optional password for the whole app: set WIJNKOOP_PASSWORD (and WIJNKOOP_USER, default
    "wijnkoop") in docker-compose. Without it the app is open, which is fine at home and over
    Tailscale; with it the browser asks once and remembers it."""
    password = os.environ.get("WIJNKOOP_PASSWORD") or ""
    user = os.environ.get("WIJNKOOP_USER") or "wijnkoop"

    def wrap(app):
        if not password:
            return app

        import base64
        import hmac

        expected = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()

        async def guarded(scope, receive, send):
            if scope["type"] != "http":
                return await app(scope, receive, send)
            given = dict(scope.get("headers") or []).get(b"authorization", b"").decode("latin-1")
            if hmac.compare_digest(given, expected):
                return await app(scope, receive, send)
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"www-authenticate", b'Basic realm="WijnKoop", charset="UTF-8"'),
                                    (b"content-type", b"text/plain; charset=utf-8")]})
            await send({"type": "http.response.body", "body": "WijnKoop: password needed".encode()})

        return guarded

    return wrap


@hookimpl
def prepare_connection(conn, database):
    """Wait for a shop run that is saving (its scores take a while) instead of failing with
    "database is locked" after Datasette's usual 5 seconds."""
    if database == DB:
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT * 1000}")
        conn.execute("PRAGMA synchronous = NORMAL")


def _busy(e: Exception) -> bool:
    return "locked" in str(e) or "busy" in str(e)


@hookimpl
def register_routes():
    return [
        (r"^/$", home),
        (r"^/find$", find),
        (r"^/search$", search),
        (r"^/wine/(?P<wine_id>\d+)$", wine),
        (r"^/week$", week),
        (r"^/watchlist$", watchlist),
        (r"^/watch/target$", watch_target),
        (r"^/mine$", mine),
        (r"^/cellar$", cellar),
        (r"^/awards$", awards),
        (r"^/awards/not-sold$", awards_not_sold),
        (r"^/quality$", quality_page),
        (r"^/how$", how),
        (r"^/upload$", upload),
        (r"^/upload/(?P<upload_id>\d+)$", upload_report),
        (r"^/upload/(?P<upload_id>\d+)/file$", upload_file),
        (r"^/upload/(?P<upload_id>\d+)/report\.csv$", upload_csv),
        (r"^/upload/progress$", upload_progress),
        (r"^/upload/check$", upload_check),
        (r"^/activity$", activity_page),
        (r"^/activity\.json$", activity_json),
        (r"^/order$", order),
        (r"^/order/add$", order_add),
        (r"^/watch$", watch),
        (r"^/update$", update),
    ]


# In the expert tables, show which wine, offer or award an id stands for, linked to its page.
ID_LABELS = {
    "wine_id": ("SELECT name || COALESCE(' ' || vintage, '') FROM wine WHERE id = ?", "/wine/{}"),
    "offer_id": ("SELECT w.name || COALESCE(' ' || w.vintage, '') || ' (' || s.name || ')' FROM offer o "
                 "JOIN wine w ON w.id = o.wine_id JOIN shop s ON s.id = o.shop_id WHERE o.id = ?", "/wine/{}"),
    "award_id": ("SELECT raw_text FROM award WHERE id = ?", None),
}


@hookimpl
def render_cell(value, column, table, database, datasette):
    if column not in ID_LABELS or database != DB or table in ("wine", "offer", "award"):
        return None
    if isinstance(value, dict):  # a foreign key Datasette already expanded
        value = value.get("value")
    if not isinstance(value, int):
        return None

    async def inner():
        sql, link = ID_LABELS[column]
        db = datasette.get_database(DB)
        row = (await db.execute(sql, [value])).first()
        if not row or row[0] is None:
            return None
        target = link
        if column == "offer_id":
            wine_id = (await db.execute("SELECT wine_id FROM offer WHERE id = ?", [value])).first()
            target = link.format(wine_id[0]) if wine_id else None
        elif link:
            target = link.format(value)
        label = f"{value} · {row[0]}"
        if target:
            return Markup('<a href="{}">{}</a>').format(target, label)
        return Markup("{}").format(label)

    return inner


def _verdict_class(verdict):
    return {"Top buy": "top", "Good buy": "good", "Fair": "fair"}.get(verdict, "pricey") if verdict else ""


@hookimpl
def prepare_jinja2_environment(env):
    env.filters["money"] = ui.money
    env.filters["bottle_size"] = bottle_size
    env.filters["score"] = lambda s: f"{s:.1f}".rstrip("0").rstrip(".") if s is not None else "–"
    env.filters["verdict_class"] = _verdict_class
    env.filters["country"] = ui.country_en
