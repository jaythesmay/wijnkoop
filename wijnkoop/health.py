"""Noticing that a shop changed its site.

When a shop rebuilds its pages, WijnKoop suddenly reads pages without a price (or cannot read
them at all). A run where that is true for more than WARN_SHARE of the pages leaves a warning:
on the page Read shops now, and at the top of the weekly e-mail, until a later run of that shop
reads normally again.
"""
import datetime as dt

WARN_SHARE = 0.3
MIN_PAGES = 20


def _key(shop_key: str) -> str:
    return f"parser_warning:{shop_key}"


def check_run(con, shop, read: int, no_price: int, failed: int, today: dt.date | None = None) -> str | None:
    """After a run (or a part of one): note or clear the warning. Returns the warning, if any."""
    today = today or dt.date.today()
    pages = read + failed
    if pages < MIN_PAGES:
        return None
    bad = no_price + failed
    if bad / pages > WARN_SHARE:
        text = (f"{today:%d %b}: {bad} of {pages} pages had no price or could not be read. "
                "The shop may have changed its site; its prices may be out of date until WijnKoop is adjusted.")
        con.execute("INSERT OR REPLACE INTO setting (key, value) VALUES (?, ?)", (_key(shop.KEY), text))
        con.commit()
        return text
    con.execute("DELETE FROM setting WHERE key=?", (_key(shop.KEY),))
    con.commit()
    return None


def warning(con, shop_key: str) -> str | None:
    row = con.execute("SELECT value FROM setting WHERE key=?", (_key(shop_key),)).fetchone()
    return row[0] if row else None


def warnings(con) -> list[tuple[str, str]]:
    """[(shop name, warning)] for every shop with one."""
    from .shops import SHOPS

    out = []
    for key, module in SHOPS.items():
        text = warning(con, key)
        if text:
            out.append((module.SHOP_NAME, text))
    return out
