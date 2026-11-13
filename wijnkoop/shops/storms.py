"""Wijnkoperij Alex Storms - Mertens (storms.nl): a Drupal Commerce shop with about 480 wines.

robots.txt sets no crawl-delay but forbids addresses with "?", so the category pages beyond the
first cannot be read. The sitemap has no product pages but does list every estate ("/wijnhuis/..."),
and each estate's page links to its wines: those pages plus the first "Wijnen" page give the list. Each product page lists its details as labelled fields
(Soort wijn, Jaar, Land, Wijngebied, Druivensoort, Alcohol, Inhoud, Wijnhuis), the price with
quantity prices (48+, 144+), and ratings as images ("Grote Hamersma 8,5", "Onno Kleyn 9-").
"""
import re

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from .gall import GallProduct as Product
from .vindict import _price, ten_to_hundred

KEY = "storms"
SHOP_NAME = "Alex Storms"
BASE = "https://storms.nl"
LISTING = BASE + "/categorie/wijnen"

URL_RE = re.compile(r"^/wijnen/(?P<slug>[a-z0-9-]+)")
COLOURS = {"rode wijn": "red", "witte wijn": "white", "rosé": "rosé", "rose": "rosé"}
FIELDS = {"jaar": "vintage", "land": "country", "streek": "region", "druivensoort": "grapes",
          "alcohol": "alcohol", "inhoud": "volume", "wijnhuis": "producer", "bescherming": "classification",
          "smaakprofiel": "style", "afsluiting": "closure", "houdbaarheid": "drink_until"}
TEN_POINT_CRITICS = re.compile(r"hamersma|onno\s+kleyn", re.I)
MEDAL_IN_FILE = re.compile(r"goud|gold|zilver|silver|brons|bronze|platin", re.I)
NOT_WINE = re.compile(r"pakket|proef|cadeau|geschenk|\bglas|karaf|whisk|\bgin\b|\brum\b|vodka|cognac|armagnac|"
                      r"calvados|likeur|grappa|brandy|alcoholvrij|0[.,]0\b", re.I)


def product_slugs(html: str) -> list[str]:
    return list(dict.fromkeys(m.group("slug") for h in re.findall(r'href="([^"]+)"', html)
                              if (m := URL_RE.match(h))))


def estate_pages(xml: str) -> list[str]:
    return list(dict.fromkeys(re.findall(r"<loc>\s*(https://storms\.nl/wijnhuis/[a-z0-9-]+)\s*<", xml)))


def list_products(client) -> list[tuple[str, str]]:
    slugs = product_slugs(client.get(LISTING))
    for page in estate_pages(client.get(BASE + "/sitemap.xml")):
        slugs += product_slugs(client.get(page))
    return [(f"{BASE}/wijnen/{s}", s) for s in dict.fromkeys(slugs)]


def _fields(article) -> dict[str, str]:
    out = {}
    for label in article.css(".field-label"):
        name = label.text().strip().lower()
        # a value's first line: the producer's field also holds a long story about the estate
        values = [i.text().strip().split("\n")[0].strip() for i in label.parent.css(".field-item")]
        if name and values and name not in out:
            out[name] = " | ".join(dict.fromkeys(v for v in values if v))
    return out


def _awards(article) -> list[tuple[str, str]]:
    out = []
    for img in article.css(".field-field_award_afbeelding img"):
        alt = re.sub(r"\s+", " ", img.attributes.get("alt") or "").strip()
        if not alt:
            continue
        m = re.match(r"(.*?)\s+(\d{1,2}(?:[.,]\d)?\s*[+-]?)$", alt)
        if TEN_POINT_CRITICS.search(alt) and m:
            pts = ten_to_hundred(m.group(2))
            award = (m.group(1), f"{pts:g} punten") if pts else None
        else:
            medal = MEDAL_IN_FILE.search(img.attributes.get("src") or "")
            award = (alt, medal.group(0) if medal and not MEDAL_IN_FILE.search(alt) else "")
        if award and award not in out:
            out.append(award)
    return out


def _colour(kind: str, title: str) -> str | None:
    k = kind.lower()
    for word, colour in COLOURS.items():
        if word in k:
            return colour
    if not re.search(r"mousser|dessert|port|sherry|zoet", k + " " + title.lower()):
        return None
    t = title.lower()
    if re.search(r"ros[eé]\b", t):
        return "rosé"
    if re.search(r"\b(rood|rouge|rosso|tinto|ruby|tawny|lbv|vintage|port)\b", t) and "white" not in t:
        return "red"
    return "white"


def parse_product(url: str, html: str) -> Product:
    tree = HTMLParser(html)
    article = tree.css_first("article.product") or tree.body
    fields = _fields(article)
    specs = {key: fields[name] for name, key in FIELDS.items() if fields.get(name)}
    if specs.get("region") is None and fields.get("wijngebied"):
        specs["region"] = fields["wijngebied"].split(" | ")[-1]
    specs = {k: v.replace(" | ", ", ") for k, v in specs.items()}
    if not re.fullmatch(r"(19|20)\d{2}", specs.get("vintage", "")):
        specs.pop("vintage", None)
    volume = None
    m = re.match(r"(\d+(?:[.,]\d+)?)", specs.pop("volume", ""))
    if m:  # in litres: "0,750"
        volume = round(float(m.group(1).replace(",", ".")) * 100, 1) or None
    if fields.get("list price"):
        specs["list_price"] = fields["list price"]
    box = article.css_first(".field-price")
    price = _price(box.css_first(".field-item").text()) if box and box.css_first(".field-item") else None
    tiers = [re.sub(r"\s+", " ", t.text()).strip() for t in box.css(".staffel > div")] if box else []
    if tiers:
        specs["quantity_prices"] = ", ".join(tiers)
    stock = fields.get("stock", "").lower()
    h1 = tree.css_first("h1")
    title = re.sub(r"\s+", " ", h1.text()).strip() if h1 else url.rsplit("/", 1)[-1]
    kind = fields.get("soort wijn", "")
    return Product(
        url=url, product_id=url.rstrip("/").rsplit("/", 1)[-1], title=title, colour=_colour(kind, title),
        volume_cl=volume or 75.0, price=price,
        in_stock_online=None if not stock else not re.search(r"uitverkocht|niet (meer )?(leverbaar|op voorraad)",
                                                             stock),
        category=kind or None, expert_awards=_awards(article), specs=specs,
    )


def is_wine_product(p: Product) -> bool:
    if not (p.price and p.colour) or NOT_WINE.search(p.title) or NOT_WINE.search(p.category or ""):
        return False
    return p.is_wine
