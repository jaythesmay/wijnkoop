"""Award wines that none of your shops sells (yet).

Every award from an uploaded results file is kept, also when no shop sells the wine. Those awards
are the list of wines WijnKoop keeps looking for: after every shop run it checks the new wines
against them (relink_uploads), and the data quality run checks all wines again. When one turns up
in a shop it is linked, its award counts, and it is noted here as found.
"""
from collections import defaultdict

from .normalize import cuvee_key, norm

MEDAL_ORDER = {"Trophy": 0, "Grand Gold": 1, "Platinum": 1, "Double Gold": 1, "Gold": 2, "Silver": 3,
               "Bronze": 4, "Commended": 5}
PER_PAGE = 100


def _rank(medal, points):
    if points:
        return (0 if points >= 95 else 2 if points >= 90 else 3 if points >= 87 else 4, -points)
    return (MEDAL_ORDER.get(medal or "", 6), 0)


def _gold(medal, points):
    return (points or 0) >= 93 or MEDAL_ORDER.get(medal or "", 9) <= 2


def not_sold(con, q: str = "", colour: str = "", competition: str = "", gold: str = "", page: int = 1) -> dict:
    """The award wines no shop sells, one row per wine and vintage with all its awards, best first."""
    rows = con.execute(
        """SELECT a.id, a.producer_raw, a.name_raw, a.vintage, a.colour, a.country, a.medal, a.points, a.year,
                  c.short
           FROM award a JOIN competition c ON c.id = a.competition_id
           WHERE a.source = 'upload'
             AND NOT EXISTS (SELECT 1 FROM award_match m WHERE m.award_id = a.id AND COALESCE(m.confirmed, 1) = 1)
        """).fetchall()
    competitions = sorted({r["short"] for r in rows})
    wines = defaultdict(lambda: {"awards": []})
    for r in rows:
        name = r["name_raw"] or ""
        if r["producer_raw"] and norm(r["producer_raw"]) not in norm(name):
            name = f"{r['producer_raw']} {name}"
        w = wines[(cuvee_key(name), r["vintage"])]
        w.setdefault("name", name)
        w.setdefault("vintage", r["vintage"])
        w["colour"] = w.get("colour") or r["colour"]
        w["country"] = w.get("country") or r["country"]
        w["awards"].append({"short": r["short"], "year": r["year"], "medal": r["medal"], "points": r["points"],
                            "rank": _rank(r["medal"], r["points"]), "gold": _gold(r["medal"], r["points"])})
    out = []
    q_norm = norm(q)
    for w in wines.values():
        w["awards"].sort(key=lambda a: a["rank"])
        w["best"] = w["awards"][0]["rank"]
        w["gold"] = any(a["gold"] for a in w["awards"])
        if q_norm and q_norm not in norm(w["name"]):
            continue
        if colour and w["colour"] != colour:
            continue
        if competition and not any(a["short"] == competition for a in w["awards"]):
            continue
        if gold and not w["gold"]:
            continue
        out.append(w)
    out.sort(key=lambda w: (w["best"], -len(w["awards"]), w["name"]))
    page = max(1, page)
    found = [dict(r) for r in con.execute(
        """SELECT e.happened_on, e.wine_id, e.new_value, e.detail, w.name, w.vintage
           FROM event e JOIN wine w ON w.id = e.wine_id
           WHERE e.kind = 'award_found' ORDER BY e.id DESC LIMIT 30""")]
    return {"wines": out[(page - 1) * PER_PAGE: page * PER_PAGE], "total": len(out), "all_total": len(wines),
            "page": page, "pages": max(1, -(-len(out) // PER_PAGE)), "competitions": competitions,
            "found": found, "args": {"q": q, "colour": colour, "competition": competition, "gold": gold}}
