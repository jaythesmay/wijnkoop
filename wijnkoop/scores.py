"""Scores, track record, value and deals. Recomputed after every shop run.

overall  = (sum(w * s) + K * M) / (sum(w) + K)   K = 0.5, M = 88
           s = points, or the medal's score; Bronze/Commended are no evidence;
           w = competition weight (1.0 / 0.7 / 0.4).
track    = awards of OTHER vintages of the same wine, and of the producer's other wines.
           Shown next to the score, never mixed into it.
value    = overall - expected(price), expected(p) = a + b * ln(p), fitted on all wines
           with award evidence (falls back to a = 80, b = 3 with too few wines).
deal     = price per bottle at 6 bottles at least 15% below this offer's own 90-day
           median, or below the cheapest other shop. Until 90 days of history exist,
           the normal (non-promotion) price is the reference.
"""
import datetime as dt
import math
import statistics
from collections import defaultdict

from .deals import order_of_six
from .reference import award_score

K = 0.5
M = 88.0
DEAL_DROP = 0.15
HISTORY_DAYS = 90
DEFAULT_FIT = (80.0, 3.0)


def overall(scored: list[tuple[float, float]]) -> float | None:
    """scored: [(score, weight)]"""
    if not scored:
        return None
    sw = sum(w for _, w in scored)
    return round((sum(s * w for s, w in scored) + K * M) / (sw + K), 1)


def fit_expected(points: list[tuple[float, float]]) -> tuple[float, float]:
    """Least squares fit of score = a + b * ln(price). points: [(price, score)]"""
    pts = [(math.log(p), s) for p, s in points if p and p > 0]
    if len(pts) < 20:
        return DEFAULT_FIT
    mx = sum(x for x, _ in pts) / len(pts)
    my = sum(y for _, y in pts) / len(pts)
    sxx = sum((x - mx) ** 2 for x, _ in pts)
    if sxx == 0:
        return DEFAULT_FIT
    b = sum((x - mx) * (y - my) for x, y in pts) / sxx
    return my - b * mx, b


def _wine_scores(con, now):
    rows = con.execute(
        """SELECT m.wine_id, w.cuvee_id, c.producer_key, a.medal, a.points, a.raw_text,
                  comp.short, comp.weight, a.year
           FROM award_match m
           JOIN award a ON a.id = m.award_id
           JOIN competition comp ON comp.id = a.competition_id
           JOIN wine w ON w.id = m.wine_id
           LEFT JOIN cuvee c ON c.id = w.cuvee_id
           WHERE COALESCE(m.confirmed, 1) = 1"""
    ).fetchall()
    per_wine = defaultdict(list)
    info = {}
    for r in rows:
        s = award_score(r["medal"], r["points"])
        info[r["wine_id"]] = (r["cuvee_id"], r["producer_key"])
        if s is None:
            continue
        label = " ".join(str(x) for x in (r["short"], r["year"], r["medal"],
                                          f"{r['points']:g} pts" if r["points"] else None) if x)
        per_wine[r["wine_id"]].append((s, r["weight"] or 0.4, label))

    wine_overall = {w: overall([(s, wt) for s, wt, _ in v]) for w, v in per_wine.items()}
    by_cuvee = defaultdict(dict)
    by_producer = defaultdict(dict)
    for wine_id, score in wine_overall.items():
        cuvee_id, producer_key = info[wine_id]
        if cuvee_id:
            by_cuvee[cuvee_id][wine_id] = score
        if producer_key:
            by_producer[producer_key][(cuvee_id, wine_id)] = score

    con.execute("DELETE FROM wine_score")
    wines = con.execute(
        """SELECT w.id, w.cuvee_id, c.producer_key FROM wine w LEFT JOIN cuvee c ON c.id = w.cuvee_id
           WHERE w.id IN (SELECT wine_id FROM offer WHERE is_current = 1)
              OR w.id IN (SELECT wine_id FROM award_match)"""
    ).fetchall()
    for w in wines:
        mine = per_wine.get(w["id"], [])
        other_vintages = [s for wid, s in by_cuvee.get(w["cuvee_id"], {}).items() if wid != w["id"]]
        # Producer track: other cuvees only (other vintages are the cuvee track).
        other_wines = [s for (cid, wid), s in by_producer.get(w["producer_key"], {}).items()
                       if cid != w["cuvee_id"]]
        best = max(mine, key=lambda x: x[0])[2] if mine else None
        con.execute(
            """INSERT INTO wine_score (wine_id, n_awards, evidence, overall, best_award, cuvee_vintages,
                                       cuvee_avg, producer_wines, producer_avg, updated)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (w["id"], len(mine), round(sum(x[1] for x in mine), 2), wine_overall.get(w["id"]), best,
             len(other_vintages), round(statistics.mean(other_vintages), 1) if other_vintages else None,
             len(other_wines), round(statistics.mean(other_wines), 1) if other_wines else None, now),
        )
    return wine_overall


def _reference_price(history: list[tuple[str, float]], normal: float | None, today: dt.date):
    """history: [(seen_on, effective price per 75cl)], oldest first."""
    if history:
        first = dt.date.fromisoformat(history[0][0])
        if (today - first).days >= HISTORY_DAYS:
            since = (today - dt.timedelta(days=HISTORY_DAYS)).isoformat()
            recent = [p for d, p in history if d >= since and p]
            if recent:
                return statistics.median(recent), "90-day median"
    return normal, "normal price"


def recompute(con, today: dt.date | None = None):
    today = today or dt.date.today()
    now = dt.datetime.now().isoformat(timespec="seconds")
    wine_overall = _wine_scores(con, now)

    offers = con.execute(
        """SELECT o.id, o.wine_id, o.shop_id, o.price_per_75cl, o.was_price, o.price, o.volume_cl,
                  o.effective_price_per_75cl, o.in_stock_online, s.name AS shop,
                  s.delivery_cost, s.free_delivery_from
           FROM offer o JOIN shop s ON s.id = o.shop_id
           WHERE o.is_current = 1 AND o.gone_since IS NULL"""
    ).fetchall()
    history = defaultdict(list)
    since = (today - dt.timedelta(days=HISTORY_DAYS + 400)).isoformat()
    for r in con.execute(
        """SELECT offer_id, seen_on, effective_price_per_75cl AS p
           FROM price_history WHERE seen_on >= ? AND effective_price_per_75cl IS NOT NULL
           ORDER BY seen_on""", (since,)
    ):
        history[r["offer_id"]].append((r["seen_on"], r["p"]))

    cheapest = defaultdict(list)  # wine_id -> [(effective, shop_id)]
    for o in offers:
        if o["effective_price_per_75cl"] and o["in_stock_online"] != 0:
            cheapest[o["wine_id"]].append((o["effective_price_per_75cl"], o["shop_id"]))

    fit_points = []
    for o in offers:
        s = wine_overall.get(o["wine_id"])
        if s and o["effective_price_per_75cl"]:
            fit_points.append((o["effective_price_per_75cl"], s))
    a, b = fit_expected(fit_points)

    con.execute("DELETE FROM offer_score")
    for o in offers:
        eff = o["effective_price_per_75cl"]
        if not eff:
            continue
        bottle = round(eff * (o["volume_cl"] or 75) / 75, 2)  # per bottle as sold
        total6, delivery = order_of_six(bottle, o["delivery_cost"], o["free_delivery_from"])
        normal = o["price_per_75cl"]
        if o["was_price"] and o["volume_cl"]:
            normal = max(normal or 0, round(o["was_price"] * 75 / o["volume_cl"], 2))
        ref, ref_kind = _reference_price(history.get(o["id"], []), normal, today)
        reasons = []
        if ref and eff <= ref * (1 - DEAL_DROP):
            reasons.append(f"{round(100 * (1 - eff / ref))}% below {ref_kind}")
        others = [p for p, shop in cheapest.get(o["wine_id"], []) if shop != o["shop_id"]]
        if others and eff < min(others):
            reasons.append("cheapest shop")
        score = wine_overall.get(o["wine_id"])
        value = round(score - (a + b * math.log(eff)), 1) if score else None
        con.execute(
            """INSERT INTO offer_score (offer_id, bottle_price_at_6, order_of_6, delivery_at_6, reference_price,
                                        is_deal, deal_reason, value, updated)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (o["id"], bottle, total6, delivery, ref, int(bool(reasons)), "; ".join(reasons) or None,
             value, now),
        )
    con.commit()
    return {"wines_scored": len(wine_overall), "fit": (round(a, 2), round(b, 2))}
