"""Wijnbeurs (wijnbeurs.nl): the same shop software as Wijnvoordeel, read the same way."""
from .wijnvoordeel import NEEDS_BROWSER, is_wine_product, parse_product, parse_url  # noqa: F401
from . import wijnvoordeel

KEY = "wijnbeurs"
SHOP_NAME = "Wijnbeurs"
BASE = "https://www.wijnbeurs.nl"
PACE = 20  # crawl-delay in its robots.txt (November 2026); a longer one read at the start of a run wins
PACE_WHY = "the shop's robots.txt asks for this"
SITEMAP = BASE + "/products.xml"


def list_products(client):
    return wijnvoordeel.list_products(client, SITEMAP)
