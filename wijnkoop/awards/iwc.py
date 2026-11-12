"""International Wine Challenge results (internationalwinechallenge.com, robots.txt: allow all).

The public search lists 20 wines per page and can filter on exact IWC points, so walking
the points 90..100 gives every Silver-or-better wine with its points without opening
each wine's own page.
"""
import re
from urllib.parse import urlencode

from selectolax.lexbor import LexborHTMLParser as HTMLParser

BASE = "https://www.internationalwinechallenge.com"
SHORT = "IWC"
POINTS = range(90, 101)
MEDAL_IMG_RE = re.compile(r"/medals/IWC/\d{4}/([A-Za-z]+)_thumb")
MEDAL_NAMES = {"Trophy": "Trophy", "Gold": "Gold", "Silver": "Silver", "Bronze": "Bronze",
               "Commended": "Commended", "GreatValue": "Great Value"}


def list_url(year: int, points: int, page: int = 1) -> str:
    query = {"wpcat": "WineTab.S", "Challenge_Year": year, "Score": points}
    if page > 1:
        query = {"page": page, **query}
    return f"{BASE}/canopy/search_results?{urlencode(query)}"


def parse_list(html: str, year: int, points: int | None = None) -> list[dict]:
    tree = HTMLParser(html)
    out = []
    for a in tree.css("a.result"):
        href = a.attributes.get("href") or ""
        h2 = a.css_first("h2")
        if not h2:
            continue
        title = re.sub(r"\s+", " ", h2.text()).strip()
        name, _, vintage = title.rpartition(",")
        vintage = vintage.strip()
        if not name:
            name, vintage = title, ""
        producer = region = country = grapes = None
        for p in a.css("p"):
            t = re.sub(r"\s+", " ", p.text()).strip()
            if t.startswith("Produced by "):
                producer = t[len("Produced by "):]
            elif t.startswith("From "):
                where = t[len("From "):]
                region, _, country = where.rpartition(",")
                country = country.strip() or None
                region = region.strip() or None
            elif t.startswith("Made with "):
                grapes = t[len("Made with "):]
        medals = []
        for img in a.css("img"):
            m = MEDAL_IMG_RE.search(img.attributes.get("src") or "")
            if m and m.group(1) in MEDAL_NAMES:
                medals.append(MEDAL_NAMES[m.group(1)])
        out.append({
            "source_url": f"{BASE}/canopy/{href}" if href and not href.startswith("http") else href,
            "name": name.strip(),
            "vintage": int(vintage) if vintage.isdigit() else None,
            "producer": producer,
            "region": region,
            "country": country,
            "grapes": grapes,
            "medals": medals,
            "points": points,
            "year": year,
        })
    return out


def has_next(html: str, page: int) -> bool:
    return f"page={page + 1}&" in html.replace("&amp;", "&")


def awards_from_list(items: list[dict]):
    """One award row per wine for its medal, plus one for Great Value (not a quality score)."""
    for it in items:
        main = next((m for m in ("Trophy", "Gold", "Silver", "Bronze", "Commended") if m in it["medals"]), None)
        if main or it["points"]:
            yield {**it, "medal": main}
        if "Great Value" in it["medals"]:
            yield {**it, "medal": "Great Value", "points": None}
