"""The everyday WijnKoop screens, served by Datasette next to the expert tables.

Pages: / (home), /find, /search, /wine/<id>, /week, /watchlist, /order, /how.
The data comes from wijnkoop/ui.py; the HTML from datasette/templates/wk_*.html.
"""
import os
from urllib.parse import urlencode

from datasette import hookimpl
from datasette.utils.asgi import Response

from wijnkoop import ui

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
                                              "min_score", "deal", "in_stock", "unscored", "sort")}
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


async def how(datasette, request):
    rule = await _read(datasette, ui.price_rule)
    return await _page(datasette, request, "wk_how.html", "How the scores work", rule=rule,
                       example=ui.fair_example(rule["per_point_pct"]))


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


@hookimpl
def register_routes():
    return [
        (r"^/$", home),
        (r"^/find$", find),
        (r"^/search$", search),
        (r"^/wine/(?P<wine_id>\d+)$", wine),
        (r"^/week$", week),
        (r"^/watchlist$", watchlist),
        (r"^/how$", how),
        (r"^/order$", order),
        (r"^/order/add$", order_add),
        (r"^/watch$", watch),
    ]


def _verdict_class(verdict):
    return {"Top buy": "top", "Good buy": "good", "Fair": "fair"}.get(verdict, "pricey") if verdict else ""


@hookimpl
def prepare_jinja2_environment(env):
    env.filters["money"] = ui.money
    env.filters["score"] = lambda s: f"{s:.1f}".rstrip("0").rstrip(".") if s is not None else "–"
    env.filters["verdict_class"] = _verdict_class
    env.filters["country"] = ui.country_en
