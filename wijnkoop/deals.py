"""Understand shop promotions and turn them into an effective price.

Loes buys at least 6 bottles per order, so every deal is valued at 6 bottles of the
same wine: "2e fles 50% korting" -> 0.75, "6 halen 5 betalen" -> 5/6,
"3 voor 25" on a EUR 10 bottle -> 25/30.
"""
import re
from dataclasses import dataclass

ORDER_BOTTLES = 6


@dataclass
class Deal:
    kind: str          # nth_bottle, buy_x_pay_y, percent, bundle_price, unknown
    min_qty: int       # bottles you must buy for the deal to apply
    factor: float      # price factor when buying 6 bottles of this wine (1.0 = no discount)


def _num(text):
    return float(text.replace(".", "").replace(",", ".")) if "," in text else float(text)


def parse_deal(text: str | None, price: float | None) -> Deal | None:
    if not text:
        return None
    t = text.lower().replace("\xa0", " ")
    q = ORDER_BOTTLES

    m = re.search(r"(\d+)e\s+fles\s+(\d+)\s*%\s*korting", t)
    if m:
        n, pct = int(m.group(1)), int(m.group(2)) / 100
        return Deal("nth_bottle", n, round((q - (q // n) * pct) / q, 4))
    m = re.search(r"(\d+)e\s+fles\s+gratis", t)
    if m:
        n = int(m.group(1))
        return Deal("nth_bottle", n, round((q - q // n) / q, 4))
    m = re.search(r"(\d+)\s*\+\s*(\d+)\s+gratis", t)
    if m:
        pay, free = int(m.group(1)), int(m.group(2))
        take = pay + free
        return Deal("buy_x_pay_y", take, round(((q // take) * pay + q % take) / q, 4))
    if re.search(r"2e\s+(fles\s+)?halve\s+prijs", t):
        return Deal("nth_bottle", 2, 0.75)
    m = re.search(r"(\d+)\s+halen,?\s+(\d+)\s+betalen", t)
    if m:
        take, pay = int(m.group(1)), int(m.group(2))
        if take > pay:
            return Deal("buy_x_pay_y", take, round(((q // take) * pay + q % take) / q, 4))
    m = re.search(r"(\d+)\s+(?:flessen\s+)?voor\s+(?:€\s*)?(\d+(?:[.,]\d{1,2})?)", t)
    if m and price:
        n, total = int(m.group(1)), _num(m.group(2))
        if n >= 2 and total < n * price:
            cost = (q // n) * total + (q % n) * price
            return Deal("bundle_price", n, round(cost / (q * price), 4))
    m = re.search(r"(\d+)\s*%\s*korting", t)
    if m:
        return Deal("percent", 1, round(1 - int(m.group(1)) / 100, 4))
    return Deal("unknown", 1, 1.0)


def order_of_six(bottle_price_at_6: float, delivery_cost: float | None,
                 free_from: float | None) -> tuple[float, float]:
    """Total for 6 bottles of this wine including delivery, and the delivery part."""
    goods = round(ORDER_BOTTLES * bottle_price_at_6, 2)
    delivery = 0.0
    if delivery_cost and (free_from is None or goods < free_from):
        delivery = delivery_cost
    return round(goods + delivery, 2), delivery
