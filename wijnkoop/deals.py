"""Understand shop promotions and turn them into an effective price.

Loes buys at least 6 bottles per order, so every deal is valued at 6 bottles of the
same wine: "2e fles 50% korting" -> 0.75, "6 halen 5 betalen" -> 5/6,
"3 voor 25" on a EUR 10 bottle -> 25/30. Loes has Gall Premium, so a Premium price counts
when it is lower than the promotion.
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


NTH = r"(\d+)\s*(?:e|de|ste)\b\.?"  # "2e", "2de", "3ste"
WORD_NTH = {"tweede": 2, "derde": 3, "vierde": 4, "zesde": 6}


def _rule(text: str | None, price: float | None):
    """The promotion as a rule: ("nth", n, pct) every n-th bottle pct off; ("take_pay", take, pay);
    ("bundle", n, total) n bottles for a total price; ("percent", pct); or None when not understood."""
    if not text:
        return None
    t = text.lower().replace("\xa0", " ").replace("–", "-")
    for word, n in WORD_NTH.items():
        t = re.sub(rf"\b{word}\b", f"{n}e", t)
    # "2e fles 50% korting", "2de fles -50%", "2e fles voor 50%"
    m = re.search(NTH + r"\s+(?:fles\s+)?(?:voor\s+)?-?\s*(\d+)\s*%", t)
    if m:
        return ("nth", int(m.group(1)), int(m.group(2)) / 100)
    # "50% korting op de 2e fles"
    m = re.search(r"(\d+)\s*%\s*(?:korting\s+)?op\s+(?:de\s+|elke\s+)?" + NTH, t)
    if m:
        return ("nth", int(m.group(2)), int(m.group(1)) / 100)
    m = re.search(NTH + r"\s+(?:fles\s+)?gratis", t)
    if m:
        return ("nth", int(m.group(1)), 1.0)
    if re.search(r"\b2e\s+(?:fles\s+)?(?:voor\s+de\s+)?halve\s+prijs", t):
        return ("nth", 2, 0.5)
    m = re.search(r"(\d+)\s*\+\s*(\d+)\s+gratis", t)
    if m:
        pay, free = int(m.group(1)), int(m.group(2))
        return ("take_pay", pay + free, pay)
    m = re.search(r"(\d+)\s+halen,?\s+(\d+)\s+betalen", t)
    if m and int(m.group(1)) > int(m.group(2)):
        return ("take_pay", int(m.group(1)), int(m.group(2)))
    m = re.search(r"(\d+)\s+(?:flessen\s+)?voor\s+(€\s*)?(\d+(?:[.,]\d{1,2})?)", t)
    if m:
        n, euro, amount = int(m.group(1)), m.group(2), m.group(3)
        # "3 voor 2" is three bottles for the price of two, not three for EUR 2
        if not euro and amount.isdigit() and int(amount) < n:
            return ("take_pay", n, int(amount))
        total = _num(amount)
        if n >= 2 and price and total < n * price:
            return ("bundle", n, total)
        return None
    m = re.search(r"(\d+)\s*%\s*korting", t)
    if m:
        return ("percent", int(m.group(1)) / 100)
    return None


def _cost(rule, price: float, qty: int) -> float:
    if rule is None:
        return price * qty
    kind = rule[0]
    if kind == "nth":
        _, n, pct = rule
        return price * (qty - (qty // n) * pct)
    if kind == "take_pay":
        _, take, pay = rule
        return price * ((qty // take) * pay + qty % take)
    if kind == "bundle":
        _, n, total = rule
        return (qty // n) * total + (qty % n) * price
    return price * qty * (1 - rule[1])


KINDS = {"nth": "nth_bottle", "take_pay": "buy_x_pay_y", "bundle": "bundle_price", "percent": "percent"}


def parse_deal(text: str | None, price: float | None) -> Deal | None:
    if not text:
        return None
    rule = _rule(text, price)
    if rule is None:
        return Deal("unknown", 1, 1.0)
    min_qty = 1 if rule[0] == "percent" else rule[1]
    base = price or 10.0  # the factor of a rule without a fixed total does not depend on the price
    return Deal(KINDS[rule[0]], min_qty, round(_cost(rule, base, ORDER_BOTTLES) / (ORDER_BOTTLES * base), 4))


def effective_per_75cl(price: float | None, volume_cl: float | None, promotion: str | None,
                       member_price: float | None = None) -> float | None:
    """What one 75 cl bottle really costs in an order of 6: the promotion applied, or the
    Gall Premium price when that is lower (Loes has Premium; the two are not combined)."""
    if not price or not volume_cl:
        return None
    deal = parse_deal(promotion, price)
    bottle = price * (deal.factor if deal else 1.0)
    if member_price and member_price < bottle:
        bottle = member_price
    return round(bottle * 75 / volume_cl, 2)


def order_of_six(bottle_price_at_6: float, delivery_cost: float | None,
                 free_from: float | None) -> tuple[float, float]:
    """Total for 6 bottles of this wine including delivery, and the delivery part."""
    goods = round(ORDER_BOTTLES * bottle_price_at_6, 2)
    delivery = 0.0
    if delivery_cost and (free_from is None or goods < free_from):
        delivery = delivery_cost
    return round(goods + delivery, 2), delivery


def line_cost(promotion: str | None, price: float, qty: int, member_price: float | None = None) -> float:
    """What qty bottles of one wine cost with the shop's promotion applied, or at the Premium price
    when that is lower."""
    if not price or qty <= 0:
        return 0.0
    cost = _cost(_rule(promotion, price), price, qty)
    if member_price and member_price * qty < cost:
        cost = member_price * qty
    return round(cost, 2)
