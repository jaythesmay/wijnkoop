"""Scores, track record, value and deals. Recomputed after every shop run.

overall  = (sum(w * s) + K * M) / (sum(w) + K)   K = 0.5, M = 88
           s = points, or the middle of the medal's band in that competition (reference.py);
           w = competition weight (1.0 / 0.7 / 0.4).
track    = awards of OTHER vintages of the same wine, and of the producer's other wines.
           Shown next to the score, never mixed into it.

Value for money (version 2): one yardstick that Loes sets herself, compared with the whole market.
price as a 93 = price per 75cl at 6 bottles * (1 + rate) ^ (93 - score); rate = how much more money
           one point is worth to her (setting points_rate_pct, default 20%: "a 93 for EUR 10 is
           worth the same as a 94 for EUR 12"). Lower is better.
market rank = share of all award wines in all shops (cheapest offer per wine) whose price as a 93
           is higher: 0.85 = cheaper than 85% of award wines. Verdict: >= 0.9 Top buy, >= 0.7 Good buy,
           >= 0.3 Fair, below that Pricey for its score.
value    = the same in points: how many points more than the median award wine for the same money.
nothing better for less = no other wine of the same colour, in stock online, scores higher for the
           same money or less.
fair     = only for information in the expert tables: ln(price) = c + d * score fitted on the shops'
           award wines (one point per wine).
deal     = price per bottle at 6 bottles at least 15% below this offer's own 90-day
           median, or below the cheapest other shop. Until 90 days of history exist,
           the normal (non-promotion) price is the reference.
"""
import bisect
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
DEFAULT_PER_POINT = 0.35
DEFAULT_ANCHOR = (89.0, 12.0)  # score 89 usually costs EUR 12 per bottle
PER_POINT_RANGE = (0.15, 0.50)
MIN_FIT_WINES = 30
VERDICTS = [(0.9, "Top buy"), (0.7, "Good buy"), (0.3, "Fair"), (0.0, "Pricey for its score")]
REF_SCORE = 93.0
DEFAULT_RATE_PCT = 20.0
RATE_RANGE = (5.0, 50.0)
GOLD_POINTS = 93


def overall(scored: list[tuple[float, float]]) -> float | None:
    """scored: [(score, weight)]"""
    if not scored:
        return None
    sw = sum(w for _, w in scored)
    return round((sum(s * w for s, w in scored) + K * M) / (sw + K), 1)


def fit_price(points: list[tuple[float, float]]) -> tuple[float, float, int]:
    """Least squares fit of ln(price) = c + d * score. points: [(price, score)], one per wine.
    Returns (c, d, number of wines used); defaults when there are too few."""
    pts = [(sc, math.log(p)) for p, sc in points if p and p > 0 and sc]
    d0 = math.log(1 + DEFAULT_PER_POINT)
    default = (math.log(DEFAULT_ANCHOR[1]) - d0 * DEFAULT_ANCHOR[0], d0, 0)
    if len(pts) < MIN_FIT_WINES:
        return default
    mx = sum(x for x, _ in pts) / len(pts)
    my = sum(y for _, y in pts) / len(pts)
    sxx = sum((x - mx) ** 2 for x, _ in pts)
    if sxx == 0:
        return default
    d = sum((x - mx) * (y - my) for x, y in pts) / sxx
    d = min(max(d, math.log(1 + PER_POINT_RANGE[0])), math.log(1 + PER_POINT_RANGE[1]))
    return my - d * mx, d, len(pts)


def fair_price(score: float, c: float, d: float) -> float:
    return math.exp(c + d * score)


def verdict(rank: float | None) -> str | None:
    """rank: share of award wines that cost more as a 93 (0..1)."""
    if rank is None:
        return None
    return next(name for limit, name in VERDICTS if rank >= limit)


def rate_pct(con) -> float:
    try:
        row = con.execute("SELECT value FROM setting WHERE key = 'points_rate_pct'").fetchone()
        r = float(row[0]) if row else DEFAULT_RATE_PCT
    except Exception:
        r = DEFAULT_RATE_PCT
    return min(max(r, RATE_RANGE[0]), RATE_RANGE[1])


def price_as(price: float, score: float, rate: float, ref: float = REF_SCORE) -> float:
    """What this wine would cost as a wine of score ref, with rate (0.2 = 20%) per point."""
    return price * (1 + rate) ** (ref - score)


def nothing_better_for_less(items: list[tuple]) -> set:
    """items: [(key, price, score)]. Keys of items for which no other item has a higher score
    at the same price or less."""
    best = set()
    by_price = sorted(items, key=lambda x: x[1])
    i, top = 0, float("-inf")
    while i < len(by_price):
        j = i
        while j < len(by_price) and by_price[j][1] == by_price[i][1]:
            j += 1
        group = by_price[i:j]
        group_top = max(sc for _, _, sc in group)
        for key, _, sc in group:
            if sc >= top and sc >= group_top:
                best.add(key)
        top = max(top, group_top)
        i = j
    return best


def _wine_scores(con, now):
    from .awards.importer import unlink_conflicts

    reparse_shop_awards(con)
    unlink_conflicts(con)
    con.commit()  # not holding the database while the scores are worked out
    rows = con.execute(
        """SELECT m.wine_id, w.cuvee_id, c.producer_key, a.medal, a.points, a.raw_text,
                  comp.short, comp.weight, comp.kind, a.year, a.source
           FROM award_match m
           JOIN award a ON a.id = m.award_id
           JOIN competition comp ON comp.id = a.competition_id
           JOIN wine w ON w.id = m.wine_id
           LEFT JOIN cuvee c ON c.id = w.cuvee_id
           WHERE COALESCE(m.confirmed, 1) = 1"""
    ).fetchall()
    # One result per competition and year: an official list (read or uploaded) beats what a shop prints.
    official = {(r["wine_id"], r["short"], r["year"]) for r in rows if r["source"] != "shop page"}
    official_any = {(w, c) for w, c, _ in official}
    rows = [r for r in rows if r["source"] != "shop page" or not (
        (r["wine_id"], r["short"], r["year"]) in official
        or (r["year"] is None and (r["wine_id"], r["short"]) in official_any))]
    per_wine = defaultdict(list)
    gold = set()
    info = {}
    for r in rows:
        s = award_score(r["medal"], r["points"], r["short"])
        info[r["wine_id"]] = (r["cuvee_id"], r["producer_key"])
        if s is None:
            continue
        label = " ".join(str(x) for x in (r["short"], r["year"], r["medal"],
                                          f"{r['points']:g} pts" if r["points"] else None) if x)
        per_wine[r["wine_id"]].append((s, r["weight"] or 0.4, label))
        # "Gold or better": an award worth 93+ (an IWC or Decanter gold, a critic's 93), not every
        # medal called gold (a Gilbert & Gaillard gold is 85-89).
        if s >= GOLD_POINTS:
            gold.add(r["wine_id"])

    # The Vivino rating of the public counts lightly, as a small competition, never as gold.
    from . import vivino

    for v in vivino.counted(con):
        if v["wine_id"] not in info:
            r = con.execute("""SELECT w.cuvee_id, c.producer_key FROM wine w LEFT JOIN cuvee c ON c.id = w.cuvee_id
                               WHERE w.id = ?""", (v["wine_id"],)).fetchone()
            if not r:
                continue
            info[v["wine_id"]] = (r[0], r[1])
        per_wine[v["wine_id"]].append((v["points"], v["weight"], v["label"]))

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
        """SELECT w.id, w.cuvee_id, c.producer_key, w.name || COALESCE(' ' || w.vintage, '') AS label
           FROM wine w LEFT JOIN cuvee c ON c.id = w.cuvee_id
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
                                       cuvee_avg, producer_wines, producer_avg, updated, gold, wine)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (w["id"], len(mine), round(sum(x[1] for x in mine), 2), wine_overall.get(w["id"]), best,
             len(other_vintages), round(statistics.mean(other_vintages), 1) if other_vintages else None,
             len(other_wines), round(statistics.mean(other_wines), 1) if other_wines else None, now,
             int(w["id"] in gold), w["label"]),
        )
    con.commit()
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


def refresh_effective_prices(con) -> int:
    """Apply the current deal rules (and the Premium price) to every current offer, so a change
    in those rules counts without reading all shops again. Returns the number of offers changed."""
    from .bottles import fix_volume
    from .deals import effective_per_75cl, parse_deal

    changed = 0
    for r in con.execute("""SELECT id, title, url, price, volume_cl, promotion, member_price,
                                   effective_price_per_75cl
                            FROM offer WHERE is_current = 1""").fetchall():
        volume = fix_volume(r["volume_cl"], r["title"], r["url"])
        if volume != r["volume_cl"]:
            # A half bottle or magnum stored as 75 cl: correct its size and its price history.
            con.execute("UPDATE offer SET volume_cl=?, price_per_75cl=ROUND(price * 75.0 / ?, 2) WHERE id=?",
                        (volume, volume, r["id"]))
            con.execute("""UPDATE price_history SET effective_price_per_75cl =
                           ROUND(effective_price_per_75cl * ? / ?, 2) WHERE offer_id=?""",
                        (r["volume_cl"] or 75.0, volume, r["id"]))
            changed += 1
        eff = effective_per_75cl(r["price"], volume, r["promotion"], r["member_price"])
        if eff is None or eff == r["effective_price_per_75cl"]:
            continue
        deal = parse_deal(r["promotion"], r["price"])
        con.execute("""UPDATE offer SET effective_price_per_75cl=?, deal_type=?, deal_min_qty=?, deal_factor=?
                       WHERE id=?""", (eff, deal.kind if deal else None, deal.min_qty if deal else None,
                                       deal.factor if deal else None, r["id"]))
        con.execute("""UPDATE price_history SET effective_price_per_75cl=? WHERE offer_id=?
                       AND seen_on = (SELECT MAX(seen_on) FROM price_history WHERE offer_id=?)""",
                    (eff, r["id"], r["id"]))
        changed += 1
    return changed


def recompute(con, today: dt.date | None = None):
    today = today or dt.date.today()
    now = dt.datetime.now().isoformat(timespec="seconds")
    # Short writes with the working-out in between: other runs and the app's own buttons
    # (Save, Read now, Watch) do not have to wait minutes for a shop run's scores.
    refresh_effective_prices(con)
    con.commit()
    wine_overall = _wine_scores(con, now)

    offers = con.execute(
        """SELECT o.id, o.wine_id, o.shop_id, o.price_per_75cl, o.was_price, o.price, o.volume_cl,
                  o.effective_price_per_75cl, o.in_stock_online, s.name AS shop,
                  s.delivery_cost, s.free_delivery_from,
                  w.name || COALESCE(' ' || w.vintage, '') || ' (' || s.name || ')' AS label, w.colour
           FROM offer o JOIN shop s ON s.id = o.shop_id JOIN wine w ON w.id = o.wine_id
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

    cheapest_per_wine = {}
    for o in offers:
        s = wine_overall.get(o["wine_id"])
        eff = o["effective_price_per_75cl"]
        if s and eff and (o["wine_id"] not in cheapest_per_wine or eff < cheapest_per_wine[o["wine_id"]][0]):
            cheapest_per_wine[o["wine_id"]] = (eff, s)
    c, d, n_fit = fit_price(list(cheapest_per_wine.values()))
    con.execute("INSERT OR REPLACE INTO setting (key, value) VALUES ('price_per_point_pct', ?)",
                (str(round(100 * (math.exp(d) - 1))),))
    con.execute("INSERT OR REPLACE INTO setting (key, value) VALUES ('price_fit_wines', ?)", (str(n_fit),))
    con.execute("INSERT OR REPLACE INTO setting (key, value) VALUES ('price_fit', ?)",
                (f"{c:.6f} {d:.6f}",))

    rate = rate_pct(con) / 100
    con.execute("INSERT OR REPLACE INTO setting (key, value) VALUES ('points_rate_pct', ?)", (f"{rate * 100:g}",))
    con.commit()
    # The market: the cheapest offer of every award wine in every shop, as a 93.
    market = {w: price_as(eff, s, rate) for w, (eff, s) in cheapest_per_wine.items()}
    sorted93 = sorted(market.values())
    median93 = statistics.median(market.values()) if market else None
    frontier = set()
    by_colour = defaultdict(list)
    for o in offers:
        s = wine_overall.get(o["wine_id"])
        if s and o["effective_price_per_75cl"] and o["in_stock_online"] != 0:
            by_colour[o["colour"]].append((o["id"], o["effective_price_per_75cl"], s))
    for items in by_colour.values():
        frontier |= nothing_better_for_less(items)

    rows = []
    for o in offers:
        eff = o["effective_price_per_75cl"]
        if not eff:
            continue
        # Every price is compared per 75 cl; an order of 6 is 6 bottles of the size sold.
        bottle = round(eff * (o["volume_cl"] or 75) / 75, 2)
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
        fair = ratio = value = p93 = rank = None
        if score:
            fair = round(fair_price(score, c, d), 2)
            ratio = round(fair / eff, 2)
            p93 = price_as(eff, score, rate)
            # rank of this offer among the market (the cheapest offer of each wine)
            if len(market) >= 2:
                # the wine's own market entry is its cheapest offer, never higher than this one
                rank = round((len(sorted93) - bisect.bisect_right(sorted93, p93)) / (len(sorted93) - 1), 3)
            value = round(math.log(median93 / p93) / math.log(1 + rate), 1) if median93 else None
            p93 = round(p93, 2)
        rows.append((o["id"], round(eff, 2), total6, delivery, ref, int(bool(reasons)), "; ".join(reasons) or None,
                     value, fair, ratio, verdict(rank), now, o["label"], p93, rank, int(o["id"] in frontier)))
    con.execute("DELETE FROM offer_score")
    con.executemany(
        """INSERT INTO offer_score (offer_id, bottle_price_at_6, order_of_6, delivery_at_6, reference_price,
                                    is_deal, deal_reason, value, fair_price, price_quality, verdict, updated,
                                    wine, price_as_93, market_rank, best_for_price)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", rows)
    con.commit()
    return {"wines_scored": len(wine_overall), "per_point_pct": round(100 * (math.exp(d) - 1)),
            "fit_wines": n_fit, "rate_pct": rate * 100}


def reparse_shop_awards(con) -> int:
    """Read the stored text of every award a shop printed again with the current rules, so a
    fix in reading them (such as "96-100 punten" being 96, not 100, or a competition that was not
    recognised) also corrects old ones."""
    import sqlite3

    from .reference import competition_id, parse_award_text

    changed = 0
    for r in con.execute("""SELECT id, raw_text, medal, points, year, competition_id FROM award
                            WHERE source = 'shop page' AND raw_text IS NOT NULL""").fetchall():
        parsed = parse_award_text(r["raw_text"])
        if not parsed:
            continue
        comp_id = competition_id(con, parsed["competition"])  # e.g. "gilbert gaillard" without "&"
        if (parsed["medal"], parsed["points"], parsed["year"], comp_id) == (
                r["medal"], r["points"], r["year"], r["competition_id"]):
            continue
        try:
            con.execute("UPDATE award SET medal=?, points=?, year=?, competition_id=? WHERE id=?",
                        (parsed["medal"], parsed["points"], parsed["year"], comp_id, r["id"]))
        except sqlite3.IntegrityError:  # the corrected award is already stored: drop this copy
            con.execute("DELETE FROM award_match WHERE award_id=?", (r["id"],))
            con.execute("DELETE FROM award WHERE id=?", (r["id"],))
        changed += 1
    return changed
