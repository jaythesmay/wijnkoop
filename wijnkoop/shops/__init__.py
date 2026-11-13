"""Shops WijnKoop reads. Each module offers KEY, SHOP_NAME, BASE, list_products(client) and
parse_product(url, html); is_wine_product(product) when the sitemap also lists non-wine;
NEEDS_BROWSER = True when prices only appear after the page's JavaScript ran; fetch_page(client, url)
when parse_product reads something else than the product page itself."""
from . import (colaris, gall, grandcru, grapedistrict, henribloem, okhuysen, rootring, storms, vanoudsdezwaan,
               vindict, vinio, vinocom, wijnbeurs, wijnvoordeel, wijny)

SHOPS = {m.KEY: m for m in (gall, vindict, henribloem, okhuysen, wijnvoordeel, wijnbeurs, grandcru,
                                 grapedistrict, vinocom, vanoudsdezwaan, vinio, colaris, wijny, storms, rootring)}
