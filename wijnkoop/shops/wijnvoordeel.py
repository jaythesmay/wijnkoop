"""Wijnvoordeel (wijnvoordeel.nl); Wijnbeurs runs on the same shop software (see wijnbeurs.py).

The product pages show their price only after the page's own JavaScript has run: that
JavaScript asks the shop's product service (Adobe Commerce, GraphQL at /api/.../graphql, which
robots.txt allows) for the product. WijnKoop asks the same service directly, without a browser:
the whole shop (about 370 or 520 products) in 4 to 6 requests of 100 products, 20 s apart as
robots.txt asks. The address and the few headers the service needs come from the shop's own
/config.json, read at the start of every run. A product missing there is asked for on its own.

parse_product also still reads a rendered page (the older way, kept for the tests and as a record):
A rendered page gives: the name (h1), "2024 | Frankrijk, Bordeaux" (vintage, country,
region), the price per bottle ("Per doos van 6"), stock, a specification list (grapes,
alcohol, classification) and medal images whose file names name the medal
("concours-international-lyon-goud-1-7_33.png"). The page names no colour: it comes from the
name and the grapes.
"""
import datetime as dt
import json
import re
from urllib.parse import unquote, urlencode

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from .gall import GallProduct as Product

KEY = "wijnvoordeel"
SHOP_NAME = "Wijnvoordeel"
BASE = "https://www.wijnvoordeel.nl"
PACE = 20  # crawl-delay in its robots.txt (November 2026); a longer one read at the start of a run wins
PACE_WHY = "the shop's robots.txt asks for this"
SITEMAP = BASE + "/products.xml"
NEEDS_BROWSER = False  # read through the shop's product service since November 2026
PAGE_SIZE = 100  # products per request; the service refuses 500
MAX_PAGES = 50

URL_RE = re.compile(r"^https://www\.(?:wijnvoordeel|wijnbeurs)\.nl/products/(?P<slug>[^/?#]+)$")
SPECS = {"classificatie": "appellation", "druivenras": "grapes", "alcoholgehalte": "alcohol",
         "smaakprofiel": "style", "op dronk tot": "drink_until", "sku": "sku"}
# Packages, cases, bag-in-box and gift cards are not single wines.
NOT_WINE = re.compile(r"pakket|proef|\bkist\b|\d+\s*flessen|cadeau|bib\b|bag.in.box|\d\s*ltr\b|magnum|wijnglas|"
                      r"karaf|alcoholvrij|0[.,]0\b|alcohol.?free|gin\b|whisk|likeur|cheque|kaart", re.I)

WHITE_GRAPES = ("chardonnay", "sauvignon blanc", "riesling", "pinot grigio", "pinot gris", "pinot blanc",
                "viognier", "chenin", "verdejo", "albariño", "albarino", "grüner", "gruner", "muscat",
                "moscato", "gewürztraminer", "gewurztraminer", "sémillon", "semillon", "colombard", "macabeo",
                "viura", "garganega", "vermentino", "trebbiano", "fiano", "greco", "falanghina", "picpoul",
                "airén", "airen", "marsanne", "roussanne", "grenache blanc", "torrontés", "torrontes", "glera",
                "malvasia", "godello", "assyrtiko", "cortese", "arneis", "pecorino", "verdicchio", "furmint",
                "silvaner", "müller", "muller", "kerner", "xarel", "parellada", "loureiro", "alvarinho")
RED_GRAPES = ("cabernet", "merlot", "pinot noir", "syrah", "shiraz", "malbec", "tempranillo", "grenache",
              "garnacha", "sangiovese", "nebbiolo", "primitivo", "zinfandel", "montepulciano", "nero d",
              "carmenère", "carmenere", "pinotage", "mourvèdre", "mourvedre", "monastrell", "carignan",
              "cariñena", "gamay", "barbera", "dolcetto", "corvina", "touriga", "petit verdot", "negroamaro",
              "aglianico", "mencía", "mencia", "bobal", "graciano", "mazuelo", "tannat", "cinsault",
              "blaufränkisch", "zweigelt", "petite sirah", "frappato", "cannonau", "lagrein", "teroldego")


def parse_url(url: str) -> dict | None:
    m = URL_RE.match(url.strip())
    return {"product_id": m.group("slug"), "slug": m.group("slug")} if m else None


def product_urls(xml: str) -> list[str]:
    return [u for u in re.findall(r"<loc>\s*([^<\s]+)", xml) if parse_url(u)]


def list_products(client, sitemap=SITEMAP) -> list[tuple[str, str]]:
    urls = dict.fromkeys(product_urls(client.get(sitemap)))
    return [(u, parse_url(u)["product_id"]) for u in urls]


# --- the shop's product service -----------------------------------------------------------

VIEW_FIELDS = """__typename sku name urlKey inStock attributes(roles: []) { name value }
  ... on SimpleProductView { price { regular { amount { value } } final { amount { value } } }
                             stock_is_in_stock stock_is_salable }"""
ALL_QUERY = ("query WijnKoopProducts($page: Int!, $size: Int!) { productSearch(phrase: \"\", page_size: $size, "
             "current_page: $page) { total_count page_info { total_pages } items { productView { "
             + VIEW_FIELDS + " } } } }")
ONE_QUERY = ("query WijnKoopProduct($urlKey: String!) { productSearch(phrase: \"\", filter: [{ attribute: \"url_key\", "
             "eq: $urlKey }], page_size: 2) { items { productView { " + VIEW_FIELDS + " } } } }")


def service(client, base: str) -> tuple[str, dict]:
    """(address, headers) of the shop's product service, from its own /config.json."""
    cached = getattr(client, "_commerce", None)
    if cached:
        return cached
    conf = json.loads(client.get(base + "/config.json"))["public"]["default"]
    headers = {**conf["headers"].get("all", {}), **conf["headers"].get("cs", {}), "Accept": "application/json"}
    client._commerce = (conf["commerce-endpoint"], headers)
    return client._commerce


def _ask(client, base: str, query: str, variables: dict) -> dict:
    endpoint, headers = service(client, base)
    url = endpoint + "?" + urlencode({"query": query, "variables": json.dumps(variables, separators=(",", ":"))})
    data = json.loads(client.get(url, headers=headers))
    if data.get("errors"):
        raise RuntimeError(f"product service: {data['errors'][0].get('message')}")
    return data["data"]["productSearch"]


def prefetch(client, base: str = BASE) -> dict[str, str]:
    """The whole shop in a few requests: {url key: the product as JSON text}."""
    out = {}
    for page in range(1, MAX_PAGES + 1):
        found = _ask(client, base, ALL_QUERY, {"page": page, "size": PAGE_SIZE})
        for item in found.get("items") or []:
            view = item.get("productView") or {}
            if view.get("urlKey"):
                out[view["urlKey"]] = json.dumps(view)
        if page >= ((found.get("page_info") or {}).get("total_pages") or 1):
            break
    return out


def fetch_page(client, url: str, base: str | None = None) -> str:
    """One product that was not in the shop-wide answer, by its url key."""
    base = base or ("https://" + url.split("/")[2])
    items = _ask(client, base, ONE_QUERY, {"urlKey": parse_url(url)["product_id"]}).get("items") or []
    if not items:  # in the sitemap but no longer sold: skipped like a non-wine, not a failure
        return json.dumps({"urlKey": parse_url(url)["product_id"], "name": None, "attributes": []})
    return json.dumps(items[0]["productView"])


COLOURS = {"rood": "red", "wit": "white", "rosé": "rosé", "rose": "rosé", "port": "red"}


def _attr(view: dict, name: str):
    return next((a.get("value") for a in view.get("attributes") or [] if a.get("name") == name), None)


def _text(value) -> str | None:
    if isinstance(value, list):
        value = ", ".join(str(v) for v in value if v)
    value = re.sub(r"\s+", " ", str(value)).strip() if value not in (None, "", "no_selection") else ""
    return value or None


def parse_view(url: str, view: dict) -> Product:
    """A product as the shop's product service gives it."""
    info = parse_url(url) or {"product_id": view.get("urlKey") or url.rstrip("/").rsplit("/", 1)[-1]}
    title = _text(view.get("name")) or info["product_id"].replace("-", " ")
    specs = {}
    for key, name in (("vintage", "pf_vintage"), ("country", "pf_country"), ("region", "pf_region"),
                      ("appellation", "pf_classification"), ("grapes", "pf_grape"), ("style", "tasteindex"),
                      ("drink_until", "pf_todrinktill"), ("box", "import_bottlespercase"),
                      ("alcohol", "pf_alcohol")):
        value = _text(_attr(view, name))
        if value:
            specs[key] = value
    if not re.fullmatch(r"(19|20)\d{2}", specs.get("vintage", "")):
        specs.pop("vintage", None)
    if view.get("sku"):
        specs["sku"] = view["sku"]
    price = ((view.get("price") or {}).get("final") or {}).get("amount", {}).get("value")
    regular = ((view.get("price") or {}).get("regular") or {}).get("amount", {}).get("value")
    was = regular if regular and price and regular > price else None
    in_stock = view.get("stock_is_in_stock")
    if in_stock is None:
        in_stock = view.get("inStock")
    if not in_stock and view.get("stock_is_salable"):
        specs["presale"] = "yes"  # can be ordered, delivered later
        in_stock = False
    colour_word = str(_attr(view, "color") or "").lower()
    colour = COLOURS.get(colour_word) or colour_for(title, specs.get("grapes"))
    if colour_word == "mix":
        colour = None
    try:
        volume = round(float(_attr(view, "p_vpe_content")) * 100, 1)
    except (TypeError, ValueError):
        volume = 0
    awards = []
    for i in range(1, 11):
        image = _text(_attr(view, f"product_medal_{i}"))
        words = medal_from_image(image) if image else None
        if words:
            awards.append((words, ""))
    package = str(_attr(view, "p_is_package") or "0") == "1"
    return Product(
        url=url, product_id=info["product_id"], title=title, colour=colour,
        volume_cl=volume if 10 <= volume <= 600 else 75.0, price=price, promotion=None,
        in_stock_online=None if in_stock is None else bool(in_stock), was_price=was,
        category="pakket" if package else "wijn", expert_awards=awards, specs=specs,
    )


def _money(node) -> float | None:
    if node is None:
        return None
    text = re.sub(r"\s+", "", node.text()).replace("€", "")
    m = re.search(r"(\d+(?:\.\d{3})*),(\d{2})", text) or re.search(r"(\d+)\.(\d{2})", text)
    return float(f"{m.group(1).replace('.', '')}.{m.group(2)}") if m else None


def colour_for(title: str, grapes: str | None) -> str | None:
    t = title.lower()
    if re.search(r"\bros[eéaé]\b|rosado|rosato|\bblush\b|\brosé", t):
        return "rosé"
    if re.search(r"\b(blanc|blanco|bianco|white|weiss|weiß|wit)\b", t):
        return "white"
    if re.search(r"\b(rouge|rosso|tinto|red|rood|rot)\b", t):
        return "red"
    g = (grapes or "").lower()
    red = any(x in g for x in RED_GRAPES) or any(x in t for x in RED_GRAPES)
    white = any(x in g for x in WHITE_GRAPES) or any(x in t for x in WHITE_GRAPES)
    if re.search(r"champagne|cava\b|prosecco|crémant|cremant|spumante|sekt\b|franciacorta|\bbrut\b", t):
        return "white"
    if red and not white:
        return "red"
    if white and not red:
        return "white"
    return None


MEDAL_WORD = re.compile(r"goud|gold|zilver|silver|brons|bronze|platin|trophy|trofee|medal|medaille", re.I)


def medal_from_image(src: str) -> str | None:
    """'concours-international-lyon-goud-1-7_33.png' -> 'concours international lyon goud'."""
    name = unquote(src.rsplit("/", 1)[-1]).split("?")[0]
    name = re.sub(r"\.(png|jpe?g|webp|svg|gif)$", "", name, flags=re.I)
    # A score without a medal: 'luca-maroni-97-20_21' -> 'luca maroni 97 punten'
    m = re.match(r"^(.*?[a-z])[-_](\d{2,3})((?:[-_]\d+)*)$", name, re.I)
    if m and 80 <= int(m.group(2)) <= 100 and not MEDAL_WORD.search(m.group(1)):
        name = f"{m.group(1)}-{m.group(2)}-punten"
    name = re.sub(r"(?:[-_]\d+)+$", "", name)  # file numbers at the end
    words = re.sub(r"[-_]+", " ", name).strip()
    return words or None


def parse_product(url: str, html: str) -> Product:
    if html.lstrip().startswith("{"):
        return parse_view(url, json.loads(html))
    tree = HTMLParser(html)
    info = parse_url(url) or {"product_id": url.rstrip("/").rsplit("/", 1)[-1], "slug": ""}
    main = tree.css_first(".product-details__right-column") or tree.body
    h1 = tree.css_first("h1.pdp-header__title") or tree.css_first("h1")
    title = re.sub(r"\s+", " ", h1.text()).strip() if h1 else info["slug"].replace("-", " ")
    specs = {}
    meta = tree.css_first(".product-details__wine-meta")
    if meta:
        parts = [p.strip() for p in meta.text().split("|")]
        if parts and re.fullmatch(r"(19|20)\d{2}", parts[0]):
            specs["vintage"] = parts.pop(0)
        if parts:
            specs["country"] = parts[0].split(",")[0].strip()
            where = ", ".join(parts).split(",", 1)
            if len(where) > 1 and where[1].strip():
                specs["region"] = where[1].strip(" ,|")
    for row in tree.css(".product-attribute-row"):
        n, v = row.css_first(".product-attribute-name"), row.css_first(".product-attribute-value")
        if n and v:
            key = SPECS.get(n.text().strip().lower())
            value = re.sub(r"\s+", " ", v.text()).strip()
            if key and value:
                specs[key] = value
    price_box = main.css_first(".pdp-price") if main else None
    price = was = None
    if price_box:
        price = _money(price_box.css_first(".pdp-price__amount-special") or price_box.css_first(".pdp-price__amount")
                       or price_box.css_first(".dropin-price"))
        old = price_box.css_first(".pdp-price__amount-regular, .dropin-price--strikethrough, s, del")
        was = _money(old)
        if was is not None and (price is None or was <= price):
            was = None
        box = price_box.css_first(".product-price-per-box__text")
        if box:
            m = re.search(r"doos van (\d+)", box.text())
            if m:
                specs["box"] = m.group(1)
    text = main.text() if main else ""
    if re.search(r"uitverkocht|niet (meer )?leverbaar|niet op voorraad", text, re.I):
        in_stock = False
    elif re.search(r"verwacht op|alvast bestellen", text, re.I):
        in_stock = False  # pre-order: delivered later
        specs["presale"] = "yes"
    elif "Op voorraad" in text:
        in_stock = True
    else:
        in_stock = None
    awards = []
    for img in tree.css(".product-medals--awards img"):
        words = medal_from_image(img.attributes.get("src") or "")
        if words:
            awards.append((words, ""))
    return Product(
        url=url, product_id=info["product_id"], title=title, colour=colour_for(title, specs.get("grapes")),
        volume_cl=75.0, price=price, promotion=None, in_stock_online=in_stock, was_price=was,
        category="wijn", expert_awards=awards, specs=specs,
    )


def is_wine_product(p: Product) -> bool:
    if p.category == "pakket":
        return False
    if not (p.price and p.colour) or NOT_WINE.search(p.title) or NOT_WINE.search(p.url):
        return False
    return p.is_wine
