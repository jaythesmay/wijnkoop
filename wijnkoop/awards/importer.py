"""Read competition results, store them as awards and link them to wines in the shops.

Matching: the producer must share a meaningful word with the shop wine's producer or
name, the rest of the names must be nearly the same (rapidfuzz token_sort_ratio), and the
names may not name a different colour, tier, sweetness or age in any language (conflict()).
  >= 88   linked and counted in the score
  75-88   linked for review only (not counted), visible on the "Matches to check" page
Same vintage: linked to that wine. Other vintage of the same wine: linked to that
vintage (created without an offer), so it feeds the wine's track record.
"""
import datetime as dt
import logging
import re
from collections import defaultdict

from rapidfuzz import fuzz

from .. import db
from ..fetch import NotAllowed, PoliteClient
from ..normalize import cuvee_key, norm, strip_accents, wine_key
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
adega adegas kellerei winzergenossenschaft winzer produttori vignaioli coop
""".split())
# Colour words say nothing about which wine it is once the colours agree.
COLOUR_WORDS = set("""
rose rosado rosato rouge red tinto rosso blanc white blanco bianco branco wit rood
""".split())


# A wine's colour, type or age named in any of the usual languages. Two names that both name one
# and name a different one are different wines ("A Descoberta Tinto" is not "A Descoberta Branco"),
# however alike the rest of the names.
COLOURS = {
    "red": "red rouge tinto rosso rood rode rot roter rotwein negre vermelho",
    "white": "white blanc blanco blanca bianco bianca branco wit witte weiss weisser weisswein",
    "rosé": "rose rosado rosato roze rosee blush",
}
COLOUR_OF = {w: c for c, words in COLOURS.items() for w in words.split()}
LABEL_GROUPS = [  # (what it is, [(label, pattern)]) longest first
    ("tier", [("gran reserva", r"gran(?:de)? reserv[ae]|grand reserve"), ("gran selezione", r"gran selezione"),
              ("reserva", r"reserv[ae]|riserva"), ("crianza", r"crianza"), ("roble", r"roble"),
              ("joven", r"joven")]),
    ("sweetness", [("brut nature", r"brut nature|pas dose|dosage zero|zero dosage|non dose"),
                   ("extra brut", r"extra brut"), ("extra dry", r"extra dry|extra sec"),
                   ("demi sec", r"demi sec|semi seco|halbtrocken|medium dry"), ("brut", r"brut"),
                   ("sweet", r"doux|dolce|dulce|sweet|zoet")]),
    ("port", [("tawny", r"tawny"), ("ruby", r"ruby"), ("lbv", r"lbv|late bottled")]),
]
AGE = re.compile(r"\b(\d{1,2})\s*(?:years?|yrs?|anos|ans|jahre|jaar|y\s*o)\b")


def _plain(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", strip_accents((text or "").lower().replace("ß", "ss"))).strip()


def name_colours(name: str | None, skip: set[str] = frozenset()) -> set[str]:
    """Colours named in a wine's name, leaving out words of the producer's name ("Red Newt Cellars")."""
    return {COLOUR_OF[w] for w in _plain(name).split() if w in COLOUR_OF and w not in skip}


def _labels(name: str | None) -> dict[str, str]:
    t = f" {_plain(name)} "
    out = {}
    for group, labels in LABEL_GROUPS:
        for label, pattern in labels:
            if re.search(rf"\b(?:{pattern})\b", t):
                out[group] = label
                break
    m = AGE.search(t)
    if m:
        out["age"] = m.group(1)
    return out


def conflict(award_name: str, award_colour: str | None, wine_name: str, wine_colour: str | None,
             producer_words: set[str] = frozenset()) -> str | None:
    """Why an award cannot be for this wine (a different colour, tier, sweetness or age), or None."""
    a = name_colours(award_name, producer_words) or ({award_colour} if award_colour else set())
    w = name_colours(wine_name, producer_words) or ({wine_colour} if wine_colour else set())
    if a and w and not a & w:
        return f"colour: {'/'.join(sorted(a))} award, {'/'.join(sorted(w))} wine"
    la, lw = _labels(award_name), _labels(wine_name)
    for group in la.keys() & lw.keys():
        if la[group] != lw[group]:
            return f"{group}: {la[group]} award, {lw[group]} wine"
    # "Moscatel de Setubal 10 Anos" is another wine than the plain Moscatel de Setubal
    if ("age" in la) != ("age" in lw):
        return f"age: {la.get('age', 'none')} award, {lw.get('age', 'none')} wine"
    return None


def first_words(name: str | None, n: int = 2) -> set[str]:
    """The first meaningful words of a wine's name, which name its producer when the shop gives none
    ("Adega de Pegoes Moscatel" -> pegoes, moscatel; "Adega" and "de" say nothing)."""
    return set([w for w in cuvee_key(name or "").split() if w not in STOP and not w.isdigit()][:n])


def producers_agree(pa: set[str], award_name: str, pw: set[str], wine_name: str) -> bool:
    """Is the award's producer the shop wine's producer? pa and pw are their producer words
    (_words), empty when not given.

    One shared word is not enough: "Cape Mentelle" is not "Cape Dream". With both producers given,
    one must hold all the words of the other ("Herdade do Esporao" and "Esporao" agree). With one
    of them missing, the other's producer must be named in full in that wine's name."""
    if pa and pw:
        return bool(pa & pw) and (pa <= pw or pw <= pa)
    if pa:
        return pa <= _words(wine_name)
    shop = pw or first_words(wine_name)
    return bool(shop) and shop <= _words(award_name)


def same_producer(award_producer: str | None, wine_name: str, wine_producer: str | None,
                  award_name: str = "") -> bool:
    return producers_agree(_words(award_producer), award_name, _words(wine_producer), wine_name)


def _words(text: str | None) -> set[str]:
    return {w for w in norm(text).split() if w not in STOP and len(w) > 1 and not w.isdigit()}


def _after_producer(name: str, producer_words: set[str]) -> set[str]:
    """The words of a name after the producer part at its start ("Isabelle et Pierre Clement
    Menetou Salon" with producer Clement -> Menetou Salon). Empty when no producer word is near the start."""
    words = cuvee_key(name).split()
    last = max((i for i, w in enumerate(words[:6]) if w in producer_words), default=None)
    return set(words[last + 1:]) - COLOUR_WORDS - STOP if last is not None else set()


def _comp(short: str):
    for c in COMPETITIONS:
        if c[0] == short:
            return c[:4]
    raise KeyError(short)


class Matcher:
    def __init__(self, con, other_than_shop: int | None = None):
        """Shop wines to match against; with other_than_shop, only wines sold by OTHER shops
        (used to recognise the same wine in a second shop)."""
        self.con = con
        self.by_word = defaultdict(set)
        self.cuvees = {}
        rows = con.execute(
            """SELECT c.id, c.name, c.producer, c.colour FROM cuvee c
               WHERE c.is_wine = 1 AND c.id IN (SELECT w.cuvee_id FROM wine w JOIN offer o ON o.wine_id = w.id
                                                WHERE ? IS NULL OR o.shop_id != ?)""",
            (other_than_shop, other_than_shop)).fetchall()
        for r in rows:
            pw = _words(r["producer"])
            nw = set(cuvee_key(r["name"]).split()) - COLOUR_WORDS
            self.cuvees[r["id"]] = (r["name"], pw, nw, r["colour"])
            for w in pw or first_words(r["name"]):
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
            if conflict(rec["name"], rec.get("colour"), name, colour, pa | pw):
                continue
            # The award's producer must name the shop wine's producer (or, when the shop
            # gives no producer, the first words of the wine's name).
            if not producers_agree(pa, rec["name"], pw, name):
                continue
            left = " ".join(sorted(na - pa - pw))
            right = " ".join(sorted(nw - pa - pw))
            score = 100.0 if not left and not right else fuzz.token_sort_ratio(left, right)
            # First names before the producer ("Isabelle et Pierre Clement ...") say nothing either.
            la, ra = _after_producer(rec["name"], pa | pw), _after_producer(name, pa | pw)
            if la and ra:
                score = max(score, fuzz.token_sort_ratio(" ".join(sorted(la)), " ".join(sorted(ra))))
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


def unlink_conflicts(con) -> list[int]:
    """Undo links between an award and a wine of another producer, colour, tier, sweetness or age, made
    before the matching checked these. The awards are linked again with the current rules, and
    a vintage that existed only because of such a link is removed. Returns the award ids."""
    rows = con.execute(
        """SELECT m.award_id, m.wine_id, a.producer_raw, a.name_raw, a.colour AS award_colour,
                  w.name, w.producer, w.colour
           FROM award_match m JOIN award a ON a.id = m.award_id JOIN wine w ON w.id = m.wine_id
           WHERE a.source != 'shop page'""").fetchall()
    undone, wines = [], set()
    for r in rows:
        why = conflict(r["name_raw"], r["award_colour"], r["name"], r["colour"],
                       _words(r["producer_raw"]) | _words(r["producer"]))
        if not why and not same_producer(r["producer_raw"], r["name"], r["producer"], r["name_raw"]):
            why = f"producer: {r['producer_raw'] or r['name_raw']} award, {r['producer'] or r['name']} wine"
        if why:
            log.info("Award %s is not for wine %s (%s): link undone", r["award_id"], r["wine_id"], why)
            con.execute("DELETE FROM award_match WHERE award_id=? AND wine_id=?", (r["award_id"], r["wine_id"]))
            undone.append(r["award_id"])
            wines.add(r["wine_id"])
    for wine_id in wines:
        used = con.execute(
            """SELECT EXISTS (SELECT 1 FROM offer WHERE wine_id=:w) OR EXISTS (SELECT 1 FROM award_match WHERE wine_id=:w)
                      OR EXISTS (SELECT 1 FROM manual_score WHERE wine_id=:w) OR EXISTS (SELECT 1 FROM watch WHERE wine_id=:w)""",
            {"w": wine_id}).fetchone()[0]
        if not used:  # made only to hold that award (another vintage, no shop sells it)
            for table in ("wine_score", "event", "vivino_rating"):
                try:
                    con.execute(f"DELETE FROM {table} WHERE wine_id=?", (wine_id,))
                except Exception:  # vivino_rating may not exist yet
                    pass
            con.execute("DELETE FROM wine WHERE id=?", (wine_id,))
    # The same goes for a shop wine taken for the same wine as one from another shop.
    for r in con.execute("""SELECT a.norm_key, a.name, c.name AS cuvee, c.producer, c.colour
                            FROM cuvee_alias a JOIN cuvee c ON c.id = a.cuvee_id""").fetchall():
        why = conflict(r["name"], None, r["cuvee"], r["colour"], _words(r["producer"]))
        if why:
            # The next run of that shop reads it as a wine of its own.
            log.info("%s is not the same wine as %s (%s)", r["name"], r["cuvee"], why)
            con.execute("DELETE FROM cuvee_alias WHERE norm_key=?", (r["norm_key"],))
    con.commit()
    if undone:
        match_awards(con, award_ids=undone)
    return undone


def match_awards(con, only_unmatched: bool = True, today: dt.date | None = None,
                 award_ids: list[int] | None = None) -> dict:
    """Link competition awards (not the ones read from shop pages) to shop wines.

    A "new award" event is logged only for awards found today by a source that was read
    before, so the very first import does not flood the weekly overview."""
    today_s = (today or dt.date.today()).isoformat()
    m = Matcher(con)
    where = "a.source != 'shop page'"
    if only_unmatched:
        where += " AND NOT EXISTS (SELECT 1 FROM award_match x WHERE x.award_id = a.id)"
    if award_ids is not None:
        where += f" AND a.id IN ({','.join(str(int(i)) for i in award_ids) or 'NULL'})"
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
