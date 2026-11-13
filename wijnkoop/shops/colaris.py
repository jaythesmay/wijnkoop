"""Colaris (colaris.nl): a Magento shop with about 1,400 products.

robots.txt asks for 20 seconds between pages, so a full run takes about eight hours. Every product
page carries schema.org data with the price and a list of properties (colour, vintage, country,
region, grapes, alcohol, bottle size, producer, drinking window) and, for many wines, critics'
scores ("Robert Parkers Wine Advocate 91-93/100"). Mystery boxes and other packages are left out.
"""
import html as htmllib
import json
import re

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from ..reference import awards_in_text
from .gall import GallProduct as Product

KEY = "colaris"
SHOP_NAME = "Colaris"
BASE = "https://www.colaris.nl"
SITEMAP = BASE + "/media/siteindex/colaris_nl/siteindex.xml"

COLOURS = {"rood": "red", "rode wijn": "red", "wit": "white", "witte wijn": "white", "rosé": "rosé",
           "rose": "rosé", "rosé wijn": "rosé"}
PROPS = {"Jaargang": "vintage", "Land": "country", "Regio": "region", "Druivenras": "grapes",
         "Alcoholgehalte": "alcohol", "manufacturer": "producer", "Smaakprofiel": "style",
         "Afsluiting": "closure"}
NOT_WINE = re.compile(r"mystery|pakket|proefdoos|cadeau|geschenk|\bglas|karaf|whisk|\bgin\b|\brum\b|vodka|"
                      r"cognac|likeur|grappa|alcoholvrij|0[.,]0\b", re.I)


def product_urls(xml: str) -> list[str]:
    return [u for u in re.findall(r"<loc>\s*([^<\s]+)", xml)
            if re.match(r"^https://www\.colaris\.nl/[^/?#]+$", u)]


def list_products(client) -> list[tuple[str, str]]:
    index = client.get(SITEMAP)
    urls = []
    for sub in re.findall(r"<loc>\s*([^<\s]*/products\.xml)", index):
        urls += product_urls(client.get(sub))
    return [(u, u.rsplit("/", 1)[-1]) for u in dict.fromkeys(urls)]


def _product_data(tree) -> dict:
    for s in tree.css('script[type="application/ld+json"]'):
        try:
            d = json.loads(s.text())
        except ValueError:
            continue
        if isinstance(d, dict) and str(d.get("@type", "")).endswith("Product"):
            return d
    return {}


def _colour(kinds: list[str], title: str) -> str | None:
    for k in kinds:
        if k.strip().lower() in COLOURS:
            return COLOURS[k.strip().lower()]
    if any(re.search(r"mousser|champagne|dessert|zoet|port|sherry", k, re.I) for k in kinds):
        t = title.lower()
        if re.search(r"ros[eé]\b", t):
            return "rosé"
        if re.search(r"\b(rood|rouge|rosso|tinto|ruby|tawny|lbv|vintage port)\b", t):
            return "red"
        return "white"
    return None


def parse_product(url: str, html: str) -> Product:
    tree = HTMLParser(html)
    data = _product_data(tree)
    props = {}
    kinds = []
    for p in data.get("additionalProperty") or []:
        name, value = p.get("name"), htmllib.unescape(str(p.get("value") or "")).strip()
        if name == "Wijnsoort":
            kinds.append(value)
        props.setdefault(name, value)
    offers = data.get("offers") or {}
    spec = offers.get("priceSpecification") or {}
    price = spec.get("price", offers.get("price"))
    price = float(price) if price not in (None, "", 0, "0") else None
    old = tree.css_first(".old-price [data-price-amount]")
    was = float(old.attributes["data-price-amount"]) if old and old.attributes.get("data-price-amount") else None
    title = re.sub(r"\s+", " ", htmllib.unescape(data.get("name") or "")).strip()
    specs = {key: props[name] for name, key in PROPS.items() if props.get(name)}
    if not re.fullmatch(r"(19|20)\d{2}", specs.get("vintage", "")):
        specs.pop("vintage", None)
        m = re.search(r"\b((?:19|20)\d{2})\b", title)
        if m:
            specs["vintage"] = m.group(1)
    if specs.get("vintage"):  # the same wine in every year
        title = re.sub(r"\s{2,}", " ", re.sub(rf"\b{specs['vintage']}\b", "", title)).strip()
    if props.get("OpDronkVanaf") and props.get("OpDronkTot"):
        specs["drink_window"] = f"{props['OpDronkVanaf']}-{props['OpDronkTot']}"
    if props.get("package") == "true":
        specs["package"] = "yes"
    try:
        volume = round(float(props.get("Flesinhoud")) * 100, 1) or None
    except (TypeError, ValueError):
        volume = None
    awards = awards_in_text(props.get("Awards")) or awards_in_text(props.get("ShortDescription"))
    availability = str(offers.get("availability") or "")
    return Product(
        url=url, product_id=url.rstrip("/").rsplit("/", 1)[-1], title=title, colour=_colour(kinds, title),
        volume_cl=volume or 75.0, price=price, was_price=was if was and price and was > price else None,
        in_stock_online=None if not availability else availability.endswith("InStock"),
        category=kinds[-1] if kinds else None, expert_awards=awards, specs=specs,
    )


def is_wine_product(p: Product) -> bool:
    if not (p.price and p.colour) or p.specs.get("package") or NOT_WINE.search(p.title):
        return False
    return p.is_wine
