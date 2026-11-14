"""Reference data: competitions and critics with their weights, shops with delivery costs,
and recognising award text such as "International Wine and Spirit competition 2024 -
Zilver met 90 punten" or "93 punten James Suckling".

Weights (design review, Phase 2):
  1.0  Decanter World Wine Awards, International Wine Challenge, IWSC
  0.7  Concours Mondial de Bruxelles, Mundus Vini, well-known critics
  0.4  everything else
A medal counts as the middle of its own competition's points band: a "Gold" means 95-100 at the
IWC but 85-89 at Gilbert & Gaillard, which is the IWC's bronze.
"""
import html
import re

from .normalize import strip_accents

# (short, full name, kind, weight, recognising pattern on lower-case, accent-free text)
COMPETITIONS = [
    ("DWWA", "Decanter World Wine Awards", "competition", 1.0, r"decanter\s+world\s+wine|\bdwwa\b"),
    ("IWC", "International Wine Challenge", "competition", 1.0, r"international\s+wine\s+challenge|\biwc\b"),
    ("IWSC", "International Wine & Spirit Competition", "competition", 1.0,
     r"international\s+wine\s+(and|&)\s+spirits?\s+competition|\biwsc\b"),
    ("CMB", "Concours Mondial de Bruxelles", "competition", 0.7, r"concours\s+mondial|\bcmb\b"),
    ("Mundus Vini", "Mundus Vini", "competition", 0.7, r"mundus\s+vini"),
    ("Berliner Wein Trophy", "Berliner Wein Trophy", "competition", 0.4, r"berliner\s+wein(\s+troph(y|ies))?"),
    ("Gilbert & Gaillard", "Gilbert & Gaillard", "competition", 0.4, r"gilbert\s*(&|et|and|en)?\s*gaillard"),
    ("Concours Général Agricole", "Concours Général Agricole Paris", "competition", 0.4,
     r"concours\s+general\s+agricole|concours\s+agricole"),
    ("Sakura", "Sakura Japan Women's Wine Awards", "competition", 0.4, r"sakura"),
    ("Decanter", "Decanter (magazine)", "critic", 0.7, r"\bdecanter\b"),
    ("Suckling", "James Suckling", "critic", 0.7, r"suckling"),
    ("Parker", "Robert Parker / Wine Advocate", "critic", 0.7, r"parker|wine\s+advocate"),
    ("Wine Spectator", "Wine Spectator", "critic", 0.7, r"wine\s+spectator"),
    ("Wine Enthusiast", "Wine Enthusiast", "critic", 0.7, r"wine\s+enthusiast"),
    ("Jancis Robinson", "Jancis Robinson", "critic", 0.7, r"jancis"),
    ("Tim Atkin", "Tim Atkin", "critic", 0.7, r"tim\s+atkin"),
    ("Vinous", "Vinous / Antonio Galloni", "critic", 0.7, r"vinous|galloni"),
    ("Jeb Dunnuck", "Jeb Dunnuck", "critic", 0.7, r"dunnuck"),
    ("Falstaff", "Falstaff", "critic", 0.7, r"falstaff"),
    ("Gambero Rosso", "Gambero Rosso", "critic", 0.7, r"gambero\s+rosso"),
    ("Peñín", "Guía Peñín", "critic", 0.7, r"penin"),
    ("Luca Maroni", "Luca Maroni", "critic", 0.4, r"maroni"),
    ("Perswijn", "Perswijn", "critic", 0.4, r"perswijn"),
    ("Hamersma", "De Grote Hamersma", "critic", 0.4, r"hamersma"),
    ("Onno Kleyn", "Onno Kleyn", "critic", 0.4, r"onno\s+kleyn"),
    # Shown on the wine page but not counted (NOT_COUNTED): a shop's own tasting panel, and a
    # rater whose scores appear almost only on that shop's wines (97-99 for EUR 7-9 wines).
    ("Italian Wine Guy", "Italian Wine Guy", "critic", 0.4, r"italian\s+wine\s+guy"),
    ("Proefpanel", "Wijnvoordeel / Wijnbeurs proefpanel", "critic", 0.4, r"proefpanel"),
]
NOT_COUNTED = {"Italian Wine Guy", "Proefpanel"}
OTHER_COMPETITION = ("Other", "Other competition or critic", "competition", 0.4)

# Medal words (Dutch, English, French) -> standard medal
MEDALS = [
    (r"grand\s+gold|grande?\s+medaille\s+d.or|dubbel\s+goud|double\s+gold", "Grand Gold"),
    (r"platin[au]m?|platina", "Platinum"),
    (r"goud|gold|medaille\s+d.or", "Gold"),
    (r"zilver|silver|argent", "Silver"),
    (r"brons|bronze", "Bronze"),
    (r"commended|eervolle\s+vermelding", "Commended"),
    # Last: "Trophy" is often part of a competition's name ("Asia Wine Trophy goud" is a gold)
    (r"best\s+in\s+show|trophy|trofee", "Trophy"),
]
# Score on the 100-point scale for a medal without points: the middle of each competition's own
# points band, from its published rules (Nov 2026). The same word means very different things:
# a gold at Gilbert & Gaillard (85-89) is a bronze at Decanter (86-89).
MEDAL_SCORE_BY_COMPETITION = {
    # Gold 95-100, Silver 90-94, Bronze 85-89, Commended 80-84
    "IWC": {"Trophy": 97, "Gold": 96, "Silver": 92, "Bronze": 87, "Commended": 82},
    "IWSC": {"Trophy": 97, "Gold": 96, "Silver": 92, "Bronze": 87, "Commended": 82},
    # Platinum 97-100, Gold 95-96, Silver 90-94, Bronze 86-89, Commended 83-85
    "DWWA": {"Trophy": 98, "Platinum": 97.5, "Gold": 95.5, "Silver": 92, "Bronze": 87.5, "Commended": 84},
    # Grand Gold 95+, Gold 90-94, Silver 85-89
    "Mundus Vini": {"Grand Gold": 96, "Gold": 92, "Silver": 87},
    # OIV scale: Grand Gold 92+, Gold 85-91, Silver 82-84
    "CMB": {"Trophy": 95, "Grand Gold": 94, "Gold": 88, "Silver": 83},
    "Berliner Wein Trophy": {"Grand Gold": 94, "Gold": 88, "Silver": 83},
    # Double Gold 90+, Gold 85-89, Silver 83-84
    "Gilbert & Gaillard": {"Grand Gold": 92, "Gold": 87, "Silver": 83.5},
    # De Grote Hamersma's medal circles: bronze 7-7.5, silver 8-8.5, gold 9+ out of 10
    "Hamersma": {"Gold": 92, "Silver": 89, "Bronze": 86},
}
# Any other competition: most small ones judge on the OIV scale (Grand Gold 92+, Gold 85-91,
# Silver 82-84, Bronze 80-81). A commended without a known band is no evidence.
MEDAL_SCORE = {"Trophy": 95, "Platinum": 95, "Grand Gold": 94, "Gold": 88, "Silver": 83, "Bronze": 81}
# Critics on their own scale: Luca Maroni scores up to 99 and generously (a Tavernello got 94),
# so his points count this much lower.
CRITIC_OFFSET = {"Luca Maroni": -6}
# Medals worth less than this many points count as no evidence (None: every medal counts).
LOWEST_COUNTED = None

POINTS_RE = re.compile(r"(\d{2,3}(?:[.,]\d)?)\s*(?:/\s*100|punten|points|pts|ptn|p\b|pt\b)")
RANGE_RE = re.compile(r"\b(\d{2,3})\s*[-–]\s*(\d{2,3})\b(?!\s*[-–/]?\s*(?:19|20)\d{2})")
POINTS20_RE = re.compile(r"(\d{1,2}(?:[.,]\d)?)\s*/\s*20")
# "95 van de 100 punten", "95 out of 100": the score, written as 95/100 so the 100 of the scale is not read
OUT_OF_100 = re.compile(r"(?<![\d.,])(\d{2}(?:[.,]\d)?)\s*(?:van\s+de|van|uit|out\s+of|op|of)\s+(?:de\s+)?100(?![\d.,])")
YEAR_RE = re.compile(r"\b(19[89]\d|20[0-4]\d)\b")

# Delivery costs per shop.
SHOPS = [
    ("Gall & Gall", "https://www.gall.nl", 4.95, 55.0, 20.0,
     "EUR 4.95 for orders of EUR 20 to 55, free from EUR 55 (confirmed by Loes, Nov 2026)."),
    ("Vindict", "https://www.vindict.nl", None, 30.0, None,
     "Free delivery from EUR 30 (vindict.nl, Nov 2026); the cost below that is not published."),
    ("Henri Bloem", "https://henribloem.nl", None, 75.0, None,
     "Free delivery in NL from EUR 75 (henribloem.nl, Nov 2026); the cost below that is not on the product pages."),
    ("Okhuysen", "https://okhuysen.nl", None, 115.0, None,
     "Free delivery from EUR 115 (okhuysen.nl, Nov 2026); the cost below that is not on the product pages."),
    ("Wijnvoordeel", "https://www.wijnvoordeel.nl", None, 50.0, None,
     "Free delivery from EUR 50 (wijnvoordeel.nl, Nov 2026); sold per box of 6."),
    ("Wijnbeurs", "https://www.wijnbeurs.nl", None, None, None,
     "Delivery cost not on the product pages; sold per box of 6."),
    ("Grand Cru Wijnen", "https://www.grandcruwijnen.nl", None, None, None,
     "Delivery cost not on the product pages (grandcruwijnen.nl, Nov 2026)."),
    ("Grape District", "https://www.grapedistrict.nl", None, 75.0, None,
     "Free delivery from EUR 75 (grapedistrict.nl, Nov 2026)."),
    ("Vino.com", "https://www.vino.com", None, None, None,
     "Italian shop delivering in NL; the delivery cost is shown only in the basket (vino.com, Nov 2026)."),
    ("Vanouds de Zwaan", "https://vanoudsdezwaan.nl", None, None, None,
     "Delivery cost not in the product data (vanoudsdezwaan.nl, Nov 2026)."),
    ("Vinio", "https://www.vinio.nl", None, None, None,
     "Delivery cost not in the product data (vinio.nl, Nov 2026)."),
    ("Colaris", "https://www.colaris.nl", None, None, None,
     "Delivery cost not on the product pages (colaris.nl, Nov 2026)."),
    ("Wijny", "https://www.wijny.nl", None, 85.0, None,
     "Webshop of Bon Import, Almere. Free delivery of wine from EUR 85 (wijny.nl, Nov 2026)."),
    ("Alex Storms", "https://storms.nl", None, None, None,
     "Wijnkoperij Alex Storms - Mertens. Delivery cost not on the product pages (storms.nl, Nov 2026)."),
    ("Rootring Wijnen", "https://rootring-wijnen.nl", 6.95, 100.0, None,
     "EUR 6.95, free from EUR 100 or within 20 km of Alkmaar; 10% off from 6 bottles (rootring-wijnen.nl, Nov 2026)."),
]


def _plain(text: str) -> str:
    return strip_accents(text or "").lower()


def find_competition(text: str):
    """Return (short, name, kind, weight) for the competition or critic named in text."""
    plain = _plain(text)
    for short, name, kind, weight, pattern in COMPETITIONS:
        if re.search(pattern, plain):
            return short, name, kind, weight
    return None


# A critic's score inside running text: "Robert Parker 95/100", "Wine Advocate(91-93)/100",
# "93 punten James Suckling", "Jancis Robinson 17/20".
_SCORE_IN_TEXT = re.compile(
    # "95 van de 100 punten", "95 out of 100 points", "95 uit 100": the score is the first number,
    # never the 100 of the scale (which also reads as "100 punten")
    r"(?<![\d.,])(\d{2}(?:[.,]\d)?)\s*(?:van\s+de|van|uit|out\s+of|op|of)\s+(?:de\s+)?100(?![\d.,])"
    r"(?:\s*(?:punten|points|pts|ptn)\b)?"
    r"|\(?(?<![\d.,])(\d{2,3}(?:[.,]\d)?)(?:\s*[-–]\s*(\d{2,3}))?\)?\s*"
    r"(?:/\s*100(?!\d)|punten\b|points\b|pts\b|ptn\b)"
    r"|(?<![\d.,])(\d{1,2}(?:[.,]\d)?)\s*/\s*20(?!\d)")


def _critic_near(plain: str, start: int, end: int):
    """The critic or competition named closest before a score (or else right after it)."""
    before, after = plain[max(0, start - 60):start], plain[end:end + 40]
    best = None
    for short, name, kind, weight, pattern in COMPETITIONS:
        for m in re.finditer(pattern, before):
            if not best or m.end() > best[0]:
                best = (m.end(), name)
    if best:
        return best[1]
    first = None
    for short, name, kind, weight, pattern in COMPETITIONS:
        m = re.search(pattern, after)
        if m and (not first or m.start() < first[0]):
            first = (m.start(), name)
    return first[1] if first else None


def awards_in_text(text: str | None) -> list[tuple[str, str]]:
    """[(critic, result)] for every score a description names with its critic, e.g.
    ("Robert Parker / Wine Advocate", "95 punten"). A score without a known critic is left out."""
    plain = _plain(re.sub(r"<[^>]+>", " ", html.unescape(text or "")))
    out = []
    for m in _SCORE_IN_TEXT.finditer(plain):
        critic = _critic_near(plain, m.start(), m.end())
        if not critic:
            continue
        if m.group(1):  # "95 van de 100 punten"
            score = float(m.group(1).replace(",", "."))
            if 50 <= score <= 100 and (critic, f"{score:g} punten") not in out:
                out.append((critic, f"{score:g} punten"))
            continue
        if m.group(4):
            result = f"{m.group(4).replace(',', '.')}/20"
        else:
            low = float(m.group(2).replace(",", "."))
            high = float(m.group(3)) if m.group(3) else None
            if not 50 <= low <= 100 or (high and not low < high <= 100):
                continue
            result = f"{m.group(2)}-{m.group(3)}/100" if high else f"{low:g} punten"
        if (critic, result) not in out:
            out.append((critic, result))
    return out


def parse_award_text(source: str, result: str = "") -> dict | None:
    """Understand one award, e.g. ("International Wine and Spirit competition 2024",
    "Zilver met 90 punten") or ("93 punten James Suckling", "")."""
    text = f"{source} {result}".strip()
    plain = OUT_OF_100.sub(r"\1/100", _plain(text))
    comp = find_competition(text)
    # The competition's own name can hold a medal word ("Berliner Wein Trophy"): leave it out.
    medal_text = plain
    if comp:
        own = next((p for short, _, _, _, p in COMPETITIONS if short == comp[0]), None)
        if own:
            medal_text = re.sub(own, " ", plain)
    medal = None
    for pattern, name in MEDALS:
        if re.search(pattern, medal_text):
            medal = name
            break
    points = None
    # A barrel-sample range ("96-100 punten", "(93-95)") counts as its lower end, never the top.
    m = RANGE_RE.search(plain)
    if m and 80 <= float(m.group(1)) < float(m.group(2)) <= 100:
        points = float(m.group(1))
    m = None if points else POINTS_RE.search(plain)
    if m:
        value = float(m.group(1).replace(",", "."))
        if 50 <= value <= 100:
            points = value
    if points is None:
        m = POINTS20_RE.search(plain)
        if m:  # Jancis Robinson style: 17/20 is about 92.5/100
            points = round(50 + 2.5 * float(m.group(1).replace(",", ".")), 1)
    if not (medal or points):
        return None
    year = None
    m = YEAR_RE.search(plain)
    if m:
        year = int(m.group(1))
    return {
        "competition": comp or OTHER_COMPETITION,
        "year": year,
        "medal": medal,
        "points": points,
        "raw_text": re.sub(r"\s+", " ", text),
    }


def award_score(medal: str | None, points: float | None, competition: str | None = None) -> float | None:
    """Score on the 100-point scale, or None when the award is no evidence of quality."""
    if competition in NOT_COUNTED:
        return None
    if points is not None:
        score = points + CRITIC_OFFSET.get(competition, 0)
    else:
        score = MEDAL_SCORE_BY_COMPETITION.get(competition, {}).get(medal, MEDAL_SCORE.get(medal))
    if score is None or (LOWEST_COUNTED and score < LOWEST_COUNTED):
        return None
    return score


def competition_id(con, comp) -> int:
    short, name, kind, weight = comp
    con.execute(
        "INSERT INTO competition (name, short, kind, weight) VALUES (?,?,?,?) "
        "ON CONFLICT(name) DO NOTHING", (name, short, kind, weight))
    return con.execute("SELECT id FROM competition WHERE name=?", (name,)).fetchone()[0]


def repair_shop_awards(con):
    """Re-read medals of awards taken from shop pages, after a fix in parse_award_text."""
    rows = con.execute("SELECT id, raw_text, medal FROM award WHERE source = 'shop page' AND raw_text IS NOT NULL")
    for award_id, raw, medal in rows.fetchall():
        parsed = parse_award_text(raw)
        if parsed and parsed["medal"] != medal:
            con.execute("UPDATE OR IGNORE award SET medal = ? WHERE id = ?", (parsed["medal"], award_id))


def seed(con):
    for short, name, kind, weight, _ in COMPETITIONS:
        con.execute(
            """INSERT INTO competition (name, short, kind, weight) VALUES (?,?,?,?)
               ON CONFLICT(name) DO UPDATE SET short=excluded.short, kind=excluded.kind,
                 weight=excluded.weight""",
            (name, short, kind, weight))
    competition_id(con, OTHER_COMPETITION)
    repair_shop_awards(con)
    for name, site, cost, free_from, min_order, note in SHOPS:
        con.execute(
            """INSERT INTO shop (name, website, delivery_cost, free_delivery_from, min_order, delivery_note)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(name) DO UPDATE SET website=excluded.website,
                 delivery_cost=excluded.delivery_cost, free_delivery_from=excluded.free_delivery_from,
                 min_order=excluded.min_order, delivery_note=excluded.delivery_note""",
            (name, site, cost, free_from, min_order, note))
