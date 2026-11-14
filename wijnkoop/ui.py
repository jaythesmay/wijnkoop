"""Data for the everyday screens (home, find, search, wine page, order list, watchlist).

Every function takes a plain sqlite3 connection, so the screens can be tested without
a web server. The Datasette plugin in datasette/plugins/ calls these.
"""
import datetime as dt
import math
import re
import sqlite3
from collections import defaultdict

from rapidfuzz import fuzz, process

from .deals import line_cost, parse_deal
from .normalize import norm
from .reference import award_score
from .vivino import search_link as vivino_search_link

COLOURS = {"red": "Red", "white": "White", "rosé": "Rosé"}
# Gall writes country names in Dutch; the screens are in English.
COUNTRY_EN = {
    "frankrijk": "France", "italie": "Italy", "spanje": "Spain", "portugal": "Portugal", "duitsland": "Germany",
    "oostenrijk": "Austria", "zuid afrika": "South Africa", "chili": "Chile", "argentinie": "Argentina",
    "australie": "Australia", "nieuw zeeland": "New Zealand", "verenigde staten": "United States",
    "amerika": "United States", "griekenland": "Greece", "hongarije": "Hungary", "roemenie": "Romania",
    "bulgarije": "Bulgaria", "moldavie": "Moldova", "georgie": "Georgia", "israel": "Israel", "libanon": "Lebanon",
    "slovenie": "Slovenia", "kroatie": "Croatia", "zwitserland": "Switzerland", "uruguay": "Uruguay",
    "nederland": "Netherlands", "engeland": "England", "verenigd koninkrijk": "United Kingdom",
    "macedonie": "North Macedonia", "turkije": "Turkey", "canada": "Canada", "mexico": "Mexico", "brazilie": "Brazil",
}


def country_en(name: str | None) -> str | None:
    if not name:
        return name
    return COUNTRY_EN.get(norm(name), name)


SORTS = {
    "value": "price_as_93 ASC NULLS LAST, score DESC",
    "score": "score DESC NULLS LAST, price_as_93 ASC",
    "price": "price_per_bottle_at_6 ASC",
}
CARD_COLUMNS = """offer_id, wine_id, wine, vintage, producer, colour, country, shop, price_per_bottle_at_6,
    price_per_bottle_as_sold, volume_cl, normal_price, promotion, deal, deal_reason, score, awards, best_award, value, fair_price, price_quality,
    price_as_93, cheaper_than_pct, nothing_better_for_less, verdict, gold_or_better, other_vintages_awarded, other_vintages_avg, producer_wines_awarded, producer_avg,
    online_in_stock, url, grapes, category"""


# Filters on one competition or critic: what result counts.
AWARD_RESULTS = {
    "counted": ("Any medal or score", "award_score(a.medal, a.points, c.short) IS NOT NULL"),
    "gold": ("Gold or better (worth 93+ points)", "award_score(a.medal, a.points, c.short) >= 93"),
    "90": ("90 points or more", "award_score(a.medal, a.points, c.short) >= 90"),
    "93": ("93 points or more", "award_score(a.medal, a.points, c.short) >= 93"),
    "95": ("95 points or more", "award_score(a.medal, a.points, c.short) >= 95"),
}


def _award_sql(conn, args) -> tuple[str, list] | None:
    """SQL condition on award a / competition c for the award filters, or None when not used."""
    short, result, year = args.get("award"), args.get("award_result"), args.get("award_year")
    if not (short or result in AWARD_RESULTS or year):
        return None
    conn.create_function("award_score", 3, award_score, deterministic=True)
    cond, params = ["COALESCE(m.confirmed, 1) = 1", AWARD_RESULTS.get(result, AWARD_RESULTS["counted"])[1]], []
    if short:
        cond.append("c.short = ?")
        params.append(short)
    if year and str(year).isdigit():
        cond.append("a.year = ?")
        params.append(int(year))
    return " AND ".join(cond), params


def _rows(conn, sql, params=()):
    cur = conn.execute(sql, params)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _setting(conn, key, default=None):
    try:
        row = conn.execute("SELECT value FROM setting WHERE key=?", (key,)).fetchone()
    except sqlite3.OperationalError:
        return default
    return row[0] if row else default


def _decorate(conn, cards):
    """Add watch / order state and tags to card rows."""
    watched = {r[0] for r in conn.execute("SELECT wine_id FROM watch")}
    ordered = dict(conn.execute("SELECT offer_id, qty FROM order_item").fetchall())
    for c in cards:
        c["watched"] = c["wine_id"] in watched
        c["in_order"] = ordered.get(c["offer_id"], 0)
        tags = []
        if c["deal"] == "yes":
            tags.append("Deal")
        if c["gold_or_better"] == "yes":
            tags.append("Gold or better")
        if c["nothing_better_for_less"] == "yes":
            tags.append("Nothing better for less")
        if (c["other_vintages_awarded"] or 0) >= 2 or (c["producer_wines_awarded"] or 0) >= 3:
            tags.append("Wins every year")
        if c["online_in_stock"] == "no":
            tags.append("Out of stock online")
        c["tags"] = tags
    return cards


def price_rule(conn) -> dict:
    """Loes's own rate per point, and what the shops' award wines show (for information)."""
    from .scores import rate_pct
    rate = rate_pct(conn)
    n = int(_setting(conn, "price_fit_wines", "0") or 0)
    market = _setting(conn, "price_per_point_pct")
    award_wines = conn.execute("SELECT COUNT(DISTINCT wine_id) FROM best_buys WHERE score IS NOT NULL").fetchone()[0]
    return {"rate_pct": rate, "rate": f"{rate:g}", "example": round(10 * (1 + rate / 100), 2),
            "shops_pct": int(market) if market and n else None, "fit_wines": n, "award_wines": award_wines}


def set_rate(conn, pct) -> float:
    """Save Loes's rate per point and work out all values again."""
    from .scores import RATE_RANGE, recompute
    pct = min(max(float(str(pct).replace(",", ".")), RATE_RANGE[0]), RATE_RANGE[1])
    conn.execute("INSERT OR REPLACE INTO setting (key, value) VALUES ('points_rate_pct', ?)", (f"{pct:g}",))
    conn.commit()
    old = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        recompute(conn)
    finally:
        conn.row_factory = old
    return pct


def home(conn) -> dict:
    gold = _rows(conn, f"""SELECT {CARD_COLUMNS} FROM best_buys
        WHERE gold_or_better = 'yes' AND online_in_stock IS NOT 'no'
        ORDER BY price_as_93 ASC NULLS LAST LIMIT 6""")
    seen = {c["offer_id"] for c in gold}
    deals = [c for c in _rows(conn, f"""SELECT {CARD_COLUMNS} FROM best_buys
        WHERE deal = 'yes' AND score IS NOT NULL AND online_in_stock IS NOT 'no'
        ORDER BY price_as_93 ASC NULLS LAST LIMIT 12""") if c["offer_id"] not in seen][:6]
    counts = conn.execute(
        """SELECT COUNT(*), SUM(score IS NOT NULL), SUM(deal = 'yes' AND score IS NOT NULL),
                  SUM(gold_or_better = 'yes') FROM best_buys""").fetchone()
    return {"gold": _decorate(conn, gold), "deals": _decorate(conn, deals), "wines": counts[0] or 0,
            "scored": counts[1] or 0, "deal_count": counts[2] or 0, "gold_count": counts[3] or 0,
            "rule": price_rule(conn)}


def options(conn) -> dict:
    countries = [r[0] for r in conn.execute(
        "SELECT country FROM best_buys WHERE country IS NOT NULL GROUP BY country ORDER BY COUNT(*) DESC")]
    return {"colours": COLOURS, "countries": {c: country_en(c) for c in countries},
            "sorts": {"value": "Best value", "score": "Best score", "price": "Lowest price"},
            "awards": competitions(conn), "award_results": {k: v[0] for k, v in AWARD_RESULTS.items()},
            "award_years": [r[0] for r in conn.execute(
                "SELECT DISTINCT a.year FROM award a JOIN award_match m ON m.award_id = a.id "
                "WHERE a.year IS NOT NULL ORDER BY a.year DESC")]}


def competitions(conn) -> list[dict]:
    """Competitions and critics with how many wines for sale they gave a counted award."""
    conn.create_function("award_score", 3, award_score, deterministic=True)
    return _rows(conn, """
        SELECT c.short, c.name, c.kind, c.weight,
               COUNT(DISTINCT CASE WHEN award_score(a.medal, a.points, c.short) IS NOT NULL THEN b.wine_id END) AS wines,
               COUNT(DISTINCT CASE WHEN award_score(a.medal, a.points, c.short) >= 93
                     THEN b.wine_id END) AS gold_wines,
               MAX(a.year) AS latest_year
        FROM competition c
        JOIN award a ON a.competition_id = c.id
        JOIN award_match m ON m.award_id = a.id AND COALESCE(m.confirmed, 1) = 1
        JOIN best_buys b ON b.wine_id = m.wine_id
        GROUP BY c.id HAVING wines > 0
        ORDER BY c.weight DESC, wines DESC""")


def _float(v):
    try:
        return float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return None


def find(conn, args: dict, limit: int = 60) -> dict:
    where, params = ["1=1"], []
    max_price, min_price = _float(args.get("max_price")), _float(args.get("min_price"))
    if max_price:
        where.append("price_per_bottle_at_6 <= ?")
        params.append(max_price)
    if min_price:
        where.append("price_per_bottle_at_6 >= ?")
        params.append(min_price)
    if args.get("colour") in COLOURS:
        where.append("colour = ?")
        params.append(args["colour"])
    if args.get("country"):
        where.append("country = ?")
        params.append(args["country"])
    if args.get("grape"):
        where.append("grapes LIKE ?")
        params.append(f"%{args['grape'].strip()}%")
    if args.get("gold"):
        where.append("gold_or_better = 'yes'")
    min_score = _float(args.get("min_score"))
    if min_score:
        where.append("score >= ?")
        params.append(min_score)
    if args.get("deal"):
        where.append("deal = 'yes'")
    if args.get("in_stock"):
        where.append("online_in_stock IS NOT 'no'")
    if not args.get("unscored"):
        where.append("score IS NOT NULL")
    extra, extra_params = "", []
    award = _award_sql(conn, args)
    if award:
        cond, aparams = award
        where.append(f"""wine_id IN (SELECT m.wine_id FROM award_match m JOIN award a ON a.id = m.award_id
                         JOIN competition c ON c.id = a.competition_id WHERE {cond})""")
        params += aparams
        extra = f""", (SELECT GROUP_CONCAT(c.short || COALESCE(' ' || a.year, '') || COALESCE(' ' || a.medal, '')
                        || COALESCE(printf(' %g pts', a.points), ''), '; ')
                      FROM award_match m JOIN award a ON a.id = m.award_id JOIN competition c ON c.id = a.competition_id
                      WHERE m.wine_id = best_buys.wine_id AND {cond}) AS matched_award"""
        extra_params = aparams
    sort = SORTS.get(args.get("sort"), SORTS["value"])
    rows = _rows(conn, f"SELECT {CARD_COLUMNS}{extra} FROM best_buys WHERE {' AND '.join(where)} ORDER BY {sort} "
                       f"LIMIT {int(limit)}", extra_params + params)
    total = conn.execute(f"SELECT COUNT(*) FROM best_buys WHERE {' AND '.join(where)}", params).fetchone()[0]
    return {"cards": _decorate(conn, rows), "total": total}


def search(conn, q: str, limit: int = 40) -> list[dict]:
    q = (q or "").strip()
    if not q:
        return []
    rows = _rows(conn, f"SELECT {CARD_COLUMNS} FROM best_buys")
    choices = {i: norm(f"{r['wine']} {r['producer'] or ''} {r['vintage'] or ''} {r['grapes'] or ''}")
               for i, r in enumerate(rows)}
    hits = process.extract(norm(q), choices, scorer=fuzz.WRatio, limit=limit, score_cutoff=70)
    found = [rows[key] for _, _, key in hits]
    return _decorate(conn, found)


def _sparkline(points, width=320, height=64, pad=6):
    """points: [(iso date, price)] -> dict with an SVG path and labels, or None."""
    pts = [(dt.date.fromisoformat(d), p) for d, p in points if p]
    if len(pts) < 2:
        return None
    d0, d1 = pts[0][0], pts[-1][0]
    span = max((d1 - d0).days, 1)
    lo, hi = min(p for _, p in pts), max(p for _, p in pts)
    rng = (hi - lo) or 1
    xy = [(pad + (d - d0).days / span * (width - 2 * pad), height - pad - (p - lo) / rng * (height - 2 * pad))
          for d, p in pts]
    path = "M" + " L".join(f"{x:.1f} {y:.1f}" for x, y in xy)
    return {"path": path, "width": width, "height": height, "low": lo, "high": hi, "first": d0.isoformat(),
            "last": d1.isoformat(), "end": xy[-1]}


def wine(conn, wine_id: int) -> dict | None:
    w = _rows(conn, """SELECT w.*, c.category, c.producer AS cuvee_producer, ws.overall AS score, ws.best_award,
                              ws.gold, ws.cuvee_vintages, ws.cuvee_avg, ws.producer_wines, ws.producer_avg
                       FROM wine w LEFT JOIN cuvee c ON c.id = w.cuvee_id
                       LEFT JOIN wine_score ws ON ws.wine_id = w.id WHERE w.id = ?""", (wine_id,))
    if not w:
        return None
    w = w[0]
    offers = _decorate(conn, _rows(conn, f"SELECT {CARD_COLUMNS} FROM best_buys WHERE wine_id = ? "
                                         "ORDER BY price_per_bottle_at_6", (wine_id,)))
    awards = _rows(conn, """SELECT a.id AS award_id, c.short, c.name AS competition, c.kind, c.weight, a.year,
                                   a.medal, a.points, a.vintage AS rated_vintage, a.source, a.source_url,
                                   a.raw_text, a.found_on, m.confirmed, m.match_score,
                                   (SELECT s.name FROM offer o JOIN shop s ON s.id = o.shop_id
                                     WHERE o.url = a.source_url LIMIT 1) AS shop
                            FROM award_match m JOIN award a ON a.id = m.award_id
                            JOIN competition c ON c.id = a.competition_id
                            WHERE m.wine_id = ? ORDER BY m.confirmed DESC, a.points DESC, a.year DESC""",
                   (wine_id,))
    for a in awards:
        a.update(award_origin(a))
        a["counts_as"] = award_score(a["medal"], a["points"], a["short"])
    other_vintages = _rows(conn, """SELECT w2.id, w2.vintage, ws.overall AS score, ws.best_award
                                    FROM wine w2 JOIN wine_score ws ON ws.wine_id = w2.id
                                    WHERE w2.cuvee_id = ? AND w2.id != ? AND ws.overall IS NOT NULL
                                    ORDER BY w2.vintage DESC""", (w["cuvee_id"], wine_id))
    history = []
    for o in offers:
        pts = conn.execute("""SELECT seen_on, effective_price_per_75cl FROM price_history WHERE offer_id = ?
                              AND effective_price_per_75cl IS NOT NULL ORDER BY seen_on""",
                           (o["offer_id"],)).fetchall()
        line = _sparkline(pts)
        if line:
            history.append({"shop": o["shop"], **line})
    better = []
    if offers and offers[0]["price_per_bottle_at_6"]:
        p = offers[0]["price_per_bottle_at_6"]
        better = _decorate(conn, _rows(conn, f"""SELECT {CARD_COLUMNS} FROM best_buys
            WHERE wine_id != ? AND colour IS ? AND score IS NOT NULL AND online_in_stock IS NOT 'no'
              AND price_per_bottle_at_6 BETWEEN ? AND ? AND price_as_93 < ?
            ORDER BY price_as_93 LIMIT 3""",
            (wine_id, w["colour"], p * 0.75, p * 1.25, offers[0]["price_as_93"] or 0)))
    watched = conn.execute("SELECT 1 FROM watch WHERE wine_id=?", (wine_id,)).fetchone() is not None
    rule = price_rule(conn)
    chart = value_chart(conn, w["colour"], wine_id, rule["rate_pct"]) if w["score"] else None
    try:
        viv = _rows(conn, "SELECT rating, ratings_count, vivino_title, vivino_url, how, checked_on, price "
                          "FROM vivino_rating WHERE wine_id = ? AND (rating IS NOT NULL OR price IS NOT NULL)",
                    (wine_id,))
    except sqlite3.OperationalError:  # database not upgraded yet
        viv = []
    vivino = {"rating": viv[0] if viv else None, "search": vivino_search_link(w["name"], w["vintage"])}
    if viv and viv[0]["rating"]:
        from . import vivino as viv_mod

        c = viv_mod.counted(conn, wine_id)
        vivino["points"] = viv_mod.points(viv[0]["rating"])
        vivino["counts"] = bool(c)
        vivino["weight_pct"] = round(c[0]["weight"] * 100) if c else 0
        vivino["why"] = c[0]["why"] if c else None
        vivino["min_ratings"] = viv_mod.MIN_RATINGS
    return {"wine": w, "offers": offers, "awards": awards, "other_vintages": other_vintages,
            "history": history, "better": better, "watched": watched, "rule": rule, "chart": chart,
            "vivino": vivino}


ORIGIN_NAMES = {"iwc": "International Wine Challenge results", "cmb": "Concours Mondial de Bruxelles results"}


def award_origin(a: dict) -> dict:
    """Where an award comes from, in words and as a link, so it can be checked."""
    source, url = a.get("source") or "", a.get("source_url") or ""
    if source == "shop page":
        return {"origin": f"{a.get('shop') or 'shop'} page", "origin_url": url or None}
    if source == "upload":
        m = re.match(r"upload:(\d+):(.*)", url)
        return {"origin": f"your file {m.group(2)}" if m else "your file",
                "origin_url": f"/upload/{m.group(1)}" if m else "/upload"}
    if source == "manual":
        return {"origin": "entered by hand", "origin_url": None}
    return {"origin": ORIGIN_NAMES.get(source, source or "unknown"),
            "origin_url": url if url.startswith("http") else None}


def order(conn) -> dict:
    lines = _rows(conn, """SELECT i.offer_id, i.qty, b.wine_id, b.wine, b.vintage, b.colour, b.score, b.verdict,
                                  b.promotion, b.url, b.online_in_stock, o.price, o.member_price, s.id AS shop_id, s.name AS shop,
                                  s.website, s.delivery_cost, s.free_delivery_from, s.min_order
                           FROM order_item i JOIN offer o ON o.id = i.offer_id JOIN shop s ON s.id = o.shop_id
                           LEFT JOIN best_buys b ON b.offer_id = i.offer_id
                           ORDER BY s.name, i.added""")
    shops = defaultdict(lambda: {"lines": []})
    for ln in lines:
        ln["cost"] = line_cost(ln["promotion"], ln["price"] or 0, ln["qty"], ln["member_price"])
        ln["normal"] = round((ln["price"] or 0) * ln["qty"], 2)
        deal = parse_deal(ln["promotion"], ln["price"])
        # e.g. "6 halen 5 betalen" with 2 bottles: tell her how many make the promotion count
        ln["promo_needs"] = deal.min_qty if deal and deal.kind != "unknown" and ln["qty"] < deal.min_qty else 0
        sh = shops[ln["shop"]]
        sh.update({k: ln[k] for k in ("shop", "website", "delivery_cost", "free_delivery_from", "min_order")})
        sh["lines"].append(ln)
    out = []
    for sh in shops.values():
        goods = round(sum(ln["cost"] for ln in sh["lines"]), 2)
        bottles = sum(ln["qty"] for ln in sh["lines"])
        free_from = sh["free_delivery_from"]
        delivery = 0.0 if free_from is not None and goods >= free_from else (sh["delivery_cost"] or 0.0)
        sh.update({
            "goods": goods, "bottles": bottles, "delivery": delivery, "total": round(goods + delivery, 2),
            "saved": round(sum(ln["normal"] for ln in sh["lines"]) - goods, 2),
            "to_free_delivery": round(free_from - goods, 2) if free_from and goods < free_from else 0,
            "below_minimum": bool(sh["min_order"] and goods < sh["min_order"]),
            "bottles_short": max(0, 6 - bottles),
        })
        out.append(sh)
    return {"shops": out, "bottles": sum(s["bottles"] for s in out)}


def toggle_watch(conn, wine_id: int) -> bool:
    if conn.execute("SELECT 1 FROM watch WHERE wine_id=?", (wine_id,)).fetchone():
        conn.execute("DELETE FROM watch WHERE wine_id=?", (wine_id,))
        conn.commit()
        return False
    conn.execute("INSERT INTO watch (wine_id, added) VALUES (?, ?)", (wine_id, dt.datetime.now().isoformat()))
    conn.commit()
    return True


def set_order(conn, offer_id: int, qty: int):
    qty = max(0, min(int(qty), 99))
    if qty == 0:
        conn.execute("DELETE FROM order_item WHERE offer_id=?", (offer_id,))
    else:
        conn.execute("""INSERT INTO order_item (offer_id, qty, added) VALUES (?, ?, ?)
                        ON CONFLICT(offer_id) DO UPDATE SET qty=excluded.qty""",
                     (offer_id, qty, dt.datetime.now().isoformat()))
    conn.commit()


def add_to_order(conn, offer_id: int, qty: int = 1) -> int:
    """Add bottles; returns the new number of bottles of this offer in the order list."""
    row = conn.execute("SELECT qty FROM order_item WHERE offer_id=?", (offer_id,)).fetchone()
    new = max(0, min((row[0] if row else 0) + qty, 99))
    set_order(conn, offer_id, new)
    return new


def clear_order(conn):
    conn.execute("DELETE FROM order_item")
    conn.commit()


def watchlist(conn) -> list[dict]:
    rows = _rows(conn, f"""SELECT {CARD_COLUMNS} FROM best_buys
                          WHERE wine_id IN (SELECT wine_id FROM watch) ORDER BY deal = 'yes' DESC, wine""")
    return _decorate(conn, rows)


def week(conn, today: dt.date | None = None) -> list[dict]:
    from .mail import overview

    old = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        sections = overview(conn, today or dt.date.today())
        return [{"title": title, "rows": [dict(r) for r in rows]} for title, rows in sections]
    finally:
        conn.row_factory = old


def nav(conn) -> dict:
    """Counts for the menu bar."""
    return {"order_bottles": conn.execute("SELECT COALESCE(SUM(qty), 0) FROM order_item").fetchone()[0],
            "watched": conn.execute("SELECT COUNT(*) FROM watch").fetchone()[0],
            # award links from your results files that wait for your Yes or No (the same award once)
            "to_check": conn.execute(
                """SELECT COUNT(DISTINCT m.wine_id || '|' || LOWER(COALESCE(a.producer_raw, '') || a.name_raw))
                   FROM award_match m JOIN award a ON a.id = m.award_id
                   WHERE m.confirmed IS NULL AND a.source = 'upload'""").fetchone()[0]}


def money(x) -> str:
    if x is None:
        return ""
    return "€" + f"{x:,.2f}"


FOCUS = (7.0, 15.0)  # the prices Loes buys most: the chart gives them half its height
FOCUS_SHARE = 0.5
_NICE = [1, 1.5, 2, 3, 4, 5, 7.5, 10]  # times 10^n: the labels on the price axis outside the focus


def _nice_prices(lo: float, hi: float) -> list[float]:
    out, e = [], math.floor(math.log10(lo))
    while 10 ** e <= hi * 10:
        out += [round(m * 10 ** e, 6) for m in _NICE[:-1]]
        e += 1
    return sorted(set(out))


class PriceScale:
    """A price axis with the focus prices (€7–15) drawn large, linear inside: one euro is the same
    height everywhere between 7 and 15. Cheaper and dearer wines share the rest of the height,
    on a log scale, so the dearest wine still fits and its price is on the axis."""

    def __init__(self, lo: float, hi: float, focus=FOCUS, share=FOCUS_SHARE):
        f0, f1 = focus
        nice = _nice_prices(lo, hi)
        self.lo = max([p for p in nice if p <= lo * 0.97] or [lo])  # round axis ends, outside every dot
        self.hi = min([p for p in nice if p >= hi * 1.03] or [hi])
        if self.lo < f1 and self.hi > f0:  # wines in or around the focus: show all of it
            self.lo, self.hi = min(self.lo, f0), max(self.hi, f1)
        lo, hi = self.lo, self.hi
        parts = []  # (from, to, kind)
        if lo < f0:
            parts.append((lo, min(f0, hi), "log"))
        if hi > f0 and lo < f1:
            parts.append((max(lo, f0), min(hi, f1), "lin"))
        if hi > f1:
            parts.append((max(lo, f1), hi, "log"))
        outside = sum(math.log(b / a) for a, b, k in parts if k == "log")
        has_focus = any(k == "lin" for _, _, k in parts)
        self.parts, start = [], 0.0
        for a, b, k in parts:
            if k == "lin":
                size = share if outside else 1.0
            else:
                size = (1 - share if has_focus else 1.0) * math.log(b / a) / outside
                if has_focus:
                    size = max(size, 0.12)
            self.parts.append((a, b, k, start, size))
            start += size
        self.total = start
        self.focus = (max(lo, f0), min(hi, f1)) if has_focus else None

    def __call__(self, price: float) -> float:
        """0 at the bottom of the axis, 1 at the top (beyond the ends: continued, for the dashed line)."""
        parts = self.parts
        part = parts[0] if price < parts[0][0] else next((p for p in parts if price <= p[1]), parts[-1])
        a, b, k, start, size = part
        t = (price - a) / (b - a) if k == "lin" else math.log(max(price, 0.01) / a) / math.log(b / a)
        return (start + t * size) / self.total

    def ticks(self, height_px: float, gap_px: float = 13) -> list[float]:
        f = self.focus
        cands = [p for p in _nice_prices(self.lo, self.hi) if self.lo <= p <= self.hi]
        if f:
            cands = [p for p in cands if not f[0] < p < f[1]]
            step = 1 if (self(f[0] + 1) - self(f[0])) * height_px >= gap_px else 2
            cands += [float(e) for e in range(math.ceil(f[0]), math.floor(f[1]) + 1, step)] + [f[1]]
        out = []
        must = {self.lo, self.hi} | (set(f) if f else set())
        def round_first(p):  # €100 before €75, €50 before €30
            m = round(p / 10 ** math.floor(math.log10(p)), 6)
            return [1, 5, 2, 3, 1.5, 7.5, 4].index(m) if m in (1, 5, 2, 3, 1.5, 7.5, 4) else 9

        for p in sorted(set(cands), key=lambda p: (p not in must, p not in must and round_first(p), p)):
            if all(abs(self(p) - self(q)) * height_px >= gap_px for q in out):
                out.append(p)
        return sorted(out)


def value_chart(conn, colour, highlight_wine: int | None = None, rate_pct: float = 20.0,
                width=360, height=300, pad=40) -> dict | None:
    """Price against score for every award wine of one colour, cheapest offer per wine.
    The price axis gives €7–15 half the height (see PriceScale) and reaches the dearest wine.
    Returns SVG coordinates: dots, the 'nothing better for less' line, and the wine's equal-value line."""
    rows = conn.execute("""SELECT wine_id, MIN(price_per_bottle_at_6) AS price, score,
                                  MAX(nothing_better_for_less = 'yes') AS best
                           FROM best_buys WHERE score IS NOT NULL AND colour IS ? AND price_per_bottle_at_6 > 0
                           GROUP BY wine_id""", (colour,)).fetchall()
    if len(rows) < 3:
        return None
    prices = [r[1] for r in rows]
    scores_ = [r[2] for r in rows]
    scale = PriceScale(min(prices), max(prices))
    lo_s, hi_s = math.floor(min(scores_)) - 0.5, math.ceil(max(scores_)) + 0.5
    top, bottom, right = 10, height - pad + 6, width - 8
    plot_h = bottom - top

    def y_of(price):
        return round(bottom - scale(price) * plot_h, 1)

    def xy(score, price):
        return round(pad + (score - lo_s) / (hi_s - lo_s) * (right - pad), 1), y_of(price)

    dots = [{"x": xy(sc, p)[0], "y": xy(sc, p)[1], "me": wid == highlight_wine, "best": bool(best),
             "wine_id": wid, "label": f"{sc:g} points · {money(p)}"} for wid, p, sc, best in rows]
    frontier = sorted(((p, sc) for _, p, sc, best in rows if best), key=lambda t: t[0])
    path = ""
    if len(frontier) >= 2:
        pts = []
        for i, (p, sc) in enumerate(frontier):
            x, y = xy(sc, p)
            if i:
                pts.append(f"{px} {y}")  # step: price rises first, then the score
            pts.append(f"{x} {y}")
            px = x
        path = "M" + " L".join(pts)
    line = ""
    me = [r for r in rows if r[0] == highlight_wine]
    if me:  # the same value as this wine: a curve now that the axis bends at €7 and €15
        _, p0, s0, _ = me[0]
        r = rate_pct / 100
        steps = int((hi_s - lo_s) * 4)
        pts = [xy(lo_s + i / 4, p0 * (1 + r) ** (lo_s + i / 4 - s0)) for i in range(steps + 1)]
        line = "M" + " L".join(f"{x} {y}" for x, y in pts)
    ticks_s = [{"x": xy(t, scale.lo)[0], "label": t} for t in range(math.ceil(lo_s), math.floor(hi_s) + 1, 2)]
    ticks_p = [{"y": y_of(t), "label": f"€{t:g}", "focus": scale.focus and scale.focus[0] <= t <= scale.focus[1]}
               for t in scale.ticks(plot_h)]
    band = None
    if scale.focus:
        band = {"y": y_of(scale.focus[1]), "h": round(y_of(scale.focus[0]) - y_of(scale.focus[1]), 1),
                "label": f"€{scale.focus[0]:g}–{scale.focus[1]:g}"}
    return {"width": width, "height": height, "pad": pad, "top": top, "bottom": bottom, "right": right,
            "dots": dots, "frontier": path, "line": line, "band": band,
            "ticks_s": ticks_s, "ticks_p": ticks_p, "n": len(rows), "max_price": max(prices)}

