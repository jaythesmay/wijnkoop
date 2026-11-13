"""Shops WijnKoop reads. Each module offers KEY, SHOP_NAME, BASE, list_products(client) and
parse_product(url, html); is_wine_product(product) when the sitemap also lists non-wine;
NEEDS_BROWSER = True when prices only appear after the page's JavaScript ran."""
from . import gall, grandcru, grapedistrict, henribloem, okhuysen, vindict, vinocom, wijnbeurs, wijnvoordeel

SHOPS = {m.KEY: m for m in (gall, vindict, henribloem, okhuysen, wijnvoordeel, wijnbeurs, grandcru,
                                 grapedistrict, vinocom)}
