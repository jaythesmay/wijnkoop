"""Shops WijnKoop reads. Each module offers KEY, SHOP_NAME, BASE, list_products(client) and
parse_product(url, html); is_wine_product(product) when the sitemap also lists non-wine."""
from . import gall, vindict

SHOPS = {m.KEY: m for m in (gall, vindict)}
