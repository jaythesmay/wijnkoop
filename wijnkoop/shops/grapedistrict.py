"""Grape District (grapedistrict.nl): product pages from the (gzipped) sitemap.

robots.txt sets no crawl-delay, so the polite default applies. Each product page carries a
data block with the price, sale flag, stock, vintage, country and region, a details table
(producer) and the critics' ratings ("9/10 punten - Harold Hamersma - De Grote Hamersma 2019").
"""
import gzip
import html as htmllib
import json
import re

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from .gall import GallProduct as Product
from .vindict import ten_to_hundred
from .wijnvoordeel import colour_for

KEY = "grapedistrict"
SHOP_NAME = "Grape District"
BASE = "https://www.grapedistrict.nl"
SITEMAP = BASE + "/sitemap.xml"

URL_RE = re.compile(r"^https://www\.grapedistrict\.nl/(?P<slug>[a-z0-9][a-z0-9-]*)$")
NOT_WINE = re.compile(r"pakket|proef|cadeau|bon\b|box\b|glas|karaf|abonnement|cursus|alcoholvrij|0[.,]0|"
                      r"gin\b|whisk|likeur|bier", re.I)


def parse_url(url: str) -> dict | None:
    m = URL_RE.match(url.strip())
    return {"product_id": m.group("slug"), "slug": m.group("slug")} if m else None


def product_urls(xml: str) -> list[str]:
    return [u for u in re.findall(r"<loc>\s*([^<\s]+)", xml) if parse_url(u)]


def list_products(client) -> list[tuple[str, str]]:
    urls = []
    for sub in re.findall(r"<loc>\s*([^<\s]+)", client.get(SITEMAP)):
        raw = client.get_bytes(sub)
        xml = gzip.decompress(raw).decode("utf-8") if raw[:2] == b"\x1f\x8b" else raw.decode("utf-8")
        urls += product_urls(xml)
    return [(u, parse_url(u)["product_id"]) for u in dict.fromkeys(urls)]


def _colour(category: str, name: str) -> str | None:
    c = f"{category} {name}".lower()
    if re.search(r"ros[eé]", c):
        return "rosé"
    if re.search(r"\brode?\b|rood", c):
        return "red"
    if re.search(r"\bwitte?\b|mousserend|bubbel|champagne|cava|prosecco|cr[eé]mant", c):
        return "white"
    return None


def rating(text: str) -> tuple | None:
    """("De Grote Hamersma", "90.6 punten", None) from "9/10 punten - Harold Hamersma - De Grote Hamersma 2019"."""
    m = re.match(r"\s*(\d{1,3}(?:[.,]\d)?)\s*([+-])?\s*/\s*(10|20|100)\b", text)
    if not m:
        return None
    parts = [p.strip() for p in text.split(" - ")]
    source = parts[-1] if len(parts) > 1 else "Other"
    value, scale = m.group(1).replace(",", "."), m.group(3)
    if scale == "10":
        pts = ten_to_hundred(value + (m.group(2) or ""))
        return (source, f"{pts:g} punten") if pts else None
    if scale == "20":
        return (source, f"{value}/20")
    return (source, f"{value} punten")


def parse_product(url: str, html: str) -> Product:
    tree = HTMLParser(html)
    info = parse_url(url) or {"product_id": url.rstrip("/").rsplit("/", 1)[-1], "slug": ""}
    item = {}
    node = tree.css_first("[data-exponea-view-item]")
    if node:
        try:
            item = json.loads(htmllib.unescape(node.attributes.get("data-exponea-view-item") or "{}"))
        except ValueError:
            item = {}
    specs = {}
    table = {}
    for tr in tree.css("table tr"):
        cells = [re.sub(r"\s+", " ", c.text()).strip() for c in tr.css("th, td")]
        if len(cells) >= 2 and cells[0]:
            table[cells[0].lower()] = cells[1]
    if table.get("wijnhuis"):
        specs["producer"] = table["wijnhuis"]
    if item.get("jaar") and re.fullmatch(r"(19|20)\d{2}", str(item["jaar"])):
        specs["vintage"] = str(item["jaar"])
    for key, field in (("country", "land"), ("region", "streek"), ("grapes", "druif"), ("food", "lekker_bij")):
        if item.get(field):
            specs[key] = item[field]
    name = item.get("name") or (tree.css_first("h1").text().strip() if tree.css_first("h1") else info["slug"])
    producer = specs.get("producer")
    # Prefix the producer unless the name already starts with (part of) it.
    squash = name.lower().replace(" ", "")
    first = (producer or "").split()[0].lower() if producer else ""
    known = producer and (producer.lower().replace(" ", "") in squash or squash.startswith(first))
    title = f"{producer} {name}" if producer and not known else name
    price = float(item["price"]) if item.get("price") is not None else None
    meta = tree.css_first('meta[itemprop="price"]')
    if price is None and meta:
        price = float(meta.attributes.get("content"))
    was = None
    if item.get("special_price"):
        old = tree.css_first(".list-price-price, .product-detail-list-price, del, s")
        if old:
            m = re.search(r"(\d+)[.,](\d{2})", old.text())
            was = float(f"{m.group(1)}.{m.group(2)}") if m else None
        if was is not None and price is not None and was <= price:
            was = None
    awards = []
    for t in tree.css(".product-detail-ratings-title"):
        r = rating(re.sub(r"\s+", " ", t.text()).strip())
        if r:
            awards.append(r)
    category = item.get("category_name") or ""
    in_stock = item.get("is_in_stock") if "is_in_stock" in item else None
    return Product(
        url=url, product_id=info["product_id"], title=re.sub(r"\s+", " ", title).strip(),
        colour=_colour(category, name) or colour_for(name, specs.get("grapes")),
        volume_cl=75.0 if (item.get("type") or "Fles") == "Fles" else None,
        price=price, promotion=None, in_stock_online=in_stock, was_price=was,
        category=category or None, expert_awards=awards, specs=specs,
        shop_rating=item.get("product_review_average"), shop_rating_count=item.get("rating_count"),
    )


def is_wine_product(p: Product) -> bool:
    if not (p.price and p.colour and p.volume_cl) or NOT_WINE.search(p.title) or NOT_WINE.search(p.category or ""):
        return False
    return p.is_wine
