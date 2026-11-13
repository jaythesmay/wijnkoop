"""Vanouds de Zwaan (vanoudsdezwaan.nl): a Shopify shop with about 2,900 products.

robots.txt allows /products/ and sets no crawl-delay. The product's "type" is its colour ("Rode wijn",
"Witte wijn", "Rosé", "Mousserend"). The "vendor" is the supplier (e.g. "Goessens Dropship"), not the
producer, so it is not used. The pages show no awards.
"""
from . import shopify

KEY = "vanoudsdezwaan"
SHOP_NAME = "Vanouds de Zwaan"
BASE = "https://vanoudsdezwaan.nl"


def list_products(client):
    return shopify.list_products(client, BASE)


fetch_page = shopify.fetch_page
is_wine_product = shopify.is_wine_product


def parse_product(url, text):
    return shopify.parse_product(url, text, vendor_is_producer=False)
