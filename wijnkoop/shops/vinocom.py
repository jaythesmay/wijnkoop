"""Vino.com (vino.com/nl): an Italian online wine shop that delivers in the Netherlands.

robots.txt allows product pages ("/nl/dettaglio/...") and sets no crawl-delay for other robots,
so the polite default applies. The Dutch sitemap lists every product (wine, spirits, food);
spirits and food are left out by their address where possible, the rest by the shop's own
category. A product page holds:

- the analytics data of the product (name, producer, price, wine type such as "rossi");
- a details table ("jaargang", "land", "regio", "druivensoort", "formaat", "alcohol");
- the lowest price of the last 30 days ("Laagste prijs"), shown when the wine is reduced;
- an optional quantity price ("6 flessen" at a lower price per bottle);
- critic scores (James Suckling, Robert Parker, Falstaff, ...), often of another vintage.
  The shop's own "Vino.com" score is not a critic and is left out.
"""
import html as htmllib
import json
import re

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from .gall import GallProduct as Product

KEY = "vinocom"
SHOP_NAME = "Vino.com"
BASE = "https://www.vino.com"
SITEMAP = BASE + "/nl/sitemap.xml"

URL_RE = re.compile(r"^https://www\.vino\.com/nl/dettaglio/(?P<slug>[a-z0-9-]+)\.html$")
# Spirits, beer, food and gift sets, recognised by the address so their pages are never fetched
NOT_WINE_SLUG = re.compile(
    r"(^|-)(cognac|armagnac|calvados|brandy|whisky|whiskey|bourbon|rum|ron|gin|vodka|tequila|mezcal|grappa|"
    r"acquavite|amaro|liquore|likeur|limoncello|vermouth|bitter|sambuca|birra|beer|olio|aceto|balsamico|"
    r"panettone|pandoro|cioccolat\w*|box|cassetta|confezione|kit|degustazione|\d+x\d+)(-|$)")
NOT_WINE = re.compile(r"distillat|liquor|birra|olio|aceto|accessor|alimentar|box", re.I)
COLOURS = {"rossi": "red", "bianchi": "white", "rosati": "rosé"}
SPARKLING_ETC = re.compile(r"spumant|champagne|passit|dolc|fortificat|liquoros", re.I)
DETAILS = {"jaargang": "vintage", "land": "country", "regio": "region", "druivensoort": "grapes",
           "appellatie": "appellation", "alcohol": "alcohol", "dosage": "style", "formaat": "volume"}
SHOP_OWN = re.compile(r"^vino\.com$", re.I)


def parse_url(url: str) -> dict | None:
    m = URL_RE.match(url.strip())
    return {"slug": m.group("slug")} if m else None


def product_urls(xml: str) -> list[str]:
    return [u for u in re.findall(r"<loc>\s*([^<\s]+)", xml)
            if parse_url(u) and not NOT_WINE_SLUG.search(parse_url(u)["slug"])]


def list_products(client) -> list[tuple[str, str]]:
    urls = dict.fromkeys(product_urls(client.get(SITEMAP)))
    # The address is the product's key: the product number is only on the page itself.
    return [(u, parse_url(u)["slug"]) for u in urls]


def _analytics_item(html: str, sku: str) -> dict:
    """The analytics data of this product (the page also has data of suggested products)."""
    for m in re.finditer(r'dataLayer\.push\(\{\s*"ecommerce"\s*:\s*(\{.*?\})\s*,\s*"event"', html, re.S):
        try:
            items = json.loads(m.group(1)).get("items") or []
        except ValueError:
            continue
        for item in items:
            if not sku or str(item.get("item_id")) == sku:
                return item
    return {}


def _euros(text: str | None) -> float | None:
    """'37,90 €', '1.250,00 €' or '26.6'."""
    t = (text or "").strip()
    if re.search(r"\d,\d{1,2}\b", t):
        t = t.replace(".", "").replace(",", ".")
    m = re.search(r"\d+(?:\.\d{1,2})?", t)
    return float(m.group(0)) if m else None


def _volume_cl(text: str | None) -> float | None:
    if not text:
        return None
    if re.search(r"magnum", text, re.I) and not re.search(r"\d", text):
        return 150.0
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(ℓ|l\b|cl)", text, re.I)
    if not m:
        return None
    v = float(m.group(1).replace(",", "."))
    return round(v * 100, 1) if m.group(2).lower() in ("ℓ", "l") else v


def _awards(tree, vintage: str | None) -> list[tuple]:
    """[(critic, result, vintage)] from the product's own awards list. Other products on the page
    (suggestions further down) have their own lists, which are left out."""
    out, seen = [], set()
    main = tree.css_first(".product-detail")
    if main is None:
        return out
    for el in main.css(".awards-container .awords-element, .awards-container .awards-element"):
        source = el.css_first(".descrizione")
        year = el.css_first(".annata")
        value = el.css_first(".valore")
        source = re.sub(r"\s+", " ", source.text()).strip() if source else ""
        year = (year.text().strip() if year else "")
        value = re.sub(r"\s+", " ", value.text()).strip() if value else ""
        if not source or not value or SHOP_OWN.match(source):
            continue
        m = re.match(r"(\d{1,3}(?:[.,]\d)?)\s*/\s*(\d{2,3})", value)
        if m:
            result = f"{m.group(1)}/20" if m.group(2) == "20" else f"{m.group(1)} punten"
        else:
            result = value
        key = (source, year, result)
        if key in seen:
            continue
        seen.add(key)
        out.append((source, result, int(year) if year.isdigit() else None))
    return out


def _quantity_price(html: str, sku: str) -> tuple[int, float] | None:
    """(bottles, price per bottle) of a quantity price for this product, if any."""
    if not sku:
        return None
    n = re.search(rf'watcParams{sku}\.quantityDiscountItemNumber1\s*=\s*"(\d+)"', html)
    p = re.search(rf"watcParams{sku}\.quantityDiscountSalePriceDouble1\s*=\s*([\d.]+)", html)
    if n and p and float(p.group(1)) > 0:
        return int(n.group(1)), float(p.group(1))
    return None


def parse_product(url: str, html: str) -> Product:
    tree = HTMLParser(html)
    info = parse_url(url) or {"slug": url.rstrip("/").rsplit("/", 1)[-1]}
    # The product's own data sits in the first schema.org Product block; suggestions come later.
    main = tree.css_first('[itemtype$="schema.org/Product"]') or tree.body or tree.root
    sku_node = main.css_first('[itemprop="sku"]')
    sku = (sku_node.attributes.get("content") or "") if sku_node else ""
    item = _analytics_item(html, sku)
    specs = {}
    for row in main.css(".row.scheda"):
        label, value = row.css_first(".nome"), row.css_first(".descrizione")
        if label and value:
            key = DETAILS.get(re.sub(r"\s+", " ", label.text()).strip().lower())
            if key and key not in specs:
                specs[key] = re.sub(r"\s+", " ", value.text()).strip()
    producer = item.get("item_brand")
    if producer:
        specs["producer"] = producer
    name_node = main.css_first('[itemprop="name"]')
    name = item.get("item_name") or (htmllib.unescape(name_node.attributes.get("content") or "") if name_node else "")
    vintage = specs.get("vintage")
    if vintage:
        name = re.sub(rf"\s*\b{vintage}\b", "", name)
    # The name ends with the producer ("Rioja DOCa Reserva Marqués de Riscal"): put it in front.
    if producer and name.lower().endswith(producer.lower()):
        name = f"{producer} {name[: -len(producer)].strip()}"
    elif producer and producer.lower() not in name.lower():
        name = f"{producer} {name}"
    price = item.get("price")
    if price is None:
        node = main.css_first('[itemprop="price"]')
        price = _euros(node.attributes.get("content") or node.text()) if node else None
    price = float(price) if price is not None else None
    m = re.search(r"Laagste prijs:\s*<span>([^<]*)</span>", main.html or "")
    was = _euros(m.group(1)) if m else None
    if was is not None and (price is None or was <= price):
        was = None
    qty = _quantity_price(html, sku)
    promotion = f"{qty[0]} flessen voor €{qty[0] * qty[1]:.2f}".replace(".", ",") if qty else None
    avail = main.css_first('[itemprop="availability"]')
    avail = (avail.attributes.get("href") or avail.attributes.get("content") or "") if avail else ""
    in_stock = None
    if avail:
        in_stock = avail.rstrip("/").rsplit("/", 1)[-1] in ("InStock", "LimitedAvailability")
    kind = (item.get("item_category2") or "").lower()
    category = " / ".join(x for x in (item.get("item_category"), item.get("item_category2")) if x) or None
    rating = main.css_first('[itemprop="ratingValue"]')
    count = main.css_first('[itemprop="reviewCount"]')
    count_text = (count.attributes.get("content") or count.text()) if count else ""
    return Product(
        url=url, product_id=info["slug"], title=re.sub(r"\s+", " ", name).strip(),
        colour=COLOURS.get(kind) or _colour_from_name(kind, name),
        volume_cl=_volume_cl(specs.pop("volume", None)) or 75.0,
        price=price, promotion=promotion, in_stock_online=in_stock, was_price=was, category=category,
        shop_rating=float(rating.attributes["content"]) if rating and rating.attributes.get("content") else None,
        shop_rating_count=int(re.sub(r"\D", "", count_text)) if re.search(r"\d", count_text) else None,
        expert_awards=_awards(tree, vintage), specs=specs,
    )


def _colour_from_name(kind: str, name: str) -> str | None:
    """Sparkling, sweet and fortified wines: the colour comes from the name."""
    if not SPARKLING_ETC.search(kind):
        return None
    t = name.lower()
    if re.search(r"\bros[eéa]\b|rosato|rosé", t):
        return "rosé"
    if re.search(r"\b(rosso|rouge|tinto|red|ruby|tawny|lbv|lambrusco|brachetto|recioto della valpolicella)\b", t):
        return "red"
    return "white"


def is_wine_product(p: Product) -> bool:
    if not (p.price and p.colour) or NOT_WINE.search(p.category or ""):
        return False
    return p.is_wine
