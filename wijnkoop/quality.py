"""Data quality: a run that goes through the database and lists what looks wrong, each with what
WijnKoop suggests and the other things you can choose. You decide on the page /quality, one by
one or for many at once.

What you decide holds when the shops are read again:
- merged wines stay merged (the other shop's name becomes an alias of the wine);
- a wine split off stays apart (it has a cuvee of its own, so the next run finds that first);
- a colour you set (wine.manual_colour), "not a wine" (cuvee.manual_is_wine), a bottle size
  (offer_fix) and one name for a producer (producer_alias) win over what the shop says;
- "fine as it is" / "keep" is remembered, so the same finding is not asked again.

The checks (CHECKS) and how they find things are below; each finder returns Finding objects.
"""
import datetime as dt
import json
import logging
import re
import statistics
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass, field

from rapidfuzz import fuzz, process

from . import scores
from .normalize import cuvee_key, norm, wine_key

log = logging.getLogger("wijnkoop")

SCHEMA = """
CREATE TABLE IF NOT EXISTS dq_issue (
    id INTEGER PRIMARY KEY,
    check_code TEXT NOT NULL,     -- a key of CHECKS
    key TEXT NOT NULL UNIQUE,     -- the same finding has the same key in every run
    title TEXT,
    detail TEXT,
    links TEXT,                   -- JSON [[label, url], ...]
    data TEXT,                    -- JSON: what the actions need (ids)
    suggestion TEXT,              -- the action WijnKoop suggests
    state TEXT NOT NULL DEFAULT 'open',  -- open, done (an action was taken), kept (fine as it is), gone
    resolution TEXT,              -- the action taken, or why it is gone
    first_found TEXT,
    last_found TEXT,
    resolved_on TEXT
);
CREATE INDEX IF NOT EXISTS ix_dq_issue_open ON dq_issue (state, check_code);
-- Bottle sizes you set for a shop's product (the shop says 75 cl, it is a magnum)
CREATE TABLE IF NOT EXISTS offer_fix (
    shop_id INTEGER NOT NULL REFERENCES shop(id),
    shop_product_id TEXT NOT NULL,
    volume_cl REAL,
    PRIMARY KEY (shop_id, shop_product_id)
);
-- One name for a producer that shops spell in different ways
CREATE TABLE IF NOT EXISTS producer_alias (
    from_key TEXT PRIMARY KEY,    -- norm() of the name to replace
    producer TEXT NOT NULL,
    producer_key TEXT NOT NULL
);
"""

COLOUR_LABELS = {"red": "Red", "white": "White", "rosé": "Rosé"}
KEEP = "keep"  # the action that says: fine as it is

# code: (title, what it checks, [(action, label)]); the first action is what "keep" means for it
CHECKS = {
    "probable_award": (
        "Award links to check",
        "An official award was linked to a shop wine with a name that is alike but not the same. "
        "It counts in the score until you say it is another wine.",
        [("same", "Same wine: count it"), ("other", "Another wine: unlink")]),
    "duplicate": (
        "Probably the same wine twice",
        "Two shops sell what looks like one wine under slightly different names, so their prices are not "
        "compared and its awards may count for one of them only.",
        [("merge", "Same wine: merge"), (KEEP, "Different wines: keep apart")]),
    "size_only": (
        "Same wine, other bottle size",
        "Two names that differ only in the bottle size (magnum, 37.5 cl, 1.5 l). A bottle size is not "
        "another wine: they become one wine with a price per bottle size, compared per 75 cl. WijnKoop "
        "does this by itself; the ones it did are under Done, where you can undo one.",
        [("merge", "Same wine: one wine, a price per bottle size"), (KEEP, "Different wines: keep apart")]),
    "split_awards": (
        "One wine with two results in the same competition",
        "A competition gives one result per wine per year. Two different medals from the same competition "
        "and year on one wine mean two wines got mixed up, or one award is linked to the wrong wine.",
        [("keep_best", "Keep the award whose name fits best, unlink the others"),
         (KEEP, "Both are right: keep")]),
    "split_alias": (
        "Joined as one wine: check",
        "A wine from one shop was taken for a wine of another shop because the names are alike. "
        "If they are different wines, split it off.",
        [(KEEP, "Same wine: keep together"), ("split", "Different wines: split off")]),
    "colour_conflict": (
        "Colour does not match the name",
        "The name says one colour (Rosso, Blanc, Rosé...) and the wine is stored with another. "
        "Colours decide which awards can belong to a wine.",
        [("set_name_colour", "Use the colour from the name"), (KEEP, "The stored colour is right")]),
    "colour_missing": (
        "Wines without a colour",
        "Without a colour a wine is left out of the colour filters, and awards are matched less strictly.",
        [("red", "Red"), ("white", "White"), ("rosé", "Rosé"), ("not_wine", "Not a wine: hide"),
         (KEEP, "Leave it")]),
    "not_wine": (
        "Probably not a wine",
        "Gift boxes, tasting packs, glasses or spirits that were stored as a wine.",
        [("not_wine", "Not a wine: hide it"), (KEEP, "It is a wine")]),
    "price_outlier": (
        "Price far from other shops",
        "This shop's price per 75 cl is far below or above what other shops ask for the same wine. "
        "Usually the bottle size is wrong (a half bottle, a magnum or a box read as one 75 cl bottle).",
        [(KEEP, "Fine as it is"), ("size_37.5", "It is a half bottle (37.5 cl)"),
         ("size_50", "It is 50 cl"), ("size_150", "It is a magnum (150 cl)"),
         ("size_450", "It is a box of 6 bottles")]),
    "producer_variant": (
        "One producer, two names",
        "Two producer names that are probably the same producer. One name lets the producer's other "
        "wines count for each other.",
        [("same_a", "Same producer: use the first name"), ("same_b", "Same producer: use the second name"),
         (KEEP, "Different producers")]),
    "orphan": (
        "Wines nobody uses",
        "Wines without a shop offer, award, rating or watch: left over from earlier links.",
        [("delete", "Remove"), (KEEP, "Keep")]),
}


@dataclass
class Finding:
    check: str
    key: str
    title: str
    detail: str = ""
    suggestion: str = KEEP
    data: dict = field(default_factory=dict)
    links: list = field(default_factory=list)


def ensure_tables(con):
    con.executescript(SCHEMA)
    have = {r[1] for r in con.execute("PRAGMA table_info(dq_issue)")}
    for col, kind in (("price", "REAL"), ("price_on", "TEXT"), ("auto", "INTEGER DEFAULT 0")):
        if col not in have:
            con.execute(f"ALTER TABLE dq_issue ADD COLUMN {col} {kind}")
    missing = con.execute("SELECT id, links FROM dq_issue WHERE price_on IS NULL AND state='open'").fetchall()
    if missing:  # issues from before prices were kept: once
        stamp = dt.datetime.now().isoformat(timespec="seconds")
        for r in missing:
            con.execute("UPDATE dq_issue SET price=?, price_on=? WHERE id=?",
                        (issue_price(con, json.loads(r[1] or "[]")), stamp, r[0]))
        con.commit()


# The prices that matter most: these issues come first on the page (ui.FOCUS, Loes's own range)
FOCUS = (7.0, 15.0)
WINE_URL = re.compile(r"^/wine/(\d+)")


def issue_price(con, links) -> float | None:
    """The lowest price a bottle (per 75 cl) of the wines an issue is about, in the shops now."""
    ids = [int(m.group(1)) for _, url in links for m in [WINE_URL.match(url or "")] if m]
    if not ids:
        return None
    row = con.execute(f"""SELECT MIN(COALESCE(price_per_75cl, price)) FROM offer
                          WHERE wine_id IN ({','.join('?' * len(ids))}) AND is_current = 1 AND gone_since IS NULL
                            AND price > 0""", ids).fetchone()
    return round(row[0], 2) if row and row[0] else None


# Bottle sizes in a name (after norm()): "magnum", "150cl", "1 5 l", "37 5 cl", "halve fles"
SIZE_WORDS = re.compile(
    r"\b(?:magnum|dubbele magnum|jeroboam|rehoboam|methusalem|methuselah|salmanazar|balthazar|nebukadnezar|"
    r"nebuchadnezzar|halve|half|halbe|piccolo|fles|flessen|bottle|bouteille|"
    r"\d+(?: \d+)? ?(?:cl|ml|l|ltr|liter|litre|liters))\b")
BOX = re.compile(r"\b\d+ ?x\b|\bdoos\b|\bkist\b|\bbox\b|\bpakket\b")


def strip_size(key: str) -> str:
    """A cuvee key without its bottle size words."""
    return re.sub(r"\s+", " ", SIZE_WORDS.sub(" ", key or "")).strip()


def _wine_link(wine_id, label="wine"):
    return [label, f"/wine/{wine_id}"]


def _cuvee_wine(con, cuvee_id):
    """A wine of this cuvee to link to: one with a shop offer, newest vintage first."""
    r = con.execute("""SELECT w.id FROM wine w WHERE w.cuvee_id=?
                       ORDER BY EXISTS (SELECT 1 FROM offer o WHERE o.wine_id = w.id AND o.is_current = 1) DESC,
                                COALESCE(w.vintage, 0) DESC LIMIT 1""", (cuvee_id,)).fetchone()
    return r["id"] if r else None


def _shops_of_cuvees(con) -> dict[int, set[str]]:
    out = defaultdict(set)
    for r in con.execute("""SELECT w.cuvee_id, s.name FROM offer o JOIN wine w ON w.id = o.wine_id
                            JOIN shop s ON s.id = o.shop_id
                            WHERE o.is_current = 1 AND o.gone_since IS NULL"""):
        out[r["cuvee_id"]].add(r["name"])
    return out


# ---------------------------------------------------------------- the checks

def find_probable_awards(con):
    for r in con.execute(
            """SELECT m.award_id, m.wine_id, m.match_score, a.producer_raw, a.name_raw, a.vintage, a.medal,
                      a.points, a.year, c.short, w.name, w.vintage AS wine_vintage
               FROM award_match m JOIN award a ON a.id = m.award_id JOIN competition c ON c.id = a.competition_id
               JOIN wine w ON w.id = m.wine_id WHERE m.confirmed IS NULL"""):
        result = r["medal"] or (f"{r['points']:g} pts" if r["points"] else "")
        official = " ".join(str(x) for x in (r["producer_raw"], r["name_raw"], r["vintage"]) if x)
        score = round(100 * (r["match_score"] or 0))
        yield Finding(
            "probable_award", f"probable_award:{r['award_id']}:{r['wine_id']}",
            f"{official} ({r['short']} {r['year'] or ''} {result})".replace("  ", " "),
            f"probably {r['name']}{' ' + str(r['wine_vintage']) if r['wine_vintage'] else ''} ({score}% alike)",
            "same" if score >= 85 else "other",
            {"award_id": r["award_id"], "wine_id": r["wine_id"]}, [_wine_link(r["wine_id"], "the shop wine")])


def _name_rest(name, producer_words):
    from .awards.importer import COLOUR_WORDS, STOP

    return " ".join(sorted(set(cuvee_key(name).split()) - producer_words - COLOUR_WORDS - STOP))


def find_duplicates(con):
    """Cuvees of the same producer (or the same first words) whose names, without the producer,
    are nearly the same, and that no conflict (colour, tier, sweetness, age) sets apart."""
    from .awards.importer import _words, conflict, first_words

    shops = _shops_of_cuvees(con)
    rows = [dict(r) for r in con.execute(
        "SELECT id, name, producer, colour, norm_key FROM cuvee WHERE COALESCE(is_wine, 1) = 1")
        if r["id"] in shops]
    blocks = defaultdict(list)
    for r in rows:
        r["pw"] = _words(r["producer"]) or first_words(r["name"])
        for w in r["pw"]:
            blocks[w].append(r)
    flagged = {(min(r[0], r[1]), max(r[0], r[1])): r[2] for r in con.execute(
        "SELECT cuvee_id, other_cuvee_id, match_score FROM same_wine_check")}
    seen = set()
    for block in blocks.values():
        if len(block) > 400:  # a word nearly every wine has: says nothing
            continue
        for i, a in enumerate(block):
            for b in block[i + 1:]:
                pair = (min(a["id"], b["id"]), max(a["id"], b["id"]))
                if pair in seen:
                    continue
                seen.add(pair)
                if a["colour"] and b["colour"] and a["colour"] != b["colour"]:
                    continue
                if strip_size(a["norm_key"]) == strip_size(b["norm_key"]):
                    continue  # only the bottle size differs: find_size_only
                if not (a["pw"] <= b["pw"] or b["pw"] <= a["pw"]):
                    continue
                if len(shops[a["id"]]) == 1 and shops[a["id"]] == shops[b["id"]]:
                    continue  # one shop with two products: that shop knows they differ
                if conflict(a["name"], a["colour"], b["name"], b["colour"], a["pw"] | b["pw"]):
                    continue
                left, right = _name_rest(a["name"], a["pw"] | b["pw"]), _name_rest(b["name"], a["pw"] | b["pw"])
                score = 100.0 if left == right else fuzz.token_sort_ratio(left, right)
                score = max(score, flagged.get(pair, 0))
                if score < 85:
                    continue
                keep, other = (a, b) if a["id"] < b["id"] else (b, a)
                yield Finding(
                    "duplicate", f"duplicate:{pair[0]}:{pair[1]}",
                    f"{keep['name']} / {other['name']}",
                    f"{round(score)}% alike · {', '.join(sorted(shops[keep['id']]))} / "
                    f"{', '.join(sorted(shops[other['id']]))}",
                    "merge" if score >= 93 else KEEP,
                    {"keep": keep["id"], "other": other["id"], "other_key": other["norm_key"]},
                    [_wine_link(_cuvee_wine(con, keep["id"]), "first"), _wine_link(_cuvee_wine(con, other["id"]), "second")])


def find_size_only(con):
    """Wines whose names differ only in a bottle size: one wine, sold in more than one size."""
    shops = _shops_of_cuvees(con)
    groups = defaultdict(list)
    for r in con.execute("SELECT id, norm_key, name, colour FROM cuvee WHERE COALESCE(is_wine, 1) = 1"):
        if r["id"] in shops and not BOX.search(r["norm_key"] or ""):
            groups[strip_size(r["norm_key"])].append(dict(r))
    for base, rows in groups.items():
        if len(rows) < 2 or not base:
            continue
        # the name without a size first: the others join it
        rows.sort(key=lambda r: (r["norm_key"] != base, r["id"]))
        keep = rows[0]
        for other in rows[1:]:
            if keep["colour"] and other["colour"] and keep["colour"] != other["colour"]:
                continue
            k1, k2 = sorted((keep["norm_key"], other["norm_key"]))
            yield Finding(
                "size_only", f"size_only:{k1}|{k2}", f"{keep['name']} / {other['name']}",
                f"only the bottle size differs · {', '.join(sorted(shops[keep['id']]))} / "
                f"{', '.join(sorted(shops[other['id']]))}",
                "merge", {"keep": keep["id"], "other": other["id"], "other_key": other["norm_key"]},
                [_wine_link(_cuvee_wine(con, keep["id"]), "first"), _wine_link(_cuvee_wine(con, other["id"]), "second")])


def find_split_awards(con):
    rows = con.execute(
        """SELECT m.wine_id, a.id, a.competition_id, a.year, a.medal, a.points, a.producer_raw, a.name_raw,
                  c.short, w.name
           FROM award_match m JOIN award a ON a.id = m.award_id JOIN competition c ON c.id = a.competition_id
           JOIN wine w ON w.id = m.wine_id
           WHERE COALESCE(m.confirmed, 1) = 1 AND a.source != 'shop page' AND a.year IS NOT NULL""").fetchall()
    groups = defaultdict(list)
    for r in rows:
        groups[(r["wine_id"], r["competition_id"], r["year"])].append(r)
    for (wine_id, comp, year), awards in groups.items():
        results = {(a["medal"] or "", round(a["points"] or 0)) for a in awards}
        if len(results) < 2:
            continue
        wine = awards[0]["name"]
        fits = sorted(((fuzz.token_sort_ratio(cuvee_key(f"{a['producer_raw'] or ''} {a['name_raw']}"), cuvee_key(wine)),
                        a["id"]) for a in awards), reverse=True)
        said = "; ".join(f"{a['medal'] or (str(round(a['points'])) + ' pts')} for “{a['name_raw']}”" for a in awards)
        yield Finding(
            "split_awards", f"split_awards:{wine_id}:{comp}:{year}",
            f"{wine}: {awards[0]['short']} {year} gave {len(results)} different results",
            said, "keep_best",
            {"wine_id": wine_id, "awards": [a["id"] for a in awards], "best": fits[0][1]}, [_wine_link(wine_id)])


def find_split_alias(con):
    for r in con.execute("""SELECT a.norm_key, a.name, a.match_score, a.shop_id, c.id AS cuvee_id, c.name AS cuvee,
                                   s.name AS shop
                            FROM cuvee_alias a JOIN cuvee c ON c.id = a.cuvee_id LEFT JOIN shop s ON s.id = a.shop_id
                            WHERE a.shop_id IS NOT NULL AND COALESCE(a.match_score, 0) < 95"""):
        if cuvee_key(r["name"]) == cuvee_key(r["cuvee"]):
            continue
        yield Finding(
            "split_alias", f"split_alias:{r['norm_key']}",
            f"{r['name']} ({r['shop'] or 'a shop'}) = {r['cuvee']}?",
            f"joined because the names are {round(r['match_score'] or 0)}% alike", KEEP,
            {"norm_key": r["norm_key"], "cuvee_id": r["cuvee_id"]}, [_wine_link(_cuvee_wine(con, r["cuvee_id"]))])


def _current_wines(con):
    return con.execute(
        """SELECT DISTINCT w.id, w.name, w.producer, w.colour, w.grapes, w.cuvee_id, w.manual_colour
           FROM wine w JOIN offer o ON o.wine_id = w.id LEFT JOIN cuvee c ON c.id = w.cuvee_id
           WHERE o.is_current = 1 AND o.gone_since IS NULL AND COALESCE(c.is_wine, 1) = 1""").fetchall()


def find_colours(con):
    from .awards.importer import _words, name_colours
    from .shops.wijnvoordeel import colour_for

    for r in _current_wines(con):
        named = name_colours(r["name"], _words(r["producer"]))
        if not r["colour"]:
            guess = colour_for(r["name"], r["grapes"])
            yield Finding("colour_missing", f"colour_missing:{r['id']}", r["name"],
                          f"grapes: {r['grapes']}" if r["grapes"] else "",
                          guess if guess in COLOUR_LABELS else KEEP, {"wine_id": r["id"]}, [_wine_link(r["id"])])
        elif len(named) == 1 and r["colour"] not in named and not r["manual_colour"]:
            colour = next(iter(named))
            yield Finding("colour_conflict", f"colour_conflict:{r['id']}", r["name"],
                          f"stored as {COLOUR_LABELS.get(r['colour'], r['colour'])}, the name says "
                          f"{COLOUR_LABELS[colour]}", "set_name_colour",
                          {"wine_id": r["id"], "colour": colour}, [_wine_link(r["id"])])


NOT_WINE = re.compile(
    r"pakket|proefdoos|proefpakket|proeverij|cadeaubon|cadeaukaart|cadeau|gift\s*(?:box|set|card)|geschenk|"
    r"wijnclub|abonnement|\bglas|glazen|karaf|decanteer|kurkentrekker|opener|koeler|\bgin\b|whisk|\brum\b|"
    r"vodka|likeur|grappa|cognac|brandy|\bbier\b|alcoholvrij|alcohol-free|\b0[.,]0\b|olijfolie|azijn", re.I)


def find_not_wine(con):
    for r in con.execute("""SELECT c.id, c.name, MIN(w.id) AS wine_id FROM cuvee c JOIN wine w ON w.cuvee_id = c.id
                            JOIN offer o ON o.wine_id = w.id
                            WHERE COALESCE(c.is_wine, 1) = 1 AND c.manual_is_wine IS NULL AND o.is_current = 1
                            GROUP BY c.id"""):
        m = NOT_WINE.search(r["name"])
        if m:
            yield Finding("not_wine", f"not_wine:{r['id']}", r["name"], f"the name says “{m.group(0)}”",
                          "not_wine", {"cuvee_id": r["id"]}, [_wine_link(r["wine_id"])])


def find_price_outliers(con):
    rows = con.execute("""SELECT o.id, o.wine_id, o.shop_id, o.shop_product_id, o.price, o.volume_cl,
                                 o.price_per_75cl, s.name AS shop, w.name, w.vintage
                          FROM offer o JOIN shop s ON s.id = o.shop_id JOIN wine w ON w.id = o.wine_id
                          WHERE o.is_current = 1 AND o.gone_since IS NULL AND o.price_per_75cl > 0""").fetchall()
    fixed = {(r[0], r[1]) for r in con.execute("SELECT shop_id, shop_product_id FROM offer_fix")}
    by_wine = defaultdict(list)
    for r in rows:
        by_wine[r["wine_id"]].append(r)
    for wine_id, offers in by_wine.items():
        for o in offers:
            if (o["shop_id"], o["shop_product_id"]) in fixed:
                continue
            others = [x["price_per_75cl"] for x in offers if x["shop_id"] != o["shop_id"]]
            if not others:
                continue
            ratio = o["price_per_75cl"] / statistics.median(others)
            if 0.6 <= ratio <= 1.75:  # shops differ, but not by half or double
                continue
            guess = {0.5: "size_37.5", 0.67: "size_50", 2.0: "size_150", 6.0: "size_450"}
            near = min(guess, key=lambda g: abs(ratio - g))
            suggestion = guess[near] if abs(ratio - near) / near < 0.2 else KEEP
            yield Finding(
                "price_outlier", f"price_outlier:{o['id']}",
                f"{o['name']}{' ' + str(o['vintage']) if o['vintage'] else ''} at {o['shop']}",
                f"€{o['price']:.2f} for {o['volume_cl'] or 75:g} cl, so €{o['price_per_75cl']:.2f} per 75 cl; "
                f"other shops €{statistics.median(others):.2f} ({ratio:.1f}×)",
                suggestion, {"shop_id": o["shop_id"], "product": o["shop_product_id"], "offer_id": o["id"]},
                [_wine_link(wine_id)])


def find_producer_variants(con):
    """Producer names with different keys of which one holds all words of the other, or nearly the
    same spelling ("Chateau Musar" / "Château Musar Bekaa")."""
    from .awards.importer import _words

    rows = con.execute("""SELECT c.producer, c.producer_key, COUNT(*) AS n FROM cuvee c
                          WHERE c.producer_key IS NOT NULL AND c.producer IS NOT NULL AND COALESCE(c.is_wine, 1) = 1
                          GROUP BY c.producer_key""").fetchall()
    prods = [(r["producer_key"], r["producer"], r["n"], _words(r["producer"])) for r in rows]
    by_word = defaultdict(list)
    for i, p in enumerate(prods):
        for w in p[3]:
            by_word[w].append(i)
    seen = set()
    for i, (key, name, n, words) in enumerate(prods):
        if not words:
            continue
        for j in {j for w in words for j in by_word[w] if len(by_word[w]) <= 60} - {i}:
            pair = (min(i, j), max(i, j))
            if pair in seen:
                continue
            seen.add(pair)
            k2, name2, n2, words2 = prods[j]
            subset = words2 and (words <= words2 or words2 <= words) and min(len(words), len(words2)) >= 1 \
                and max(len(w) for w in (words & words2)) >= 4
            close = fuzz.ratio(" ".join(sorted(words)), " ".join(sorted(words2))) >= 92
            if not (subset or close):
                continue
            a, b = (prods[i], prods[j]) if (n, -i) >= (n2, -j) else (prods[j], prods[i])
            yield Finding(
                "producer_variant", f"producer_variant:{min(a[0], b[0])}|{max(a[0], b[0])}",
                f"{a[1]} / {b[1]}", f"{a[2]} wine{'s' if a[2] != 1 else ''} / {b[2]} wine{'s' if b[2] != 1 else ''}",
                "same_a",
                {"a": [a[0], a[1]], "b": [b[0], b[1]]}, [["first", f"/search?q={a[1]}"], ["second", f"/search?q={b[1]}"]])


def find_orphans(con):
    used = set()
    for table in ("offer", "award_match", "watch", "manual_score"):
        used |= {r[0] for r in con.execute(f"SELECT DISTINCT wine_id FROM {table}")}
    for r in con.execute("SELECT id, name, vintage FROM wine"):
        if r["id"] in used:
            continue
        yield Finding("orphan", f"orphan:{r['id']}", f"{r['name']}{' ' + str(r['vintage']) if r['vintage'] else ''}",
                      "no shop, award or watch", "delete", {"wine_id": r["id"]})


FINDERS = [find_probable_awards, find_size_only, find_duplicates, find_split_awards, find_split_alias, find_colours,
           find_not_wine, find_price_outliers, find_producer_variants, find_orphans]


# ---------------------------------------------------------------- the run

def run_checks(con, now: dt.datetime | None = None, look_for_awards: bool = True) -> dict:
    """Go through the database: look again for uploaded award wines in the shops, then run every
    check. New findings are added, findings that are gone are closed, decisions are kept."""
    from .process import process_token

    now = now or dt.datetime.now()
    stamp = now.isoformat(timespec="seconds")
    run_id = con.execute("INSERT INTO run_log (job, started, process, note) VALUES ('quality', ?, ?, ?)",
                         (stamp, process_token(), "looking for award wines in the shops")).lastrowid
    con.commit()
    found_awards = 0
    if look_for_awards:
        from .awards.upload import relink_uploads

        def progress(done, total, found):
            con.execute("UPDATE run_log SET note=? WHERE id=?",
                        (f"looking for award wines in the shops: {done} of {total}, {found} found", run_id))
            con.commit()

        found_awards = relink_uploads(con, everything=True, progress=progress)
    con.execute("UPDATE run_log SET note='running the checks' WHERE id=?", (run_id,))
    con.commit()
    findings = {}
    for finder in FINDERS:
        for f in finder(con):
            findings.setdefault(f.key, f)
    known = {r["key"]: dict(r) for r in con.execute("SELECT id, key, state FROM dq_issue")}
    new = 0
    for key, f in findings.items():
        values = (f.title, f.detail, json.dumps(f.links), json.dumps(f.data), f.suggestion, stamp,
                  issue_price(con, f.links), stamp)
        old = known.get(key)
        if old is None:
            con.execute("""INSERT INTO dq_issue (check_code, key, title, detail, links, data, suggestion, last_found,
                                                 price, price_on, first_found, state)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?, 'open')""",
                        (f.check, key, *values, stamp))
            new += 1
        elif old["state"] == "kept":
            con.execute("UPDATE dq_issue SET last_found=? WHERE id=?", (stamp, old["id"]))
        else:  # open, or done/gone but found again: open (again)
            con.execute("""UPDATE dq_issue SET title=?, detail=?, links=?, data=?, suggestion=?, last_found=?,
                                  price=?, price_on=?, state='open', resolution=NULL, resolved_on=NULL, auto=0
                           WHERE id=?""", (*values, old["id"]))
    gone = 0
    for key, old in known.items():
        if old["state"] == "open" and key not in findings:
            con.execute("UPDATE dq_issue SET state='gone', resolution='no longer found', resolved_on=? WHERE id=?",
                        (stamp, old["id"]))
            gone += 1
    # Only the bottle size differs: done by itself, listed under Done to check (and undo)
    sizes = [r[0] for r in con.execute("SELECT id FROM dq_issue WHERE state='open' AND check_code='size_only'")]
    merged = resolve(con, sizes, "merge")["done"] if sizes else 0
    if sizes:
        con.execute(f"UPDATE dq_issue SET auto=1 WHERE state='done' AND id IN ({','.join('?' * len(sizes))})", sizes)
        scores.mark_dirty(con)
    still = con.execute("SELECT COUNT(*) FROM dq_issue WHERE state='open'").fetchone()[0]
    note = (f"{still} to look at, {new} new, {gone} solved by themselves"
            + (f", {merged} merged by itself (only the bottle size differed)" if merged else "")
            + (f"; {found_awards} award wines found in the shops" if look_for_awards else ""))
    con.execute("UPDATE run_log SET finished=?, pages_ok=?, note=?, process=NULL WHERE id=?",
                (dt.datetime.now().isoformat(timespec="seconds"), len(findings), note, run_id))
    con.commit()
    log.info("Data quality: %s", note)
    return {"open": still, "new": new, "gone": gone, "award_wines_found": found_awards, "size_merged": merged}


def last_run(con) -> dict | None:
    r = con.execute("SELECT * FROM run_log WHERE job='quality' ORDER BY id DESC LIMIT 1").fetchone()
    return dict(r) if r else None


def running(con) -> dict | None:
    from .process import is_alive

    r = last_run(con)
    return r if r and not r["finished"] and is_alive(r["process"]) else None


def weekly_due(con, now: dt.datetime) -> bool:
    r = con.execute("SELECT MAX(started) FROM run_log WHERE job='quality'").fetchone()[0]
    return not r or r < (now - dt.timedelta(days=7)).isoformat(timespec="seconds")


def start_in_background() -> None:
    """The run as its own process, so the page stays quick."""
    proc = subprocess.Popen([sys.executable, "-m", "wijnkoop", "quality"], start_new_session=True,
                            stdin=subprocess.DEVNULL)
    import threading

    threading.Thread(target=proc.wait, daemon=True).start()


def recompute_in_background() -> None:
    """Scores again after merges, splits and unlinks, without holding up the page."""
    proc = subprocess.Popen([sys.executable, "-m", "wijnkoop", "scores"], start_new_session=True,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
    import threading

    threading.Thread(target=proc.wait, daemon=True).start()


# ---------------------------------------------------------------- what the page shows

PER_PAGE = 100


# J and N on the page: the action for "the same" and for "not the same", per check
YES_NO = {"probable_award": ("same", "other"), "duplicate": ("merge", KEEP), "size_only": ("merge", KEEP),
          "split_alias": (KEEP, "split"), "producer_variant": ("same_a", KEEP)}
BAND = (f"CASE WHEN price BETWEEN {FOCUS[0]} AND {FOCUS[1]} THEN 0 WHEN price < {FOCUS[0]} THEN 1 "
        "WHEN price > {hi} THEN 2 ELSE 3 END").replace("{hi}", str(FOCUS[1]))


def overview(con, check: str | None = None, state: str = "open", page: int = 1, focus: bool = False) -> dict:
    counts = {r["check_code"]: r["n"] for r in con.execute(
        "SELECT check_code, COUNT(*) AS n FROM dq_issue WHERE state='open' GROUP BY check_code")}
    checks = [{"code": code, "title": t, "explain": e, "open": counts.get(code, 0), "actions": a}
              for code, (t, e, a) in CHECKS.items()]
    check = check if check in CHECKS else None
    where, args = ["state=?"], [state if state in ("open", "done", "kept", "gone") else "open"]
    if check:
        where.append("check_code=?")
        args.append(check)
    in_focus = con.execute(f"SELECT COUNT(*) FROM dq_issue WHERE {' AND '.join(where)} AND price BETWEEN ? AND ?",
                           (*args, *FOCUS)).fetchone()[0]
    if focus:
        where.append("price BETWEEN ? AND ?")
        args.extend(FOCUS)
    total = con.execute(f"SELECT COUNT(*) FROM dq_issue WHERE {' AND '.join(where)}", args).fetchone()[0]
    page = max(1, page)
    # To look at: the wines of €7-15 first, then cheaper, then dearer (cheapest first), then without a price
    order = (f"{BAND}, COALESCE(price, 0), check_code, id" if args[0] == "open"
             else "COALESCE(resolved_on, '') DESC, id")
    issues = []
    for r in con.execute(f"""SELECT * FROM dq_issue WHERE {' AND '.join(where)}
                             ORDER BY {order} LIMIT ? OFFSET ?""", (*args, PER_PAGE, (page - 1) * PER_PAGE)):
        d = dict(r)
        d["links"] = json.loads(d["links"] or "[]")
        d["actions"] = CHECKS.get(d["check_code"], ("", "", []))[2]
        d["check_title"] = CHECKS.get(d["check_code"], (d["check_code"],))[0]
        d["resolution_label"] = {"undone": "Undone: kept apart"}.get(
            d["resolution"], dict(d["actions"]).get(d["resolution"], d["resolution"]))
        d["yes_no"] = YES_NO.get(d["check_code"])
        d["can_undo"] = d["state"] == "done" and d["resolution"] == "merge" and '"other_key"' in (d["data"] or "")
        d["in_focus"] = d["price"] is not None and FOCUS[0] <= d["price"] <= FOCUS[1]
        data = json.loads(d["data"] or "{}")
        if d["check_code"] in ("duplicate", "size_only") and args[0] == "open":
            d["compare"] = compare_cuvees(con, data.get("keep"), data.get("other"))
        issues.append(d)
    auto_new = con.execute("""SELECT COUNT(*) FROM dq_issue WHERE auto=1 AND state='done'
                              AND resolved_on >= COALESCE((SELECT MAX(started) FROM run_log WHERE job='quality'), '')""",
                           ).fetchone()[0]
    auto_total = con.execute("SELECT COUNT(*) FROM dq_issue WHERE auto=1 AND state='done'").fetchone()[0]
    return {"checks": checks, "check": check, "state": args[0], "issues": issues, "total": total, "page": page,
            "pages": max(1, -(-total // PER_PAGE)), "open_total": sum(counts.values()),
            "last_run": last_run(con), "running": running(con), "focus": focus, "in_focus": in_focus,
            "focus_range": FOCUS, "auto_new": auto_new, "auto_total": auto_total}


def compare_cuvees(con, *cuvee_ids) -> dict | None:
    """Two wines side by side: names, producer, colour, vintages, bottle sizes, and each shop's offer
    with its photo, price and price per 75 cl. `diff` names the fields that differ."""
    sides = []
    for cid in cuvee_ids:
        c = con.execute("SELECT id, name, producer, colour FROM cuvee WHERE id=?", (cid,)).fetchone()
        if not c:
            return None
        offers = [dict(o) for o in con.execute(
            """SELECT s.name AS shop, o.title, o.volume_cl, o.price, o.price_per_75cl, o.url, o.image_url,
                      w.vintage, w.id AS wine_id
               FROM offer o JOIN wine w ON w.id = o.wine_id JOIN shop s ON s.id = o.shop_id
               WHERE w.cuvee_id = ? AND o.is_current = 1 AND o.gone_since IS NULL
               ORDER BY o.price_per_75cl IS NULL, o.price_per_75cl LIMIT 6""", (cid,))]
        sides.append({
            "name": c["name"], "producer": c["producer"] or "", "colour": c["colour"] or "",
            "vintages": ", ".join(sorted({str(o["vintage"]) for o in offers if o["vintage"]})) or "no year",
            "sizes": ", ".join(f"{v:g} cl" for v in sorted({o["volume_cl"] for o in offers if o["volume_cl"]})) or "?",
            "image": next((o["image_url"] for o in offers if o["image_url"]), None),
            "offers": offers, "wine_id": offers[0]["wine_id"] if offers else None,
        })
    fields = ("name", "producer", "colour", "vintages", "sizes")
    diff = [f for f in fields if len({norm(str(x[f])) for x in sides}) > 1]
    return {"sides": sides, "diff": diff}


# ---------------------------------------------------------------- doing what you chose

def resolve(con, issue_ids: list[int], action: str | None = None, all_open_of: str | None = None) -> dict:
    """Take this action (None: each issue's own suggestion) on these issues, or on every open issue
    of one check. Returns how many were done and whether scores need working out again."""
    if all_open_of:
        issue_ids = [r[0] for r in con.execute("SELECT id FROM dq_issue WHERE state='open' AND check_code=?",
                                               (all_open_of,))]
    done, skipped, rescore = 0, 0, False
    stamp = dt.datetime.now().isoformat(timespec="seconds")
    for issue_id in issue_ids:
        r = con.execute("SELECT * FROM dq_issue WHERE id=? AND state='open'", (issue_id,)).fetchone()
        if not r:
            skipped += 1
            continue
        act = action or r["suggestion"] or KEEP
        if act not in dict(CHECKS[r["check_code"]][2]):
            skipped += 1
            continue
        data = json.loads(r["data"] or "{}")
        if act != KEEP:
            ok = ACTIONS[r["check_code"]](con, data, act)
            if ok is False:  # the wines it was about are gone already
                con.execute("UPDATE dq_issue SET state='gone', resolution='no longer there', resolved_on=? WHERE id=?",
                            (stamp, issue_id))
                skipped += 1
                continue
            rescore = True
        con.execute("UPDATE dq_issue SET state=?, resolution=?, resolved_on=? WHERE id=?",
                    ("kept" if act == KEEP else "done", act, stamp, issue_id))
        done += 1
    con.commit()
    return {"done": done, "skipped": skipped, "rescore": rescore}


def reopen(con, issue_ids: list[int]) -> int:
    n = 0
    for issue_id in issue_ids:
        n += con.execute("""UPDATE dq_issue SET state='open', resolution=NULL, resolved_on=NULL
                            WHERE id=? AND state='kept'""", (issue_id,)).rowcount
    con.commit()
    return n


def _exists(con, table, row_id):
    return con.execute(f"SELECT 1 FROM {table} WHERE id=?", (row_id,)).fetchone() is not None


def merge_wines(con, keep: int, gone: int):
    """Everything of wine `gone` moves to wine `keep`; then `gone` is removed."""
    if keep == gone:
        return
    con.execute("UPDATE offer SET wine_id=? WHERE wine_id=?", (keep, gone))
    con.execute("""INSERT OR IGNORE INTO award_match (award_id, wine_id, match_score, confirmed)
                   SELECT award_id, ?, match_score, confirmed FROM award_match WHERE wine_id=?""", (keep, gone))
    con.execute("DELETE FROM award_match WHERE wine_id=?", (gone,))
    con.execute("INSERT OR IGNORE INTO watch (wine_id, added) SELECT ?, added FROM watch WHERE wine_id=?", (keep, gone))
    con.execute("DELETE FROM watch WHERE wine_id=?", (gone,))
    con.execute("UPDATE manual_score SET wine_id=? WHERE wine_id=?", (keep, gone))
    con.execute("UPDATE event SET wine_id=? WHERE wine_id=?", (keep, gone))
    con.execute("UPDATE OR IGNORE vivino_rating SET wine_id=? WHERE wine_id=?", (keep, gone))
    con.execute("DELETE FROM vivino_rating WHERE wine_id=?", (gone,))
    con.execute("DELETE FROM wine_score WHERE wine_id=?", (gone,))
    for col in ("producer", "colour", "country", "region", "grapes", "style", "alcohol", "manual_colour"):
        con.execute(f"UPDATE wine SET {col}=(SELECT {col} FROM wine WHERE id=?) WHERE id=? AND {col} IS NULL",
                    (gone, keep))
    con.execute("DELETE FROM wine WHERE id=?", (gone,))


def merge_cuvees(con, keep: int, gone: int) -> bool:
    """One wine sold under two names: the wines (vintages) of `gone` join `keep`, and `gone`'s name
    becomes an alias, so the next shop run puts that shop's product with `keep` too."""
    if keep == gone or not _exists(con, "cuvee", keep) or not _exists(con, "cuvee", gone):
        return False
    for w in con.execute("SELECT id, vintage FROM wine WHERE cuvee_id=?", (gone,)).fetchall():
        same = con.execute("""SELECT id FROM wine WHERE cuvee_id=? AND COALESCE(vintage, 0)=COALESCE(?, 0)
                              ORDER BY id LIMIT 1""", (keep, w["vintage"])).fetchone()
        if same:
            merge_wines(con, same["id"], w["id"])
        else:
            con.execute("UPDATE wine SET cuvee_id=? WHERE id=?", (keep, w["id"]))
    g = con.execute("SELECT * FROM cuvee WHERE id=?", (gone,)).fetchone()
    con.execute("UPDATE cuvee_alias SET cuvee_id=? WHERE cuvee_id=?", (keep, gone))
    con.execute("""INSERT OR REPLACE INTO cuvee_alias (norm_key, cuvee_id, name, shop_id, match_score)
                   VALUES (?, ?, ?, NULL, 100)""", (g["norm_key"], keep, g["name"]))
    con.execute("DELETE FROM same_wine_check WHERE cuvee_id=? OR other_cuvee_id=?", (gone, gone))
    for col in ("producer", "producer_key", "colour", "country", "region", "grapes", "category"):
        con.execute(f"UPDATE cuvee SET {col}=? WHERE id=? AND {col} IS NULL", (g[col], keep))
    con.execute("DELETE FROM cuvee WHERE id=?", (gone,))
    return True


def split_alias(con, norm_key: str, cuvee_id: int) -> bool:
    """A shop's wine that was taken for another shop's wine becomes a wine of its own, now: a cuvee
    of its own (which the next run finds first), with that shop's offers moved to it."""
    a = con.execute("SELECT * FROM cuvee_alias WHERE norm_key=?", (norm_key,)).fetchone()
    if not a:
        return False
    c = con.execute("SELECT * FROM cuvee WHERE id=?", (a["cuvee_id"],)).fetchone()
    con.execute("DELETE FROM cuvee_alias WHERE norm_key=?", (norm_key,))
    con.execute("""INSERT OR IGNORE INTO cuvee (norm_key, name, producer, producer_key, colour, country, region,
                                                grapes, category, is_wine) VALUES (?,?,?,?,?,?,?,?,?,1)""",
                (norm_key, a["name"], c["producer"], c["producer_key"], c["colour"], c["country"], c["region"],
                 c["grapes"], c["category"]))
    new = con.execute("SELECT id FROM cuvee WHERE norm_key=?", (norm_key,)).fetchone()["id"]
    for o in con.execute("""SELECT o.id, o.title, w.vintage, w.colour, w.country, w.region, w.grapes, w.producer
                            FROM offer o JOIN wine w ON w.id = o.wine_id
                            WHERE w.cuvee_id=? AND (? IS NULL OR o.shop_id = ?)""",
                         (a["cuvee_id"], a["shop_id"], a["shop_id"])).fetchall():
        if cuvee_key(o["title"] or "") != norm_key:
            continue
        key = wine_key(a["name"], o["vintage"])
        con.execute("""INSERT INTO wine (norm_key, cuvee_id, name, producer, vintage, colour, country, region, grapes)
                       VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(norm_key) DO UPDATE SET cuvee_id=excluded.cuvee_id""",
                    (key, new, a["name"], o["producer"], o["vintage"], o["colour"], o["country"], o["region"],
                     o["grapes"]))
        wine_id = con.execute("SELECT id FROM wine WHERE norm_key=?", (key,)).fetchone()["id"]
        con.execute("UPDATE offer SET wine_id=? WHERE id=?", (wine_id, o["id"]))
    return True


def set_colour(con, wine_id: int, colour: str) -> bool:
    if not _exists(con, "wine", wine_id):
        return False
    con.execute("UPDATE wine SET colour=?, manual_colour=? WHERE id=?", (colour, colour, wine_id))
    con.execute("""UPDATE cuvee SET colour=? WHERE id=(SELECT cuvee_id FROM wine WHERE id=?)
                   AND (colour IS NULL OR colour != ?)
                   AND NOT EXISTS (SELECT 1 FROM wine w2 WHERE w2.cuvee_id = cuvee.id AND w2.id != ?
                                   AND w2.colour IS NOT NULL AND w2.colour != ?)""",
                (colour, wine_id, colour, wine_id, colour))
    return True


def hide_cuvee(con, cuvee_id: int) -> bool:
    if not _exists(con, "cuvee", cuvee_id):
        return False
    con.execute("UPDATE cuvee SET is_wine=0, manual_is_wine=0 WHERE id=?", (cuvee_id,))
    return True


def _act_probable(con, d, act):
    hit = con.execute("UPDATE award_match SET confirmed=? WHERE award_id=? AND wine_id=?",
                      (1 if act == "same" else 0, d["award_id"], d["wine_id"])).rowcount
    if hit:
        from .awards.upload import _count_links

        src = con.execute("SELECT source_url FROM award WHERE id=? AND source='upload'", (d["award_id"],)).fetchone()
        if src and (src[0] or "").count(":") >= 2:
            _count_links(con, int(src[0].split(":")[1]))
    return bool(hit)


def _act_duplicate(con, d, act):
    return merge_cuvees(con, d["keep"], d["other"])


def undo(con, issue_ids: list[int]) -> int:
    """A merge you don't agree with: the second wine becomes a wine of its own again, and the issue is
    kept as "different wines", so the next run doesn't merge them again."""
    n = 0
    stamp = dt.datetime.now().isoformat(timespec="seconds")
    for issue_id in issue_ids:
        r = con.execute("SELECT * FROM dq_issue WHERE id=? AND state='done' AND resolution='merge'",
                        (issue_id,)).fetchone()
        if not r:
            continue
        d = json.loads(r["data"] or "{}")
        key = d.get("other_key") or con.execute("SELECT norm_key FROM cuvee WHERE id=?",
                                                (d.get("other"),)).fetchone()
        if key and split_alias(con, key if isinstance(key, str) else key[0], d["keep"]):
            con.execute("UPDATE dq_issue SET state='kept', resolution='undone', resolved_on=? WHERE id=?",
                        (stamp, issue_id))
            n += 1
    con.commit()
    return n


def _act_split_awards(con, d, act):
    others = [a for a in d["awards"] if a != d["best"]]
    for award_id in others:
        con.execute("UPDATE award_match SET confirmed=0 WHERE award_id=? AND wine_id=?", (award_id, d["wine_id"]))
    return True


def _act_split_alias(con, d, act):
    return split_alias(con, d["norm_key"], d["cuvee_id"])


def _act_colour(con, d, act):
    if act == "not_wine":
        r = con.execute("SELECT cuvee_id FROM wine WHERE id=?", (d["wine_id"],)).fetchone()
        return hide_cuvee(con, r["cuvee_id"]) if r and r["cuvee_id"] else False
    return set_colour(con, d["wine_id"], d.get("colour") if act == "set_name_colour" else act)


def _act_not_wine(con, d, act):
    return hide_cuvee(con, d["cuvee_id"])


def _act_price(con, d, act):
    volume = float(act.split("_", 1)[1])
    con.execute("INSERT OR REPLACE INTO offer_fix (shop_id, shop_product_id, volume_cl) VALUES (?,?,?)",
                (d["shop_id"], d["product"], volume))
    return True


def _act_producer(con, d, act):
    (to_key, to_name), (from_key, from_name) = (d["a"], d["b"]) if act == "same_a" else (d["b"], d["a"])
    con.execute("INSERT OR REPLACE INTO producer_alias (from_key, producer, producer_key) VALUES (?,?,?)",
                (norm(from_name), to_name, to_key))
    con.execute("UPDATE producer_alias SET producer=?, producer_key=? WHERE producer_key=?", (to_name, to_key, from_key))
    con.execute("UPDATE cuvee SET producer=?, producer_key=? WHERE producer_key=?", (to_name, to_key, from_key))
    con.execute("UPDATE wine SET producer=? WHERE producer=?", (to_name, from_name))
    return True


def _act_orphan(con, d, act):
    wine_id = d["wine_id"]
    used = con.execute("""SELECT EXISTS (SELECT 1 FROM offer WHERE wine_id=:w) OR EXISTS (SELECT 1 FROM award_match WHERE wine_id=:w)
                          OR EXISTS (SELECT 1 FROM watch WHERE wine_id=:w)
                          OR EXISTS (SELECT 1 FROM manual_score WHERE wine_id=:w)""", {"w": wine_id}).fetchone()[0]
    if used or not _exists(con, "wine", wine_id):
        return False
    for table in ("wine_score", "event", "vivino_rating"):
        con.execute(f"DELETE FROM {table} WHERE wine_id=?", (wine_id,))
    con.execute("DELETE FROM wine WHERE id=?", (wine_id,))
    return True


ACTIONS = {"probable_award": _act_probable, "duplicate": _act_duplicate, "size_only": _act_duplicate, "split_awards": _act_split_awards,
           "split_alias": _act_split_alias, "colour_conflict": _act_colour, "colour_missing": _act_colour,
           "not_wine": _act_not_wine, "price_outlier": _act_price, "producer_variant": _act_producer,
           "orphan": _act_orphan}
