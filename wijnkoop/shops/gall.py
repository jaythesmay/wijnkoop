"""Gall & Gall (gall.nl) collector.

Reads the product sitemap (allowed by robots.txt) and each wine page.
Never uses the site search or filter pages, which robots.txt disallows.
"""
import json
import re
from dataclasses import dataclass, field
from urllib.parse import unquote

from selectolax.lexbor import LexborHTMLParser as HTMLParser

KEY = "gall"
BASE = "https://www.gall.nl"
SITEMAP_INDEX = BASE + "/sitemap_index.xml"
SHOP_NAME = "Gall & Gall"

# URL looks like /catena-adrianna-malbec-fortuna-terrae-rood-75cl-100374.html
URL_RE = re.compile(
    r"/(?P<slug>[^/]+?)-(?P<volume>\d+(?:\.\d+)?)cl-(?P<pid>\d{5,7})\.html$"
)
COLOUR_TOKENS = {"rood": "red", "wit": "white", "rose": "rosé", "ros%C3%A9": "rosé"}

SPEC_LABELS = {
    "land": "country",
    "oorsprong": "country",
    "regio": "region",
    "druif": "grapes",
    "druiven": "grapes",
    "druivensoort": "grapes",
    "jaargang": "vintage",
    "inhoud": "volume",
    "alcoholpercentage": "alcohol",
    "alcohol": "alcohol",
    "producent": "producer",
    "wijnhuis": "producer",
    "leverancier": "producer",
    "stijl": "style",
    "smaak": "style",
    "soort": "kind",
}

PROMO_RE = re.compile(
    r"(\d+e\s+fles\s+\d+%\s+korting|\d+\s+halen,?\s+\d+\s+betalen|\d+\s+voor\s+[\d.,]+|\d+%\s+korting|"
    r"\d+\s+flessen\s+voor\s+[\d.,]+)",
    re.IGNORECASE,
)
NOT_WINE_RE = re.compile(
    r"\b(bier|abdijbier|alcoholvrij|alcohol-vrij|gl[uü]hwein|likeur|gin|whisk[e]?y|rum|vodka|"
    r"cocktail|cider|frisdrank|sap|0[.,]0)\b|bier\b", re.IGNORECASE)
YEAR_ONLY_RE = re.compile(r"^(19|20)\d{2}$")
AWARDS_RE = re.compile(r"(\d+)\s+keer\s+bekroond", re.IGNORECASE)
PRICE_META_RE = re.compile(r"voor\s+(\d+[.,]\d{2})\s*EUR", re.IGNORECASE)


@dataclass
class GallProduct:
    url: str
    product_id: str
    title: str
    colour: str | None = None
    volume_cl: float | None = None
    price: float | None = None
    promotion: str | None = None
    in_stock_online: bool | None = None
    awards_count: int | None = None
    was_price: float | None = None
    member_price: float | None = None   # Gall Premium members only; not counted as a deal
    category: str | None = None          # e.g. "Mousserend/Champagne", "Rood"
    shop_rating: float | None = None     # Gall customer reviews, 1-5
    shop_rating_count: int | None = None
    expert_awards: list = field(default_factory=list)  # [(source, result)], e.g. ("IWSC 2024", "Zilver met 90 punten")
    specs: dict = field(default_factory=dict)
    image_url: str | None = None          # the bottle photo at the shop (images.image_of)

    @property
    def is_wine(self) -> bool:
        """False for beer, spirits, gluhwein and alcohol-free drinks that share a colour word."""
        if NOT_WINE_RE.search(self.category or ""):
            return False
        alcohol = _to_float(self.specs.get("alcohol"))
        if alcohol is not None and alcohol < 0.5:
            return False
        if NOT_WINE_RE.search(self.title):
            return False
        return True


def parse_url(url: str) -> dict | None:
    m = URL_RE.search(url)
    if not m:
        return None
    slug = m.group("slug")
    tokens = slug.split("-")
    colour = None
    for tok in reversed(tokens[-3:]):
        if tok in COLOUR_TOKENS:
            colour = COLOUR_TOKENS[tok]
            break
    return {
        "slug": unquote(slug),
        "volume_cl": float(m.group("volume")),
        "product_id": m.group("pid"),
        "colour": colour,
    }


def product_urls_from_sitemap(xml: str) -> list[str]:
    locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", xml)
    return [u for u in locs if parse_url(u)]


def list_products(client) -> list[tuple[str, str]]:
    """[(url, product_id)] of the wines in the sitemap: the colour word sits in the address."""
    urls = []
    for sm in child_sitemaps(client.get(SITEMAP_INDEX)):
        urls += product_urls_from_sitemap(client.get(sm))
    return [(u, parse_url(u)["product_id"]) for u in urls if (parse_url(u) or {}).get("colour")]


def child_sitemaps(index_xml: str) -> list[str]:
    locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", index_xml)
    return [u for u in locs if "product" in u]


def _to_float(text: str | None) -> float | None:
    if not text:
        return None
    m = re.search(r"\d+(?:[.,]\d+)?", text.replace("\xa0", " "))
    if not m:
        return None
    return float(m.group(0).replace(",", "."))


def _jsonld_products(tree: HTMLParser):
    for node in tree.css('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.text())
        except (ValueError, TypeError):
            continue
        items = data if isinstance(data, list) else data.get("@graph", [data])
        for item in items:
            if isinstance(item, dict) and item.get("@type") in ("Product", ["Product"]):
                yield item


def _spec_pairs(tree: HTMLParser):
    """Yield (label, value) pairs from definition lists and two-cell tables/rows."""
    for dl in tree.css("dl"):
        dts, dds = dl.css("dt"), dl.css("dd")
        for dt, dd in zip(dts, dds):
            yield dt.text(strip=True), dd.text(strip=True)
    for tr in tree.css("tr"):
        cells = tr.css("th, td")
        if len(cells) == 2:
            yield cells[0].text(strip=True), cells[1].text(strip=True)
    # Generic "label / value" siblings, e.g. <div><span>Land</span><span>Spanje</span></div>
    for node in tree.css("li, div, p"):
        kids = [k for k in node.iter() if k.tag in ("span", "div", "strong", "b", "p", "dt", "dd")]
        if len(kids) == 2:
            a, b = kids[0].text(strip=True), kids[1].text(strip=True)
            if a and b and len(a) < 30 and len(b) < 120:
                yield a, b


def _specs_from_text(text: str) -> dict:
    """Fallback: 'Label: value' or 'Label\nvalue' patterns in the visible text."""
    specs = {}
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for i, line in enumerate(lines):
        label, _, value = line.partition(":")
        key = SPEC_LABELS.get(label.strip().lower())
        if key and value.strip():
            specs.setdefault(key, value.strip())
        elif SPEC_LABELS.get(line.lower()) and i + 1 < len(lines):
            specs.setdefault(SPEC_LABELS[line.lower()], lines[i + 1])
    return specs


def parse_product(url: str, html: str) -> GallProduct:
    info = parse_url(url) or {}
    tree = HTMLParser(html)

    title = ""
    h1 = tree.css_first("h1")
    if h1:
        title = h1.text(strip=True)
    if not title:
        og = tree.css_first('meta[property="og:title"]')
        title = (og.attributes.get("content") or "") if og else ""
    if not title:
        title = info.get("slug", "").replace("-", " ")

    product = GallProduct(
        url=url,
        product_id=info.get("product_id", ""),
        title=title,
        colour=info.get("colour"),
        volume_cl=info.get("volume_cl"),
    )

    # Price: structured data first, then meta tags, then the meta description.
    for item in _jsonld_products(tree):
        offers = item.get("offers") or {}
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        product.price = _to_float(str(offers.get("price") or "")) or product.price
        avail = str(offers.get("availability") or "")
        if avail:
            product.in_stock_online = "InStock" in avail
        brand = item.get("brand")
        if isinstance(brand, dict):
            brand = brand.get("name")
        if brand:
            product.specs.setdefault("producer", brand)
    if product.price is None:
        for sel in ('meta[property="product:price:amount"]', 'meta[itemprop="price"]',
                    '[itemprop="price"]'):
            node = tree.css_first(sel)
            if node:
                product.price = _to_float(node.attributes.get("content") or node.text())
                if product.price:
                    break
    if product.price is None:
        desc = tree.css_first('meta[name="description"]')
        if desc:
            m = PRICE_META_RE.search(desc.attributes.get("content") or "")
            if m:
                product.price = _to_float(m.group(1))

    _parse_pdp(tree, product)

    body = tree.body
    text = body.text(separator="\n") if body else ""

    for label, value in _spec_pairs(tree):
        key = SPEC_LABELS.get(label.strip().rstrip(":").lower())
        if key and value:
            product.specs.setdefault(key, value)
    for key, value in _specs_from_text(text).items():
        product.specs.setdefault(key, value)

    if product.promotion is None and not tree.css_first(".pdp-info_badges"):
        # Older or unknown page layout: look in the text, but only above the
        # "others also bought" tiles, which carry their own promotions.
        top = text.split("Anderen kochten ook")[0]
        m = PROMO_RE.search(top)
        if m:
            product.promotion = re.sub(r"\s+", " ", m.group(1))
    m = AWARDS_RE.search(text)
    if m:
        product.awards_count = int(m.group(1))
    if product.in_stock_online is None:
        low = text.lower()
        if "online uitverkocht" in low or "online niet op voorraad" in low:
            product.in_stock_online = False
        elif "in winkelmand" in low or "online op voorraad" in low:
            product.in_stock_online = True
    return product


def _price_node(node) -> float | None:
    """A Gall price element: <span class="price-value" data-decimals="99">8.</span>."""
    if node is None:
        return None
    pv = node if "price-value" in (node.attributes.get("class") or "") else node.css_first(".price-value")
    if pv is None:
        return None
    whole = re.sub(r"[^0-9]", "", pv.text())
    if not whole:
        return None
    dec = pv.attributes.get("data-decimals") or "00"
    if not dec.isdigit():
        dec = "00"
    return float(f"{whole}.{dec}")


def _parse_pdp(tree, product: "GallProduct"):
    """The product header and blocks of the 2026 gall.nl layout."""
    # Vintage, alcohol: <p class="pdp-info_desc"><span class="pdp-info_country">Frankrijk - Pinot noir</span>
    #                    <span>2018</span><i>75CL</i><i>12,5%</i></p>
    desc = tree.css_first(".pdp-info_desc")
    if desc:
        for node in desc.css("span, i"):
            t = node.text(strip=True)
            if YEAR_ONLY_RE.match(t):
                product.specs["vintage"] = t
            elif t.endswith("%") and _to_float(t) is not None:
                product.specs.setdefault("alcohol", t)
        country = desc.css_first(".pdp-info_country")
        if country:
            first, _, grapes = country.text(strip=True).partition(" - ")
            if first.strip():
                product.specs.setdefault("country", first.strip())
            if grapes.strip():
                product.specs.setdefault("grapes", grapes.strip())

    # Category, from the add-to-cart button's product data.
    for node in tree.css("[data-product]"):
        try:
            data = json.loads(node.attributes.get("data-product") or "")
        except ValueError:
            continue
        if str(data.get("id")) == product.product_id and data.get("category"):
            product.category = data["category"]
            break
    if not product.category:
        prof = tree.css_first(".pdp-info_profile-info")
        if prof:
            product.category = prof.text(strip=True)

    # Promotion badges. The Premium badge is a members-only price, not a deal.
    badges = tree.css_first(".pdp-info_badges")
    if badges:
        premium = badges.css_first(".premium-discount-badge__label")
        texts = []
        for em in badges.css("em, span"):
            if premium and (em == premium or "premium" in (em.attributes.get("class") or "")):
                continue
            t = re.sub(r"\s+", " ", em.text(strip=True))
            if t and t not in texts and "premium" not in t.lower():
                texts.append(t)
        if texts:
            product.promotion = texts[0]

    # Prices: a struck-through "Normaal" price next to a Premium price is the normal
    # price; next to anything else it is the was-price of a price cut.
    member = tree.css_first(".pdp-info_price.is--premium-discount")
    if member:
        product.member_price = _price_node(member)
    else:
        struck = tree.css_first(".pdp-info_price .price-original-value s")
        if struck:
            was = _to_float(struck.text())
            if was and product.price and was > product.price:
                product.was_price = was

    # Expert opinion: <div class="pdp-expert-review"><strong rel="author">IWSC 2024</strong>
    #                 <blockquote>Zilver met 90 punten</blockquote></div>
    for block in tree.css(".pdp-expert-review"):
        src = block.css_first("strong, [rel=author]")
        quote = block.css_first("blockquote")
        source = src.text(strip=True) if src else ""
        result = quote.text(strip=True) if quote else ""
        if source or result:
            product.expert_awards.append((source, result))

    # Customer reviews on gall.nl (JSON-LD aggregateRating).
    for item in _jsonld_products(tree):
        rating = item.get("aggregateRating") or {}
        if rating.get("ratingValue"):
            product.shop_rating = _to_float(str(rating.get("ratingValue")))
            product.shop_rating_count = int(rating.get("reviewCount") or 0) or None
