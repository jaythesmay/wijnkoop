"""What Shopify shops share: the product list in /sitemap.xml and, per product, the small JSON
Shopify serves at <product page>.js (title, type, tags, price in cents, variants, description).
One small request per wine instead of a full page of several hundred kB.

A shop module sets KEY, SHOP_NAME, BASE and calls these with its own settings.
"""
import json
import re

from ..bottles import volume_from_text
from ..reference import awards_in_text
from .gall import GallProduct as Product

URL_RE = re.compile(r"^https://[^/]+/products/(?P<handle>[^/?#.]+)/?$")
COLOURS = [  # checked in this order on the shop's type, then its tags, then the name
    ("rosé", r"\bros[eé]\b|\brosato\b|\brosado\b"),
    ("red", r"\brode?\b|\brood\b|\bred\b|\brosso\b|\btinto\b|\brouge\b|\bport\b|\bruby\b|\btawny\b"),
    ("white", r"\bwitt?e?\b|\bwhite\b|\bbianco\b|\bblanco\b|\bblanc\b"),
]
SPARKLING_OR_SWEET = re.compile(
    r"mousser|sparkling|bubbel|champagne|prosecco|cava\b|cremant|crémant|spumante|franciacorta|sekt\b|"
    r"dessert|zoet|sweet|sauternes|passito|moscato|tokaji", re.I)
NOT_WINE = re.compile(r"pakket|proefdoos|proeverij|cadeaubon|cadeaukaart|gift\s*card|wijnclub|abonnement|"
                      r"\bglas|glazen|karaf|kurkentrekker|\bgin\b|whisk|\brum\b|vodka|likeur|grappa|cognac|"
                      r"\bbier\b|alcoholvrij|alcohol-free|0[.,]0\b|\bolie\b|olijfolie|azijn", re.I)
BOX = re.compile(r"\bkist\b|\bdoos\b|\bbox\b|\b\d+\s*(?:fles|flessen|bottles?|x)\b", re.I)
SPECS = {
    "herkomst": "country", "land": "country", "regio": "region", "streek": "region",
    "druivensoort": "grapes", "druivensoorten": "grapes", "druiven": "grapes", "druif": "grapes",
    "alcoholgehalte": "alcohol", "alcohol": "alcohol", "producent": "producer", "wijnhuis": "producer",
    "classificatie": "classification", "jaargang": "vintage", "inhoud": "volume",
}


def parse_url(url: str) -> dict | None:
    m = URL_RE.match(url.strip())
    return {"product_id": m.group("handle")} if m else None


def product_urls(xml: str) -> list[str]:
    return [u for u in re.findall(r"<loc>\s*([^<\s]+)", xml) if parse_url(u)]


def list_products(client, base: str) -> list[tuple[str, str]]:
    index = client.get(base + "/sitemap.xml")
    urls = []
    for sub in re.findall(r"<loc>\s*([^<\s]*sitemap_products_\d+\.xml[^<\s]*)", index):
        urls += product_urls(client.get(sub.replace("&amp;", "&")))
    return [(u, parse_url(u)["product_id"]) for u in dict.fromkeys(urls)]


def fetch_page(client, url: str) -> str:
    return client.get(url.rstrip("/") + ".js")


BULK_SIZE = 250  # the most products Shopify gives per page of /products.json
BULK_MAX_PAGES = 200


def _cents(value) -> int | None:
    try:
        return round(float(value) * 100)
    except (TypeError, ValueError):
        return None


def as_product_js(item: dict) -> dict:
    """One product of /products.json in the shape of <product page>.js, so parse_product reads both."""
    variants = [{"title": v.get("title"), "price": _cents(v.get("price")),
                 "compare_at_price": _cents(v.get("compare_at_price")), "available": v.get("available")}
                for v in item.get("variants") or []]
    first = variants[0] if variants else {}
    return {"handle": item.get("handle"), "title": item.get("title"), "vendor": item.get("vendor"),
            "type": item.get("product_type"), "tags": item.get("tags") or [],
            "description": item.get("body_html"), "variants": variants, "price": first.get("price"),
            "compare_at_price": first.get("compare_at_price"),
            "available": any(v.get("available") for v in variants) if variants else None}


def prefetch(client, base: str) -> dict[str, str]:
    """Every product with its price in a few requests: /products.json gives 250 products per page,
    so a shop of 2,900 wines takes about 12 requests instead of 2,900. {handle: product JSON as text}.
    Products missing here are read one by one as before."""
    out = {}
    for page in range(1, BULK_MAX_PAGES + 1):
        items = json.loads(client.get(f"{base}/products.json?limit={BULK_SIZE}&page={page}")).get("products") or []
        for item in items:
            if item.get("handle"):
                out[item["handle"]] = json.dumps(as_product_js(item))
        if len(items) < BULK_SIZE:
            break
    return out


def _colour(kind: str, tags: list[str], title: str) -> str | None:
    for text in (kind, " ".join(tags)):
        for colour, pattern in COLOURS:
            if re.search(pattern, text, re.I):
                return colour
    if SPARKLING_OR_SWEET.search(f"{kind} {' '.join(tags)} {title}"):
        for colour, pattern in COLOURS:
            if re.search(pattern, title, re.I):
                return colour
        return "white"
    return None


def _specs_from_description(text: str) -> dict:
    """"<strong>Herkomst</strong>: Italië, Toscane<br>" and plain "Druivensoorten: Sangiovese" lines."""
    plain = re.sub(r"<br\s*/?>|</p>|</li>|</tr>|</td>", "\n", text or "", flags=re.I)
    plain = re.sub(r"<[^>]+>", "", plain).replace("&nbsp;", " ")
    plain = re.sub(r"\s+[·|]\s+", "\n", plain)  # "Druiven: X · Alcohol: 11,5% · Inhoud: 0,75 L"
    specs = {}
    for line in plain.splitlines():
        m = re.match(r"\s*[-•]?\s*([A-Za-zë ]{3,25}?)\s*:\s*(.+?)\s*$", line)
        if m and m.group(1).strip().lower() in SPECS:
            key = SPECS[m.group(1).strip().lower()]
            specs.setdefault(key, re.sub(r"\s+", " ", m.group(2)))
    if "," in specs.get("country", ""):
        country, region = (x.strip() for x in specs["country"].split(",", 1))
        specs["country"] = country
        specs.setdefault("region", region)
    return specs


def parse_product(url: str, text: str, vendor_is_producer: bool = True) -> Product:
    d = json.loads(text)
    info = parse_url(url) or {"product_id": d.get("handle") or url.rstrip("/").rsplit("/", 1)[-1]}
    variants = d.get("variants") or [{}]
    # The single bottle, not the box of six the shop also sells
    variant = next((v for v in variants if not BOX.search(v.get("title") or "")), variants[0])
    price = (variant.get("price") if variant.get("price") is not None else d.get("price"))
    price = price / 100 if price else None
    compare = variant.get("compare_at_price") or d.get("compare_at_price")
    was = compare / 100 if compare and price and compare / 100 > price else None
    title = re.sub(r"\s+", " ", d.get("title") or "").strip()
    tags = [str(t) for t in d.get("tags") or []]
    kind = d.get("type") or ""
    specs = _specs_from_description(d.get("description"))
    years = re.findall(r"\b((?:19|20)\d{2})\b", title)
    if years:
        specs["vintage"] = years[-1]
    elif not re.fullmatch(r"(19|20)\d{2}", specs.get("vintage", "")):
        specs.pop("vintage", None)
    # The vintage is part of the name in these shops; the wine is the same in every year.
    title = re.sub(rf"\s+(?:{specs['vintage']}|NV|N\.V\.)$" if specs.get("vintage") else r"\s+(?:NV|N\.V\.)$",
                   "", title, flags=re.I)
    if vendor_is_producer and d.get("vendor") and not specs.get("producer"):
        specs["producer"] = re.sub(r"\s+", " ", d["vendor"]).strip()
    alc = next((t for t in tags if re.fullmatch(r"alc\.?\s*\d+(?:[.,]\d)?%?", t, re.I)), None)
    if alc and not specs.get("alcohol"):
        specs["alcohol"] = re.search(r"\d+(?:[.,]\d)?", alc).group(0) + "%"
    volume = volume_from_text(variant.get("title"), specs.pop("volume", None), " ".join(tags), title)
    return Product(
        url=url, product_id=info["product_id"], title=title, colour=_colour(kind, tags, title),
        volume_cl=volume or 75.0, price=price, was_price=was,
        in_stock_online=variant.get("available", d.get("available")), category=kind or None,
        expert_awards=awards_in_text(d.get("description")), specs=specs,
    )


def is_wine_product(p: Product) -> bool:
    if not (p.price and p.colour) or NOT_WINE.search(p.title) or NOT_WINE.search(p.category or ""):
        return False
    return p.is_wine
