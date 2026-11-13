"""Grand Cru Wijnen (grandcruwijnen.nl): product pages from the sitemap.

robots.txt asks for 2-3 seconds; the polite minimum (5 s) applies. Each product page carries
schema.org data with the price, stock, producer and the critics' scores (Parker, Suckling,
Vinous, ... out of 100), a breadcrumb (colour, country, region) and a specification list
(vintage, volume, grapes, alcohol).
"""
import json
import re

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from .gall import GallProduct as Product
from .vindict import _volume_cl

KEY = "grandcru"
SHOP_NAME = "Grand Cru Wijnen"
BASE = "https://www.grandcruwijnen.nl"
SITEMAP = BASE + "/sitemaps/sitemap_nl.xml"

URL_RE = re.compile(r"^https://www\.grandcruwijnen\.nl/(?P<slug>[a-z0-9][a-z0-9-]*)$")
SPECS = {"jaar": "vintage", "land": "country", "regio": "region", "appellatie": "appellation",
         "wijnhuis": "producer", "druiven": "grapes", "alcohol": "alcohol", "inhoud": "volume",
         "wijnsoort": "colour_nl", "drinkvenster": "drink_window"}
COLOURS = {"rood": "red", "rode wijn": "red", "wit": "white", "witte wijn": "white", "rosé": "rosé",
           "rose": "rosé", "rosé wijn": "rosé"}
NOT_WINE = re.compile(r"\bcase\b|\bkist\b|wijnbox|proefpakket|pakket|cadeau|gift|glas|karaf|decanter\b|boek|"
                      r"masterclass|proeverij|olijfolie|gin\b|whisk|rum\b|cognac|armagnac|grappa|likeur", re.I)


def parse_url(url: str) -> dict | None:
    m = URL_RE.match(url.strip())
    return {"product_id": m.group("slug"), "slug": m.group("slug")} if m else None


def product_urls(xml: str) -> list[str]:
    """Product pages are the sitemap entries with a product image; articles and lists have none."""
    out = []
    for entry in re.findall(r"<url>(.*?)</url>", xml, re.S):
        m = re.search(r"<loc>\s*([^<\s]+)", entry)
        if m and "<image:image>" in entry and parse_url(m.group(1)):
            out.append(m.group(1))
    return out


def list_products(client) -> list[tuple[str, str]]:
    urls = dict.fromkeys(product_urls(client.get(SITEMAP)))
    return [(u, parse_url(u)["product_id"]) for u in urls]


def _ld(tree) -> tuple[dict, list[str]]:
    product, crumbs = {}, []
    for s in tree.css('script[type="application/ld+json"]'):
        try:
            d = json.loads(s.text(), strict=False)
        except ValueError:
            continue
        if not isinstance(d, dict):
            continue
        if d.get("@type") == "Product":
            product = d
        elif d.get("@type") == "BreadcrumbList":
            crumbs = [i.get("name", "") for i in d.get("itemListElement", [])]
    return product, crumbs


def parse_product(url: str, html: str) -> Product:
    tree = HTMLParser(html)
    info = parse_url(url) or {"product_id": url.rstrip("/").rsplit("/", 1)[-1], "slug": ""}
    data, crumbs = _ld(tree)
    specs = {}
    for row in tree.css(".gcw-spec__row"):
        label = row.css_first(".gcw-spec__label span")
        values = [c for c in row.iter() if c.tag == "div"]
        if label and values:
            key = SPECS.get(label.text().strip().lower())
            value = re.sub(r"\s+", " ", values[-1].text()).strip()
            if key and value:
                specs[key] = value.replace(" · ", ", ")
    if specs.get("drink_window"):
        m = re.match(r"(\d{4})\s*[–-]\s*(\d{4})", specs["drink_window"])
        specs["drink_window"] = f"{m.group(1)}-{m.group(2)}" if m else specs["drink_window"]
    if not specs.get("producer") and isinstance(data.get("brand"), dict):
        specs["producer"] = data["brand"].get("name")
    title = (data.get("name") or (tree.css_first("h1").text() if tree.css_first("h1") else info["slug"])).strip()
    m = re.match(r"((?:19|20)\d{2})\s+(.*)", title)
    if m:
        specs.setdefault("vintage", m.group(1))
        title = m.group(2)
    if specs.get("vintage") and not re.fullmatch(r"(19|20)\d{2}", specs["vintage"]):
        specs.pop("vintage")
    colour_nl = (specs.pop("colour_nl", "") or (crumbs[2] if len(crumbs) > 2 else "")).lower()
    colour = COLOURS.get(colour_nl)
    if colour is None and re.search(r"mousserend|champagne|bubbels", colour_nl + " " + title.lower()):
        colour = "rosé" if re.search(r"ros[eé]", title.lower()) else "white"
    if colour is None and re.search(r"port|dessert|zoet|sherry|madeira", colour_nl):
        colour = "red" if re.search(r"ruby|tawny|lbv|vintage port", title.lower()) else "white"
    offer = data.get("offers") or {}
    if isinstance(offer, list):
        offer = offer[0] if offer else {}
    try:
        price = float(offer.get("price")) if offer.get("price") not in (None, "") else None
    except (TypeError, ValueError):
        price = None
    availability = str(offer.get("availability") or "")
    if re.search(r"en primeur|pre.?order", title, re.I):
        specs["presale"] = "yes"
    awards = []
    for r in data.get("review") or []:
        author = (r.get("author") or {}).get("name") if isinstance(r.get("author"), dict) else r.get("author")
        rating = r.get("reviewRating") or {}
        value, best = rating.get("ratingValue"), rating.get("bestRating") or "100"
        if author and value:
            awards.append((str(author), f"{value}/{best}" if str(best) == "20" else f"{value} punten"))
    category = crumbs[2] if len(crumbs) > 2 else None
    return Product(
        url=url, product_id=info["product_id"], title=title, colour=colour,
        volume_cl=_volume_cl((specs.pop("volume", "") or "").replace(",", ".")) or 75.0,
        price=price, promotion=None,
        in_stock_online=None if not availability else (availability.endswith("InStock") and "presale" not in specs),
        was_price=None, category=category, expert_awards=awards, specs=specs,
    )


def is_wine_product(p: Product) -> bool:
    if not (p.price and p.colour) or NOT_WINE.search(p.title) or NOT_WINE.search(p.category or ""):
        return False
    return p.is_wine
