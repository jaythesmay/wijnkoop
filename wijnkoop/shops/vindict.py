"""Vindict (vindict.nl): product pages from the sitemap.

robots.txt asks for 10 seconds between pages. Every product page carries schema.org data
(name, price, availability, colour) and a table with the details and the press ratings
("Onderscheidingen": a year, which is the vintage that was rated, and the rating).
"""
import json
import re

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from .gall import GallProduct as Product

KEY = "vindict"
SHOP_NAME = "Vindict"
BASE = "https://www.vindict.nl"
PACE = 10  # crawl-delay in its robots.txt (November 2026); a longer one read at the start of a run wins
PACE_WHY = "the shop's robots.txt asks for this"
SITEMAP = BASE + "/sitemap.xml"

URL_RE = re.compile(r"^https://www\.vindict\.nl/product/(?P<pid>\d+)/(?P<slug>[^/?#]+)$")
COLOURS = {"rood": "red", "wit": "white", "rosé": "rosé", "rose": "rosé"}
SPECS = {
    "dranksoort": "category", "kleur": "colour_nl", "smaak": "style", "land": "country",
    "herkomstgebied": "region", "producent": "producer", "oogstjaar": "vintage",
    "flesinhoud": "volume", "alcohol": "alcohol", "druivenrassen": "grapes",
}
# "Dranksoort" is often the wine type itself (Wijn, Banyuls, Pedro Ximénez); these are not wine.
NOT_WINE = re.compile(r"likeur|limoncello|gin|whisk|rum|vodka|grappa|cognac|armagnac|calvados|brandy|bier|"
                      r"cider|sap|frisdrank|alcoholvrij|proeverij|cursus|glas|karaf|cadeau|bon\b|boek|accessoire",
                      re.I)


def parse_url(url: str) -> dict | None:
    m = URL_RE.match(url.strip())
    return {"product_id": m.group("pid"), "slug": m.group("slug")} if m else None


def product_urls_from_sitemap(xml: str) -> list[str]:
    locs = re.findall(r"<loc>\s*(?:<!\[CDATA\[)?\s*([^<\]\s]+)", xml)
    return [u for u in locs if parse_url(u)]


def list_products(client) -> list[tuple[str, str]]:
    """[(url, product_id)] for every product page in the sitemap (wine or not: the page tells)."""
    return [(u, parse_url(u)["product_id"]) for u in product_urls_from_sitemap(client.get(SITEMAP))]


def _price(text: str | None) -> float | None:
    if not text:
        return None
    m = re.search(r"\d+(?:\.\d{3})*,\d{2}|\d+\.\d{2}", text)
    if not m:
        return None
    v = m.group(0)
    return float(v.replace(".", "").replace(",", ".")) if "," in v else float(v)


def _volume_cl(text: str | None) -> float | None:
    if not text:
        return None
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(ml|cl|l)\b", text.lower())
    if not m:
        return None
    v = float(m.group(1).replace(",", "."))
    return {"ml": v / 10, "cl": v, "l": v * 100}[m.group(2)]


def ten_to_hundred(text: str) -> float | None:
    """De Grote Hamersma rates out of 10, with + and -: 8 = 88, 9 = 91.5, 10 = 95."""
    m = re.match(r"\s*(\d{1,2}(?:[.,]\d)?)\s*([+-])?", text)
    if not m:
        return None
    v = float(m.group(1).replace(",", ".")) + {"+": 0.25, "-": -0.25, None: 0}[m.group(2)]
    return round(60 + 3.5 * v, 1) if 5 <= v <= 10 else None


def _awards(rows) -> list[tuple]:
    """[(source, result, vintage)] from the "Jaar | Onderscheiding" rows."""
    out = []
    for year, text in rows:
        vintage = int(year) if re.fullmatch(r"(19|20)\d{2}", year or "") else None
        parts = [p.strip() for p in text.split("|")]
        source, rest = parts[0], " ".join(parts[1:])
        if "hamersma" in source.lower() and len(parts) > 1:
            pts = ten_to_hundred(parts[1])
            if pts:
                out.append((source, f"{pts:g} punten", vintage))
            continue
        # e.g. "Perswijn - september 2024: 15.5/20 Goed tot zeer goed"
        if ":" in source:
            source, first = source.split(":", 1)
            rest = f"{first} {rest}".strip()
        out.append((source.strip(), rest, vintage))
    return out


def parse_product(url: str, html: str) -> Product:
    tree = HTMLParser(html)
    info = parse_url(url) or {"product_id": "", "slug": ""}
    data = {}
    for s in tree.css('script[type="application/ld+json"]'):
        try:
            d = json.loads(s.text())
        except ValueError:
            continue
        main = d.get("mainEntity") if isinstance(d, dict) else None
        if isinstance(main, dict) and main.get("@type") == "Product":
            data = main
            break
    specs, award_rows = {}, []
    for tr in tree.css("table tr"):
        cells = [c for c in (re.sub(r"\s+", " ", td.text()).strip() for td in tr.css("td, th")) if c]
        if len(cells) < 2:
            continue
        label = cells[0].lower()
        if label in SPECS:
            specs[SPECS[label]] = cells[1]
        elif re.fullmatch(r"(19|20)\d{2}", cells[0]):
            award_rows.append((cells[0], " | ".join(cells[1:])))
    offer = data.get("offers") or {}
    if isinstance(offer, list):
        offer = offer[0] if offer else {}
    price = _price(str(offer.get("price"))) if offer.get("price") else None
    was = None
    box = tree.css_first(".price-container .price")
    if box:
        struck = box.css_first("s")
        if struck:
            was = _price(struck.text())
        if price is None:
            price = _price(re.sub(r"<s>.*?</s>", "", box.html or ""))
    if was is not None and price is not None and was <= price:
        was = None
    availability = str(offer.get("availability") or "")
    colour_nl = (specs.pop("colour_nl", None) or data.get("color") or "").lower()
    title = data.get("name") or (tree.css_first("h1").text().strip() if tree.css_first("h1") else info["slug"])
    vintage = specs.get("vintage")
    if vintage and not re.fullmatch(r"(19|20)\d{2}", vintage):
        specs.pop("vintage")
    p = Product(
        url=url, product_id=info["product_id"], title=title.strip(),
        colour=COLOURS.get(colour_nl), volume_cl=_volume_cl(specs.pop("volume", None)) or 75.0,
        price=price, promotion=None,
        in_stock_online=None if not availability else availability.endswith("InStock"),
        was_price=was, category=specs.get("category"),
        expert_awards=_awards(award_rows), specs=specs,
    )
    return p


def is_wine_product(p: Product) -> bool:
    """Wine, sparkling, port or dessert wine with a price and a colour; not spirits, glasses or tastings."""
    if not (p.price and p.colour) or NOT_WINE.search(p.category or "") or NOT_WINE.search(p.title):
        return False
    return p.is_wine
