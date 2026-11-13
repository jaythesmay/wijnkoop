"""Vinio (vinio.nl): an importer of Italian wine on Shopify, about 180 products.

robots.txt allows /products/ and sets no crawl-delay. The "vendor" is the producer. The description
lists critics' scores ("Robert Parker 95/100 - Wine Spectator 95/100") and details ("Herkomst: Italië,
Toscane", "Druivensoorten: 100% Sangiovese").
"""
from . import shopify

KEY = "vinio"
SHOP_NAME = "Vinio"
BASE = "https://www.vinio.nl"


def list_products(client):
    return shopify.list_products(client, BASE)


fetch_page = shopify.fetch_page
is_wine_product = shopify.is_wine_product


def parse_product(url, text):
    return shopify.parse_product(url, text, vendor_is_producer=True)
