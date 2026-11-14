"""The everyday WijnKoop screens, served by Datasette next to the expert tables.

Pages: / (home), /find, /search, /wine/<id>, /week, /watchlist, /order, /how, /awards, /upload,
/update (read shops now).
The data comes from wijnkoop/ui.py; the HTML from datasette/templates/wk_*.html.
"""
import csv
import io
import os
import re
import time
from urllib.parse import urlencode

from datasette import hookimpl
from datasette.utils.asgi import Response
from markupsafe import Markup

from wijnkoop import jobs, ui
from wijnkoop.bottles import bottle_size
from wijnkoop.awards import upload as award_upload
from wijnkoop.db import BUSY_TIMEOUT

DB = "wijnkoop"


async def _read(datasette, fn, *args):
    return await datasette.get_database(DB).execute_fn(lambda conn: fn(conn, *args))


async def _write(datasette, fn, *args):
    return await datasette.get_database(DB).execute_write_fn(lambda conn: fn(conn, *args), block=True)


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
    return await _page(datasette, request, "wk_wine.html", f"{w['name']} {w['vintage'] or ''}".strip(), **data)


async def week(datasette, request):
    sections = await _read(datasette, ui.week)
    return await _page(datasette, request, "wk_week.html", "This week", sections=sections)


async def watchlist(datasette, request):
    cards = await _read(datasette, ui.watchlist)
    return await _page(datasette, request, "wk_watchlist.html", "Watchlist", cards=cards)


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
            UPLOAD_PROGRESS[job] = {"step": "wait", "since": since}

        def progress(step, **details):
            if job:
                UPLOAD_PROGRESS[job] = {"step": step, "since": since, **details}

        data = await request.post_body()
        try:
            report = await _write(datasette, award_upload.import_upload, filename, data, competition,
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
            await _write(datasette, award_upload.judge_match, upload_id, int(post["award"]), int(post["wine"]),
                         post["verdict"] == "same")
            return Response.redirect(f"/upload/{upload_id}#a{post['award']}")
        if post.get("delete"):
            await _write(datasette, award_upload.delete_upload, upload_id)
        return Response.redirect("/upload")
    report = await _read(datasette, award_upload.upload_report, upload_id)
    if not report:
        return Response.html("<p>This upload is not known. <a href='/upload'>Uploads</a></p>", status=404)
    return await _page(datasette, request, "wk_upload_report.html", f"Upload: {report['upload']['filename']}",
                       **report)


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
            await _write(datasette, jobs.set_limits, post.get("max_runs"), post.get("max_browser_runs"))
            return Response.redirect("/update#limits")
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
    return await _page(datasette, request, "wk_update.html", "Read shops now", said=said, **status)


@hookimpl
def prepare_connection(conn, database):
    """Wait for a shop run that is saving (its scores take a while) instead of failing with
    "database is locked" after Datasette's usual 5 seconds."""
    if database == DB:
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT * 1000}")


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
        (r"^/awards$", awards),
        (r"^/how$", how),
        (r"^/upload$", upload),
        (r"^/upload/(?P<upload_id>\d+)$", upload_report),
        (r"^/upload/(?P<upload_id>\d+)/file$", upload_file),
        (r"^/upload/(?P<upload_id>\d+)/report\.csv$", upload_csv),
        (r"^/upload/progress$", upload_progress),
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
