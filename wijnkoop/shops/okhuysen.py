"""Okhuysen (okhuysen.nl): product pages from the sitemap.

robots.txt sets no crawl-delay, so the polite default applies. A product page shows the vintage
in its heading, the price per bottle (and the normal price when it is reduced), a details list
(colour, region, volume, alcohol, grapes) and the guide ratings ("Gidsbeoordeling", e.g.
"Jamessuckling.com : 94"). Its analytics data names the producer. Pre-sale wines ("Voorverkoop")
are delivered months later; they count as not in stock.
"""
import html as htmllib
import json
import re

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from .gall import GallProduct as Product
from .vindict import _volume_cl

KEY = "okhuysen"
SHOP_NAME = "Okhuysen"
BASE = "https://okhuysen.nl"
SITEMAP = BASE + "/sitemap.xml"

URL_RE = re.compile(r"^https://(?:www\.)?okhuysen\.nl/product/(?P<slug>[^/?#]+?-(?P<pid>\d+))/?$")
DETAILS = {
    "streek": "region", "herkomst": "appellation", "kleur / soort": "colour_nl", "inhoud": "volume",
    "alcohol %": "alcohol", "druivenras": "grapes", "wijn karakter": "style", "wijn-spijs advies": "food",
    "schenkadvies": "drink_window",
}
COLOURS = {"rode wijn": "red", "witte wijn": "white", "rosé": "rosé", "rose": "rosé", "rosé wijn": "rosé"}
NOT_WINE = re.compile(r"proeverij|proefpakket|cadeau|bon\b|wijnazijn|olijfolie|azijn|glas|karaf|boek|"
                      r"gin\b|whisk|rum\b|cognac|armagnac|calvados|grappa|likeur|alcoholvrij|0[.,]0", re.I)


def parse_url(url: str) -> dict | None:
    m = URL_RE.match(url.strip())
    return {"product_id": m.group("pid"), "slug": m.group("slug")} if m else None


def product_urls(xml: str) -> list[str]:
    return [u for u in re.findall(r"<loc>\s*([^<\s]+)", xml) if parse_url(u)]


def list_products(client) -> list[tuple[str, str]]:
    urls = dict.fromkeys(product_urls(client.get(SITEMAP)))
    return [(u, parse_url(u)["product_id"]) for u in urls]


def _num(text: str | None) -> float | None:
    m = re.search(r"\d+(?:\.\d+)?", (text or "").replace(",", "."))
    return float(m.group(0)) if m else None


def _analytics_item(html: str) -> dict:
    m = re.search(r'"items":\[(\{.*?\})\]', htmllib.unescape(html))
    if not m:
        return {}
    try:
        return json.loads(m.group(1))
    except ValueError:
        return {}


def _details(tree) -> dict:
    """{label: text} from the <p class="h4">label</p><p>text</p> pairs; repeated labels join."""
    out = {}
    for h in tree.css("p.h4"):
        label = re.sub(r"\s+", " ", h.text()).strip()
        values, node = [], h.next
        while node is not None and not (node.tag == "p" and "h4" in (node.attributes.get("class") or "").split()):
            if node.tag == "p":
                values.append(re.sub(r"\s+", " ", node.text()).strip())
            node = node.next
        if label and values and label not in out:
            out[label] = values
    return out


def _awards(lines: list[str]) -> list[tuple]:
    """[(critic, result)] from "Jamessuckling.com : 94" or "Robert Parker : 93-95" lines."""
    out = []
    for line in lines:
        if ":" not in line:
            continue
        source, value = (x.strip() for x in line.rsplit(":", 1))
        source = re.sub(r"\.(com|net|nl|co\.uk)$", "", source, flags=re.I)
        source = re.sub(r"(?i)^jamessuckling$", "James Suckling", source)
        m = re.match(r"(\d{1,3}(?:[.,]\d)?)(?:\s*[-–]\s*\d{1,3})?\s*(/\s*(?:20|100))?", value)
        if not m:
            continue
        result = f"{m.group(1)}{m.group(2).replace(' ', '')}" if m.group(2) else f"{m.group(1)} punten"
        out.append((source, result))
    return out


def parse_product(url: str, html: str) -> Product:
    tree = HTMLParser(html)
    info = parse_url(url) or {"product_id": url.rstrip("/").rsplit("-", 1)[-1], "slug": ""}
    item = _analytics_item(html)
    details = _details(tree)
    specs = {}
    for label, values in details.items():
        key = DETAILS.get(label.lower())
        if key:
            specs[key] = values[0]
    h1 = tree.css_first("h1")
    heading = re.sub(r"\s+", " ", h1.text()).strip() if h1 else ""
    m = re.match(r"((?:19|20)\d{2})\s+(.*)", heading)
    if m:
        specs["vintage"], name = m.group(1), m.group(2)
    else:
        name = heading
    name = item.get("item_name") or name
    producer = item.get("item_brand")
    if producer:
        specs["producer"] = producer
    title = f"{producer} {name}" if producer and producer.lower() not in name.lower() else name
    price = None
    euro, cent = tree.css_first(".product-detail__card__price--euro"), tree.css_first(".product-detail__card__price--cent")
    if euro:
        price = float(f"{_num(euro.text()):.0f}.{(cent.text().strip() if cent else '00')[:2]}")
    if price is None and item.get("price") is not None:
        price = float(item["price"])
    normal = tree.css_first(".price__normal")
    was = _num(normal.text()) if normal else None
    if was is not None and (price is None or was <= price):
        was = None
    presale = tree.css_first(".status-badge--presale") is not None
    can_order = "In winkelwagen" in html
    in_stock = False if presale else can_order
    if presale:
        specs["presale"] = "yes"
    for chooser in tree.css("[data-wine-link-chooser]"):
        title_node = chooser.css_first(".wine-link-chooser__title")
        if title_node and chooser.css_first('a[href*="/wijn?country"]'):
            specs["country"] = title_node.text().strip()
            break
    colour_nl = (specs.pop("colour_nl", "") or item.get("item_category") or "").lower()
    awards = _awards(details.get("Gidsbeoordeling", []))
    return Product(
        url=url, product_id=info["product_id"], title=re.sub(r"\s+", " ", title).strip(),
        colour=COLOURS.get(colour_nl) or _colour_from_name(colour_nl, title),
        volume_cl=_volume_cl(specs.pop("volume", None)) or 75.0,
        price=price, promotion=None, in_stock_online=in_stock, was_price=was,
        category=item.get("item_category") or colour_nl or None, expert_awards=awards, specs=specs,
    )


def _colour_from_name(kind: str, title: str) -> str | None:
    """Sparkling, sweet and fortified wines: the colour comes from the name."""
    if not re.search(r"mousser|champagne|dessert|zoet|versterkt|port|sherry|madeira", kind):
        return None
    t = title.lower()
    if re.search(r"ros[eé]\b", t):
        return "rosé"
    if re.search(r"\b(rood|rouge|rosso|tinto|ruby|tawny|lbv|vintage port|banyuls|maury)\b", t):
        return "red"
    return "white"


def is_wine_product(p: Product) -> bool:
    if not (p.price and p.colour) or NOT_WINE.search(p.title) or NOT_WINE.search(p.category or ""):
        return False
    return p.is_wine
