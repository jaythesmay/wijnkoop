"""Concours Mondial de Bruxelles results (results.concoursmondial.com, robots.txt: allow all;
the main site asks for 10 seconds between requests, which we also use here).

Listed per colour so the colour is known without opening each wine's page.
"""
import re
from urllib.parse import urlencode

from selectolax.lexbor import LexborHTMLParser as HTMLParser

BASE = "https://results.concoursmondial.com"
SHORT = "CMB"
MIN_DELAY = 10
COLOURS = {"red": "red", "white": "white", "rose": "rosé"}
MEDALS = {"grand gold medal": "Grand Gold", "gold medal": "Gold", "silver medal": "Silver",
          "bronze medal": "Bronze"}
YEAR_END_RE = re.compile(r"\s+((?:19|20)\d{2})$")


def list_url(year: int, colour: str, page: int = 1) -> str:
    query = {"search[color]": colour}
    if page > 1:
        query["page"] = page
    return f"{BASE}/en/results/{year}?{urlencode(query)}"


def parse_list(html: str, year: int, colour: str | None = None) -> list[dict]:
    tree = HTMLParser(html)
    out = []
    for card in tree.css(".card-product"):
        link = card.css_first("a.stretched-link")
        title = card.css_first(".card-title")
        if not (link and title):
            continue
        name = re.sub(r"\s+", " ", title.text()).strip()
        vintage = None
        m = YEAR_END_RE.search(name)
        if m:
            vintage = int(m.group(1))
            name = name[: m.start()].strip()
        spans = [re.sub(r"\s+", " ", s.text()).strip() for s in card.css(".card-body span.d-block")]
        medal_node = card.css_first(".card-body .text-primary")
        medal_text = medal_node.text(strip=True).lower() if medal_node else ""
        producer = spans[0] if spans else None
        country = region = None
        if len(spans) > 1:
            country, _, region = spans[1].partition(" - ")
        appellation = spans[2] if len(spans) > 2 and spans[2].lower() not in MEDALS else None
        footer = card.css_first(".card-footer")
        kind = footer.text(strip=True) if footer else ""
        out.append({
            "source_url": link.attributes.get("href"),
            "name": name,
            "vintage": vintage,
            "producer": producer,
            "country": country or None,
            "region": region or None,
            "appellation": appellation,
            "colour": COLOURS.get(colour) if colour else None,
            "kind": kind,
            "medal": MEDALS.get(medal_text),
            "points": None,
            "year": year,
        })
    return out


def has_next(html: str, page: int) -> bool:
    return f"page={page + 1}" in html.replace("&amp;", "&")
