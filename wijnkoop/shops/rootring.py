"""Rootring Wijnen (rootring-wijnen.nl), a wine shop in Alkmaar with about 4,000 wines.

robots.txt allows everything and sets no crawl-delay. The sitemap is out of date (its addresses still
name old vintages and answer 404), so the wines come from the four colour pages (?pagina=1, 2, ...).
Each product page carries schema.org data with the price, a details table (Wijnhuis, Streek, Gebied,
Druifsoort, Wijnsoort, Jaargang, Inhoud, Land) and the shop's quantity discount ("10% korting
vanaf 6+").
"""
import json
import re

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from ..reference import awards_in_text
from .gall import GallProduct as Product
from .vindict import _volume_cl

KEY = "rootring"
SHOP_NAME = "Rootring Wijnen"
BASE = "https://rootring-wijnen.nl"
CATEGORIES = ["rode-wijn", "witte-wijn", "rose-wijn", "mousserende-wijn"]

DETAILS = {"wijnhuis": "producer", "streek": "region", "gebied": "appellation", "druifsoort": "grapes",
           "jaargang": "vintage", "inhoud": "volume", "land": "country", "alcohol": "alcohol",
           "alcoholpercentage": "alcohol"}
COLOURS = {"rode wijn": "red", "witte wijn": "white", "rosé wijn": "rosé", "rose wijn": "rosé"}
NOT_WINE = re.compile(r"pakket|proef|cadeau|geschenk|\bglas|karaf|whisk|\bgin\b|\brum\b|vodka|cognac|"
                      r"likeur|grappa|alcoholvrij|0[.,]0\b|olijfolie|\bkaas\b", re.I)


def product_slugs(html: str) -> list[str]:
    tree = HTMLParser(html)
    return list(dict.fromkeys(a.attributes["href"].strip("/") for a in tree.css("a.product-card[href]")
                              if re.fullmatch(r"/[a-z0-9-]+", a.attributes["href"])))


def list_products(client) -> list[tuple[str, str]]:
    slugs = []
    for category in CATEGORIES:
        first = client.get(f"{BASE}/{category}")
        last = max((int(n) for n in re.findall(r"\?pagina=(\d+)", first)), default=1)
        slugs += product_slugs(first)
        for page in range(2, last + 1):
            slugs += product_slugs(client.get(f"{BASE}/{category}?pagina={page}"))
    return [(f"{BASE}/{s}", s) for s in dict.fromkeys(slugs)]


def _colour(kind: str | None, title: str) -> str | None:
    if not kind:
        return None
    k = kind.strip().lower()
    if k in COLOURS:
        return COLOURS[k]
    t = title.lower()
    if re.search(r"ros[eé]\b", t):
        return "rosé"
    return "white"  # sparkling


def parse_product(url: str, html: str) -> Product:
    tree = HTMLParser(html)
    data = {}
    for s in tree.css('script[type="application/ld+json"]'):
        try:
            d = json.loads(s.text())
        except ValueError:
            continue
        if isinstance(d, dict) and d.get("@type") == "Product":
            data = d
            break
    details = {}
    for tr in tree.css(".product__specs tr"):
        cells = tr.css("td")
        if len(cells) >= 2:
            details.setdefault(cells[0].text().strip().lower(), re.sub(r"\s+", " ", cells[1].text()).strip())
    specs = {key: details[name] for name, key in DETAILS.items() if details.get(name)}
    if not re.fullmatch(r"(19|20)\d{2}", specs.get("vintage", "")):
        specs.pop("vintage", None)
    offers = data.get("offers") or {}
    price = float(offers["price"]) if offers.get("price") not in (None, "", 0, "0") else None
    title = re.sub(r"\s+", " ", data.get("name") or "").strip()
    if specs.get("vintage"):  # the same wine in every year
        title = re.sub(rf"\s+{specs['vintage']}$", "", title)
    promotion = None
    discount = tree.css_first(".product__discount")
    m = re.search(r"(\d+)\s*%\s*korting\s+vanaf\s+(\d+)", discount.text() if discount else "")
    if m and int(m.group(2)) <= 6:
        promotion = f"{m.group(1)}% korting vanaf {m.group(2)} flessen"
    score = tree.css_first(".product__score")
    awards = awards_in_text(" ".join([score.text(separator=" ")] + [i.attributes.get("alt") or ""
                                                                    for i in score.css("img")])) if score else []
    availability = str(offers.get("availability") or "")
    kind = details.get("wijnsoort")
    return Product(
        url=url, product_id=url.rstrip("/").rsplit("/", 1)[-1], title=title, colour=_colour(kind, title),
        volume_cl=_volume_cl(specs.pop("volume", None)) or 75.0, price=price, promotion=promotion,
        in_stock_online=None if not availability else availability.endswith("InStock"),
        category=kind, expert_awards=awards, specs=specs,
    )


def is_wine_product(p: Product) -> bool:
    if not (p.price and p.colour) or NOT_WINE.search(p.title):
        return False
    return p.is_wine
