"""The bottle photo of a shop's product page, for comparing two wines side by side on the data
quality page. Read from what the run fetches anyway (no extra requests): the page's og:image,
the image of its JSON-LD Product, or a Shopify product's featured image. The photo itself stays
at the shop: the page shows it from there."""
import json
import re
from urllib.parse import urljoin

OG = re.compile(r"""<meta[^>]+(?:property|name)=["'](?:og:image|og:image:url|twitter:image)["'][^>]*>""", re.I)
CONTENT = re.compile(r"""content=["']([^"']+)["']""", re.I)
LD = re.compile(r"""<script[^>]+application/ld\+json[^>]*>(.*?)</script>""", re.I | re.S)


def _first_image(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list) and value:
        return _first_image(value[0])
    if isinstance(value, dict):
        return value.get("url") or value.get("contentUrl") or value.get("src")
    return None


def _ld_products(node):
    if isinstance(node, list):
        for n in node:
            yield from _ld_products(n)
    elif isinstance(node, dict):
        kind = node.get("@type")
        if kind == "Product" or (isinstance(kind, list) and "Product" in kind):
            yield node
        for n in node.get("@graph") or []:
            yield from _ld_products(n)


def _clean(src: str | None, page_url: str) -> str | None:
    if not src or not isinstance(src, str):
        return None
    src = src.strip()
    if src.startswith("//"):
        src = "https:" + src
    src = urljoin(page_url, src)
    return src if src.startswith(("https://", "http://")) and len(src) < 1000 else None


def image_of(text: str | None, page_url: str) -> str | None:
    """The product photo's address, or None."""
    if not text:
        return None
    head = text.lstrip()[:1]
    if head in "{[":  # a shop's product JSON (Shopify)
        try:
            d = json.loads(text)
        except ValueError:
            return None
        if isinstance(d, dict):
            return _clean(d.get("featured_image") or _first_image(d.get("images")) or _first_image(d.get("image")),
                          page_url)
        return None
    for block in LD.findall(text):
        try:
            data = json.loads(block.strip())
        except ValueError:
            continue
        for product in _ld_products(data):
            src = _clean(_first_image(product.get("image")), page_url)
            if src:
                return src
    m = OG.search(text)
    if m:
        c = CONTENT.search(m.group(0))
        if c:
            return _clean(c.group(1).replace("&amp;", "&"), page_url)
    return None
