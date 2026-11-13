"""Data for the everyday screens (home, find, search, wine page, order list, watchlist).

Every function takes a plain sqlite3 connection, so the screens can be tested without
a web server. The Datasette plugin in datasette/plugins/ calls these.
"""
import datetime as dt
import sqlite3
from collections import defaultdict

from rapidfuzz import fuzz, process

from .deals import line_cost, parse_deal
from .normalize import norm

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
    "value": "price_quality DESC NULLS LAST, score DESC",
    "score": "score DESC NULLS LAST, price_quality DESC",
    "price": "price_per_bottle_at_6 ASC",
}
CARD_COLUMNS = """offer_id, wine_id, wine, vintage, producer, colour, country, shop, price_per_bottle_at_6,
    normal_price, promotion, deal, deal_reason, score, awards, best_award, value, fair_price, price_quality,
    verdict, gold_or_better, other_vintages_awarded, other_vintages_avg, producer_wines_awarded, producer_avg,
    online_in_stock, url, grapes, category"""


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
        if (c["other_vintages_awarded"] or 0) >= 2 or (c["producer_wines_awarded"] or 0) >= 3:
            tags.append("Wins every year")
        if c["online_in_stock"] == "no":
            tags.append("Out of stock online")
        c["tags"] = tags
    return cards


def price_rule(conn) -> dict:
    pct = _setting(conn, "price_per_point_pct", "35")
    n = int(_setting(conn, "price_fit_wines", "0") or 0)
    return {"per_point_pct": int(pct), "fit_wines": n, "learned": n > 0}


def home(conn) -> dict:
    gold = _rows(conn, f"""SELECT {CARD_COLUMNS} FROM best_buys
        WHERE gold_or_better = 'yes' AND online_in_stock IS NOT 'no'
        ORDER BY price_quality DESC NULLS LAST LIMIT 6""")
    seen = {c["offer_id"] for c in gold}
    deals = [c for c in _rows(conn, f"""SELECT {CARD_COLUMNS} FROM best_buys
        WHERE deal = 'yes' AND score IS NOT NULL AND online_in_stock IS NOT 'no'
        ORDER BY price_quality DESC NULLS LAST LIMIT 12""") if c["offer_id"] not in seen][:6]
    counts = conn.execute(
        """SELECT COUNT(*), SUM(score IS NOT NULL), SUM(deal = 'yes' AND score IS NOT NULL),
                  SUM(gold_or_better = 'yes') FROM best_buys""").fetchone()
    return {"gold": _decorate(conn, gold), "deals": _decorate(conn, deals), "wines": counts[0] or 0,
            "scored": counts[1] or 0, "deal_count": counts[2] or 0, "gold_count": counts[3] or 0,
            "rule": price_rule(conn)}


def options(conn) -> dict:
    countries = [r[0] for r in conn.execute(
        "SELECT country FROM best_buys WHERE country IS NOT NULL GROUP BY country ORDER BY COUNT(*) DESC")]
    return {"colours": COLOURS, "countries": {c: country_en(c) for c in countries}, "sorts": {"value": "Best value", "score": "Best score",
                                                                  "price": "Lowest price"}}


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
    sort = SORTS.get(args.get("sort"), SORTS["value"])
    rows = _rows(conn, f"SELECT {CARD_COLUMNS} FROM best_buys WHERE {' AND '.join(where)} ORDER BY {sort} "
                       f"LIMIT {int(limit)}", params)
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
    awards = _rows(conn, """SELECT c.short, c.name AS competition, c.kind, c.weight, a.year, a.medal, a.points,
                                   a.source, a.source_url, a.raw_text, m.confirmed
                            FROM award_match m JOIN award a ON a.id = m.award_id
                            JOIN competition c ON c.id = a.competition_id
                            WHERE m.wine_id = ? ORDER BY m.confirmed DESC, a.points DESC, a.year DESC""",
                   (wine_id,))
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
              AND price_per_bottle_at_6 BETWEEN ? AND ? AND price_quality > ?
            ORDER BY price_quality DESC LIMIT 3""",
            (wine_id, w["colour"], p * 0.75, p * 1.25, offers[0]["price_quality"] or 0)))
    watched = conn.execute("SELECT 1 FROM watch WHERE wine_id=?", (wine_id,)).fetchone() is not None
    return {"wine": w, "offers": offers, "awards": awards, "other_vintages": other_vintages,
            "history": history, "better": better, "watched": watched, "rule": price_rule(conn)}


def order(conn) -> dict:
    lines = _rows(conn, """SELECT i.offer_id, i.qty, b.wine_id, b.wine, b.vintage, b.colour, b.score, b.verdict,
                                  b.promotion, b.url, b.online_in_stock, o.price, s.id AS shop_id, s.name AS shop,
                                  s.website, s.delivery_cost, s.free_delivery_from, s.min_order
                           FROM order_item i JOIN offer o ON o.id = i.offer_id JOIN shop s ON s.id = o.shop_id
                           LEFT JOIN best_buys b ON b.offer_id = i.offer_id
                           ORDER BY s.name, i.added""")
    shops = defaultdict(lambda: {"lines": []})
    for ln in lines:
        ln["cost"] = line_cost(ln["promotion"], ln["price"] or 0, ln["qty"])
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
            "watched": conn.execute("SELECT COUNT(*) FROM watch").fetchone()[0]}


def money(x) -> str:
    if x is None:
        return ""
    return "€" + f"{x:,.2f}"


def fair_example(per_point_pct: int, price: float = 10.0) -> float:
    """What one point more 'should' cost, for the explanation text."""
    return round(price * (1 + per_point_pct / 100), 2)

