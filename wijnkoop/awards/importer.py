"""Read competition results, store them as awards and link them to wines in the shops.

Matching: the producer must share a meaningful word with the shop wine's producer or
name, and the rest of the names must be nearly the same (rapidfuzz token_sort_ratio).
  >= 88   linked and counted in the score
  75-88   linked for review only (not counted), visible on the "Matches to check" page
Same vintage: linked to that wine. Other vintage of the same wine: linked to that
vintage (created without an offer), so it feeds the wine's track record.
"""
import datetime as dt
import logging
from collections import defaultdict

from rapidfuzz import fuzz

from .. import db
from ..fetch import NotAllowed, PoliteClient
from ..normalize import cuvee_key, norm, wine_key
from ..reference import COMPETITIONS, competition_id
from . import cmb, iwc

log = logging.getLogger("wijnkoop")

AUTO = 88
REVIEW = 75
STOP = set("""
s a sa sl slu srl spa ss ltd limited gmbh bv nv lda unipessoal sas sarl scea earl gaec ag kg co cie inc llc pty
and the of de del della dei la le les l di da do dos das du d y e et und van von
wine wines winery vineyard vineyards estate estates bodega bodegas cantina cantine azienda agricola vinicola
vinhos vins vini family famille group company cellars cellar weingut domaine domaines maison maisons
societa sociedade agro pecuaria cooperative cooperativa vignerons producteurs
chateau chateaux castello castillo quinta tenuta tenute cave caves grand saint sainte sant santa san
vina vinas vinedos vigna vigneti finca hacienda
""".split())
# Colour words say nothing about which wine it is once the colours agree.
COLOUR_WORDS = set("""
rose rosado rosato rouge red tinto rosso blanc white blanco bianco branco wit rood
""".split())


def _words(text: str | None) -> set[str]:
    return {w for w in norm(text).split() if w not in STOP and len(w) > 1 and not w.isdigit()}


def _comp(short: str):
    for c in COMPETITIONS:
        if c[0] == short:
            return c[:4]
    raise KeyError(short)


class Matcher:
    def __init__(self, con):
        self.con = con
        self.by_word = defaultdict(set)
        self.cuvees = {}
        rows = con.execute(
            """SELECT c.id, c.name, c.producer, c.colour FROM cuvee c
               WHERE c.is_wine = 1 AND c.id IN (SELECT w.cuvee_id FROM wine w JOIN offer o ON o.wine_id = w.id)"""
        ).fetchall()
        for r in rows:
            pw = _words(r["producer"])
            nw = set(cuvee_key(r["name"]).split()) - COLOUR_WORDS
            self.cuvees[r["id"]] = (r["name"], pw, nw, r["colour"])
            for w in pw or (set(cuvee_key(r["name"]).split()[:2]) - STOP):
                self.by_word[w].add(r["id"])

    def best(self, rec: dict):
        """Return (cuvee_id, score) for the best shop wine, or (None, 0)."""
        pa = _words(rec.get("producer"))
        na = set(cuvee_key(rec["name"]).split()) - COLOUR_WORDS
        candidates = set()
        for w in pa:
            candidates |= self.by_word.get(w, set())
        best = (None, 0.0)
        for cid in candidates:
            name, pw, nw, colour = self.cuvees[cid]
            if rec.get("colour") and colour and rec["colour"] != colour:
                continue
            # The award's producer must name the shop wine's producer (or, when the shop
            # gives no producer, the first words of the wine's name).
            first = set(cuvee_key(name).split()[:2]) - STOP
            if not pa & (pw or first):
                continue
            left = " ".join(sorted(na - pa - pw))
            right = " ".join(sorted(nw - pa - pw))
            score = 100.0 if not left and not right else fuzz.token_sort_ratio(left, right)
            if score > best[1]:
                best = (cid, score)
        return best

    def wine_for(self, cuvee_id: int, vintage: int | None) -> int:
        row = self.con.execute(
            "SELECT id FROM wine WHERE cuvee_id=? AND COALESCE(vintage, 0)=COALESCE(?, 0) ORDER BY id LIMIT 1",
            (cuvee_id, vintage)).fetchone()
        if row:
            return row["id"]
        c = self.con.execute("SELECT * FROM cuvee WHERE id=?", (cuvee_id,)).fetchone()
        self.con.execute(
            """INSERT INTO wine (norm_key, cuvee_id, name, producer, vintage, colour, country, region, grapes)
               VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(norm_key) DO NOTHING""",
            (wine_key(c["name"], vintage), cuvee_id, c["name"], c["producer"], vintage, c["colour"],
             c["country"], c["region"], c["grapes"]))
        return self.con.execute("SELECT id FROM wine WHERE norm_key=?",
                                (wine_key(c["name"], vintage),)).fetchone()["id"]


def match_awards(con, only_unmatched: bool = True, today: dt.date | None = None) -> dict:
    """Link competition awards (not the ones read from shop pages) to shop wines.

    A "new award" event is logged only for awards found today by a source that was read
    before, so the very first import does not flood the weekly overview."""
    today_s = (today or dt.date.today()).isoformat()
    m = Matcher(con)
    where = "a.source != 'shop page'"
    if only_unmatched:
        where += " AND NOT EXISTS (SELECT 1 FROM award_match x WHERE x.award_id = a.id)"
    rows = con.execute(
        f"""SELECT a.id, a.producer_raw, a.name_raw, a.vintage, a.colour, a.raw_text,
                   (a.found_on = ? AND EXISTS (SELECT 1 FROM award b WHERE b.source = a.source
                                                AND b.found_on < ?)) AS announce
            FROM award a WHERE {where}""", (today_s, today_s)).fetchall()
    linked = review = 0
    for r in rows:
        cid, score = m.best({"producer": r["producer_raw"], "name": r["name_raw"], "colour": r["colour"]})
        if not cid or score < REVIEW:
            continue
        wine_id = m.wine_for(cid, r["vintage"])
        confirmed = 1 if score >= AUTO else None
        con.execute(
            "INSERT OR IGNORE INTO award_match (award_id, wine_id, match_score, confirmed) VALUES (?,?,?,?)",
            (r["id"], wine_id, round(score / 100, 3), confirmed))
        if confirmed:
            linked += 1
            if not r["announce"]:
                continue
            offer = con.execute("SELECT id, shop_id FROM offer WHERE wine_id=? AND is_current=1 LIMIT 1",
                                (wine_id,)).fetchone()
            if offer:
                con.execute(
                    "INSERT INTO event (happened_on, kind, shop_id, offer_id, wine_id, new_value) VALUES (?,?,?,?,?,?)",
                    (today_s, "new_award", offer["shop_id"], offer["id"], wine_id, r["raw_text"]))
        else:
            review += 1
    con.commit()
    return {"checked": len(rows), "linked": linked, "to_review": review}


def store_award(con, comp_id: int, rec: dict, source: str, today: str) -> bool:
    raw = " ".join(str(x) for x in (rec.get("producer"), rec["name"], rec.get("vintage"), "-", source.upper(),
                                     rec["year"], rec.get("medal"),
                                     f"{rec['points']} pts" if rec.get("points") else None) if x)
    cur = con.execute(
        """INSERT OR IGNORE INTO award (competition_id, year, producer_raw, name_raw, vintage, colour, country,
                                        medal, points, source, source_url, raw_text, found_on)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (comp_id, rec["year"], rec.get("producer"), rec["name"], rec.get("vintage"), rec.get("colour"),
         rec.get("country"), rec.get("medal"), rec.get("points"), source, rec["source_url"], raw, today))
    return bool(cur.rowcount)


def _walk(client, url_for, parse, has_next, max_pages=400):
    page = 1
    while page <= max_pages:
        html = client.get(url_for(page))
        items = parse(html)
        yield from items
        if not items or not has_next(html, page):
            return
        page += 1


def import_iwc(con, year: int, client=None) -> int:
    client = client or PoliteClient(iwc.BASE)
    comp_id = competition_id(con, _comp("IWC"))
    today = dt.date.today().isoformat()
    new = 0
    for points in iwc.POINTS:
        items = _walk(client, lambda page, p=points: iwc.list_url(year, p, page),
                      lambda html, p=points: iwc.parse_list(html, year, p), iwc.has_next)
        for rec in iwc.awards_from_list(list(items)):
            new += store_award(con, comp_id, rec, "iwc", today)
        con.commit()
        log.info("IWC %d: %d points done, %d new awards so far", year, points, new)
    return new


def import_cmb(con, year: int, client=None) -> int:
    client = client or PoliteClient(cmb.BASE, min_delay=cmb.MIN_DELAY)
    comp_id = competition_id(con, _comp("CMB"))
    today = dt.date.today().isoformat()
    new = 0
    for colour in cmb.COLOURS:
        items = _walk(client, lambda page, c=colour: cmb.list_url(year, c, page),
                      lambda html, c=colour: cmb.parse_list(html, year, c), cmb.has_next)
        for rec in items:
            if rec["medal"]:
                new += store_award(con, comp_id, rec, "cmb", today)
        con.commit()
        log.info("CMB %d: %s done, %d new awards so far", year, colour, new)
    return new


IMPORTERS = {"iwc": import_iwc, "cmb": import_cmb}


def years_to_read(con, source: str, this_year: int) -> list[int]:
    """This year and last year, plus any of the two years before that are still missing
    (they feed the track record; an import cut short by the NAS switching off is redone)."""
    con.execute("CREATE TABLE IF NOT EXISTS award_import (source TEXT, year INTEGER, finished_on TEXT, "
                "PRIMARY KEY (source, year))")
    have = {r[0] for r in con.execute("SELECT year FROM award_import WHERE source=?", (source,))}
    older = [y for y in (this_year - 3, this_year - 2) if y not in have]
    return older + [this_year - 1, this_year]


def run_award_import(con=None, sources=("iwc", "cmb"), today: dt.date | None = None) -> dict:
    con = con or db.connect()
    today = today or dt.date.today()
    started = dt.datetime.now().isoformat(timespec="seconds")
    run_id = con.execute("INSERT INTO run_log (job, started) VALUES ('awards', ?)", (started,)).lastrowid
    con.commit()
    new, failed, notes = 0, 0, []
    for source in sources:
        for year in years_to_read(con, source, today.year):
            con.execute("UPDATE run_log SET note=? WHERE id=?",
                        ("; ".join(notes + [f"busy with {source.upper()} {year}"]), run_id))
            con.commit()
            try:
                n = IMPORTERS[source](con, year)
                con.execute("INSERT OR REPLACE INTO award_import (source, year, finished_on) VALUES (?,?,?)",
                            (source, year, today.isoformat()))
                new += n
                notes.append(f"{source.upper()} {year}: {n} new")
            except NotAllowed as exc:
                notes.append(f"{source.upper()} {year}: not allowed ({exc})")
            except Exception as exc:  # one source failing must not stop the other
                failed += 1
                notes.append(f"{source.upper()} {year}: failed ({exc})")
                log.exception("Award import %s %d failed", source, year)
    result = match_awards(con, today=today)
    notes.append(f"linked {result['linked']}, to check {result['to_review']}")
    con.execute("UPDATE run_log SET finished=?, pages_ok=?, pages_failed=?, note=? WHERE id=?",
                (dt.datetime.now().isoformat(timespec="seconds"), new, failed, "; ".join(notes), run_id))
    con.commit()
    from ..scores import recompute

    recompute(con)
    return {"new_awards": new, **result}


def award_check_interval(day: dt.date) -> int:
    """Days between award checks. Results come out in March (CMB, Mundus Vini), late May to
    June (DWWA, IWC), September (CMB sessions) and late November (IWSC, CMB sessions)."""
    m, d = day.month, day.day
    busy = m in (3, 6, 9) or (m == 5 and d >= 20) or (m == 11 and d >= 20)
    return 7 if busy else 30


def awards_due(con, now: dt.datetime) -> bool:
    row = con.execute("SELECT MAX(started) FROM run_log WHERE job='awards' AND finished IS NOT NULL").fetchone()
    if not row or not row[0]:
        return True
    last = dt.datetime.fromisoformat(row[0])
    return (now - last).days >= award_check_interval(now.date())
