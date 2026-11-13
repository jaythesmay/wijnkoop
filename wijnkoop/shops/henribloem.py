"""Henri Bloem (henribloem.nl): WooCommerce product pages from the sitemap.

robots.txt sets no crawl-delay, so the polite default applies. Every product page carries
schema.org data (name, price, availability), the shop's categories (colour), an "Eigenschappen"
table (vintage, country, producer, grapes) and a quantity discount table (6+ and 12+ bottles).
The pages show no awards.
"""
import json
import re

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from .gall import GallProduct as Product
from .vindict import _price, _volume_cl

KEY = "henribloem"
SHOP_NAME = "Henri Bloem"
BASE = "https://henribloem.nl"
SITEMAP = BASE + "/sitemaps.xml"

URL_RE = re.compile(r"^https://(?:www\.)?henribloem\.nl/product/(?P<slug>[^/?#]+)/?$")
SPECS = {
    "jaartal": "vintage", "land": "country", "regio": "region", "subregio": "subregion",
    "producent": "producer", "druivensoorten": "grapes", "alcoholpercentage": "alcohol",
    "karakter": "style", "lekker bij": "food", "inhoud": "volume",
}
# Category slug -> colour; sparkling, sweet and fortified wines get theirs from the name.
CATEGORY_COLOUR = {"rode-wijn": "red", "witte-wijn": "white", "rose": "rosé"}
WINE_CATEGORIES = set(CATEGORY_COLOUR) | {"mousserend", "zoete-wijnen", "versterkte-wijnen"}
NOT_WINE = re.compile(r"pakket|proef|cadeau|bon\b|geschenk|0[.,]0|alcoholvrij|non-alc|glas|karaf|gin\b|"
                      r"whisk|rum\b|vodka|likeur|bier", re.I)


def parse_url(url: str) -> dict | None:
    m = URL_RE.match(url.strip())
    return {"product_id": m.group("slug"), "slug": m.group("slug")} if m else None


def product_urls(xml: str) -> list[str]:
    return [u for u in re.findall(r"<loc>\s*([^<\s]+)", xml) if parse_url(u)]


def list_products(client) -> list[tuple[str, str]]:
    index = client.get(SITEMAP)
    urls = []
    for sub in re.findall(r"<loc>\s*([^<\s]*product-sitemap\d*\.xml)", index):
        urls += product_urls(client.get(sub))
    return [(u, parse_url(u)["product_id"]) for u in dict.fromkeys(urls)]


def _colour(categories: list[str], title: str) -> str | None:
    for c in categories:
        if c in CATEGORY_COLOUR:
            return CATEGORY_COLOUR[c]
    if not WINE_CATEGORIES & set(categories):
        return None
    t = title.lower()
    if re.search(r"ros[eé]\b", t):
        return "rosé"
    if re.search(r"\b(rood|rouge|rosso|tinto|ruby|tawny|lbv|vintage port|banyuls|maury|recioto della valpolicella)\b", t):
        return "red"
    return "white"


def _offer(data: dict) -> dict:
    offers = data.get("offers") or {}
    return (offers[0] if offers else {}) if isinstance(offers, list) else offers


def parse_product(url: str, html: str) -> Product:
    tree = HTMLParser(html)
    info = parse_url(url) or {"product_id": url.rstrip("/").rsplit("/", 1)[-1], "slug": ""}
    data = {}
    for s in tree.css('script[type="application/ld+json"]'):
        try:
            d = json.loads(s.text())
        except ValueError:
            continue
        for item in (d.get("@graph", [d]) if isinstance(d, dict) else d):
            if isinstance(item, dict) and item.get("@type") == "Product":
                data = item
                break
        if data:
            break
    offer = _offer(data)
    price = _price(str(offer.get("price"))) if offer.get("price") else None
    specs = {}
    for tr in tree.css("table.wc-attr-table tr, table.porto-cp-attr-table tr"):
        name, term = tr.css_first(".porto-attr-name, th"), tr.css_first(".porto-attr-term, td")
        if name and term:
            label = name.text().strip().lower()
            if label in SPECS:
                specs[SPECS[label]] = re.sub(r"\s+", " ", term.text()).strip()
    meta = tree.css_first(".product_meta")
    categories = []
    if meta:
        categories = [m.group(1) for a in meta.css("a")
                      if (m := re.search(r"/product-category/([^/]+)/", a.attributes.get("href") or ""))]
        brand = next((a.text().strip() for a in meta.css("a") if "/brand/" in (a.attributes.get("href") or "")), None)
        if brand and not specs.get("producer"):
            specs["producer"] = brand
    if not specs.get("producer") and isinstance(data.get("brand"), dict):
        specs["producer"] = data["brand"].get("name")
    was, promotion = None, None
    table = tree.css_first("table.tiered-pricing-table")
    if table:
        regular = _price(table.attributes.get("data-regular-price"))
        sale = _price(table.attributes.get("data-sale-price"))
        if sale and regular and sale < regular:
            was, price = regular, sale
        try:
            rules = json.loads(table.attributes.get("data-price-rules") or "{}")
        except ValueError:
            rules = {}
        tiers = sorted((int(q), float(v)) for q, v in rules.items() if str(q).isdigit())
        six = [(q, v) for q, v in tiers if q <= 6]
        if six and price and six[-1][1] < price:
            q, v = six[-1]
            promotion = f"{q} flessen voor €{q * v:.2f}".replace(".", ",")  # quantity discount
        if tiers:
            specs["quantity_prices"] = ", ".join(f"{q}+: {v:.2f}" for q, v in tiers)
    title = (data.get("name") or (tree.css_first("h1").text() if tree.css_first("h1") else info["slug"])).strip()
    vintage = specs.get("vintage")
    if vintage and not re.fullmatch(r"(19|20)\d{2}", vintage):
        specs.pop("vintage")
    if not specs.get("vintage"):
        m = re.search(r"\b((?:19|20)\d{2})$", title)
        if m:
            specs["vintage"] = m.group(1)
    # The vintage is part of Henri Bloem's product name; the wine is the same in every year.
    if specs.get("vintage"):
        title = re.sub(rf"\s+{specs['vintage']}$", "", title)
    title = re.sub(r"\s+NM$", "", re.sub(r"\s{2,}", " ", title), flags=re.I)
    availability = str(offer.get("availability") or "")
    category = next((c for c in categories if c in WINE_CATEGORIES), categories[0] if categories else None)
    return Product(
        url=url, product_id=info["product_id"], title=title,
        colour=_colour(categories, title), volume_cl=_volume_cl(specs.pop("volume", None)) or 75.0,
        price=price, promotion=promotion,
        in_stock_online=None if not availability else availability.endswith("InStock"),
        was_price=was, category=category, specs=specs,
    )


def is_wine_product(p: Product) -> bool:
    if not (p.price and p.colour) or NOT_WINE.search(p.title) or NOT_WINE.search(p.category or ""):
        return False
    return p.is_wine
