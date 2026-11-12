"""Reference data: competitions and critics with their weights, shops with delivery costs,
and recognising award text such as "International Wine and Spirit competition 2024 -
Zilver met 90 punten" or "93 punten James Suckling".

Weights (design review, Phase 2):
  1.0  Decanter World Wine Awards, International Wine Challenge, IWSC
  0.7  Concours Mondial de Bruxelles, Mundus Vini, well-known critics
  0.4  everything else
Bronze and Commended medals are stored but count as no evidence.
"""
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
    ("Berliner Wein Trophy", "Berliner Wein Trophy", "competition", 0.4, r"berliner\s+wein"),
    ("Gilbert & Gaillard", "Gilbert & Gaillard", "competition", 0.4, r"gilbert\s*(&|et|and)\s*gaillard"),
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
]
OTHER_COMPETITION = ("Other", "Other competition or critic", "competition", 0.4)

# Medal words (Dutch, English, French) -> standard medal
MEDALS = [
    (r"grand\s+gold|grande?\s+medaille\s+d.or|dubbel\s+goud|double\s+gold", "Grand Gold"),
    (r"platin[au]m?|platina", "Platinum"),
    (r"best\s+in\s+show|trophy|trofee", "Trophy"),
    (r"goud|gold|medaille\s+d.or", "Gold"),
    (r"zilver|silver|argent", "Silver"),
    (r"brons|bronze", "Bronze"),
    (r"commended|eervolle\s+vermelding", "Commended"),
]
# Score on the 100-point scale for a medal without points: the middle of each
# competition's own points band. Bronze/Commended: no evidence.
MEDAL_SCORE = {"Trophy": 97, "Platinum": 97, "Grand Gold": 95, "Gold": 92, "Silver": 88}
MEDAL_SCORE_BY_COMPETITION = {
    # Gold 95-100, Silver 90-94
    "IWC": {"Trophy": 97, "Gold": 96, "Silver": 92},
    "IWSC": {"Trophy": 97, "Gold": 96, "Silver": 92},
    # Platinum 97-100, Gold 95-96, Silver 90-94
    "DWWA": {"Trophy": 98, "Platinum": 97.5, "Gold": 95.5, "Silver": 92},
    # OIV scale: Grand Gold 92+, Gold 85-91.9, Silver 82-84.9 (OIV points run lower)
    "CMB": {"Trophy": 95, "Grand Gold": 94, "Gold": 90, "Silver": 87},
    # Grand Gold 95+, Gold 90-94.9, Silver 85-89.9
    "Mundus Vini": {"Grand Gold": 96, "Gold": 92, "Silver": 87.5},
}

POINTS_RE = re.compile(r"(\d{2,3}(?:[.,]\d)?)\s*(?:/\s*100|punten|points|pts|ptn|p\b|pt\b)")
POINTS20_RE = re.compile(r"(\d{1,2}(?:[.,]\d)?)\s*/\s*20")
YEAR_RE = re.compile(r"\b(19[89]\d|20[0-4]\d)\b")

# Delivery costs per shop.
SHOPS = [
    ("Gall & Gall", "https://www.gall.nl", 4.95, 55.0, 20.0,
     "EUR 4.95 for orders of EUR 20 to 55, free from EUR 55 (confirmed by Loes, Nov 2026)."),
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


def parse_award_text(source: str, result: str = "") -> dict | None:
    """Understand one award, e.g. ("International Wine and Spirit competition 2024",
    "Zilver met 90 punten") or ("93 punten James Suckling", "")."""
    text = f"{source} {result}".strip()
    plain = _plain(text)
    comp = find_competition(text)
    medal = None
    for pattern, name in MEDALS:
        if re.search(pattern, plain):
            medal = name
            break
    points = None
    m = POINTS_RE.search(plain)
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
    if medal in ("Bronze", "Commended"):
        return None
    if points is not None:
        return points
    return MEDAL_SCORE_BY_COMPETITION.get(competition, {}).get(medal, MEDAL_SCORE.get(medal))


def competition_id(con, comp) -> int:
    short, name, kind, weight = comp
    con.execute(
        "INSERT INTO competition (name, short, kind, weight) VALUES (?,?,?,?) "
        "ON CONFLICT(name) DO NOTHING", (name, short, kind, weight))
    return con.execute("SELECT id FROM competition WHERE name=?", (name,)).fetchone()[0]


def seed(con):
    for short, name, kind, weight, _ in COMPETITIONS:
        con.execute(
            """INSERT INTO competition (name, short, kind, weight) VALUES (?,?,?,?)
               ON CONFLICT(name) DO UPDATE SET short=excluded.short, kind=excluded.kind,
                 weight=excluded.weight""",
            (name, short, kind, weight))
    competition_id(con, OTHER_COMPETITION)
    for name, site, cost, free_from, min_order, note in SHOPS:
        con.execute(
            """INSERT INTO shop (name, website, delivery_cost, free_delivery_from, min_order, delivery_note)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(name) DO UPDATE SET website=excluded.website,
                 delivery_cost=excluded.delivery_cost, free_delivery_from=excluded.free_delivery_from,
                 min_order=excluded.min_order, delivery_note=excluded.delivery_note""",
            (name, site, cost, free_from, min_order, note))
