"""Wijnbeurs (wijnbeurs.nl): the same shop software as Wijnvoordeel, read the same way."""
from .wijnvoordeel import NEEDS_BROWSER, is_wine_product, parse_product, parse_url  # noqa: F401
from . import wijnvoordeel

KEY = "wijnbeurs"
SHOP_NAME = "Wijnbeurs"
BASE = "https://www.wijnbeurs.nl"
SITEMAP = BASE + "/products.xml"


def list_products(client):
    return wijnvoordeel.list_products(client, SITEMAP)
