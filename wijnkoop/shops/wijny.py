"""Wijny (wijny.nl), the webshop of Bon Import in Almere. bonimport.nl itself asks all robots to stay
away; wijny.nl allows them and sets no crawl-delay.

The shop sitemap lists about 4,000 products, wine and also spirits, beer and soft drinks. Every
product page carries schema.org data with the price and properties (colour, vintage, country,
region, grapes, alcohol, bottle size); only wines have a colour. The pages show no awards.
"""
import html as htmllib
import json
import re

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from .gall import GallProduct as Product
from .vindict import _volume_cl

KEY = "wijny"
SHOP_NAME = "Wijny"
BASE = "https://www.wijny.nl"
SITEMAP = BASE + "/sitemap/shop.xml"

COLOURS = {"rood": "red", "wit": "white", "rosé": "rosé", "rose": "rosé"}
PROPS = {"Land": "country", "Regio": "region", "Druif": "grapes", "Alcoholpercentage": "alcohol",
         "Jaartal": "vintage", "Inhoud": "volume", "Afsluiting": "closure"}
NOT_WINE = re.compile(r"pakket|proefdoos|cadeau|geschenk|\bglas|glasses|karaf|whisk|\bgin\b|\brum\b|vodka|"
                      r"cognac|likeur|liqueur|grappa|alcoholvrij|alcohol-free|0[.,]0\b|\bbier\b", re.I)


def product_urls(xml: str) -> list[str]:
    return [u for u in re.findall(r"<loc>\s*([^<\s]+)", xml)
            if re.match(r"^https://www\.wijny\.nl/product/[^/?#]+/?$", u)]


def list_products(client) -> list[tuple[str, str]]:
    urls = product_urls(client.get(SITEMAP))
    return [(u, u.rstrip("/").rsplit("/", 1)[-1]) for u in dict.fromkeys(urls)]


def _product_data(tree) -> dict:
    for s in tree.css('script[type="application/ld+json"]'):
        try:
            d = json.loads(s.text())
        except ValueError:
            continue
        for item in (d.get("@graph", [d]) if isinstance(d, dict) else d):
            if isinstance(item, dict) and item.get("@type") == "Product":
                return item
    return {}


def _colour(kind: str | None, title: str) -> str | None:
    if not kind:
        return None
    k = kind.strip().lower()
    if k in COLOURS:
        return COLOURS[k]
    t = title.lower()
    if re.search(r"ros[eé]\b", t):
        return "rosé"
    if re.search(r"\b(rood|rouge|rosso|tinto|ruby|tawny|lbv|vintage port)\b", t):
        return "red"
    return "white"  # sparkling, sweet: white unless the name says otherwise


def parse_product(url: str, html: str) -> Product:
    tree = HTMLParser(html)
    data = _product_data(tree)
    props: dict[str, list[str]] = {}
    for p in data.get("additionalProperty") or []:
        props.setdefault(p.get("name"), []).append(htmllib.unescape(str(p.get("value") or "")).strip())
    specs = {key: ", ".join(props[name]) for name, key in PROPS.items() if props.get(name)}
    if not re.fullmatch(r"(19|20)\d{2}", specs.get("vintage", "")):
        specs.pop("vintage", None)
    if specs.get("alcohol") and not specs["alcohol"].endswith("%"):
        specs["alcohol"] += "%"
    brand = data.get("brand")
    if isinstance(brand, dict) and brand.get("name"):
        specs["producer"] = brand["name"]
    offers = data.get("offers") or {}
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    price = float(offers["price"]) if offers.get("price") not in (None, "", "0", 0) else None
    title = re.sub(r"\s+", " ", htmllib.unescape(data.get("name") or "")).strip()
    kind = (props.get("Kleur") or [None])[0]
    availability = str(offers.get("availability") or "")
    return Product(
        url=url, product_id=url.rstrip("/").rsplit("/", 1)[-1], title=title, colour=_colour(kind, title),
        volume_cl=_volume_cl(specs.pop("volume", None)) or 75.0, price=price,
        in_stock_online=None if not availability else availability.endswith("InStock"),
        category=data.get("category"), specs=specs,
    )


def is_wine_product(p: Product) -> bool:
    if not (p.price and p.colour) or NOT_WINE.search(p.title):
        return False
    return p.is_wine
