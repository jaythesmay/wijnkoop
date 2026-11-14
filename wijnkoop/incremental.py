"""Reading only what changed.

Most shops put a date (<lastmod>) next to every product in their sitemap: the last time the
product was changed. A wine WijnKoop read after that date has not changed since, so its page is
not opened again; the wine counts as seen this week with the price it had. Once every FULL_DAYS
a shop is read in full anyway, because not every shop updates that date when only a promotion
changes.

The dates are taken from the sitemaps the shop module reads anyway (no extra requests): the
client is wrapped while list_products runs, and every <url> with a <lastmod> is noted.
"""
import datetime as dt
import gzip
import logging
import re

log = logging.getLogger("wijnkoop")

FULL_DAYS = 28  # read every page of a shop at least this often
URL_ENTRY = re.compile(r"<url>\s*(.*?)</url>", re.S)
LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>")
LASTMOD = re.compile(r"<lastmod>\s*([^<\s]+)\s*</lastmod>")


class SitemapRecorder:
    """Passes every call to the real client and notes the <lastmod> of every sitemap entry."""

    def __init__(self, client):
        self._client = client
        self.lastmod: dict[str, str] = {}

    def __getattr__(self, name):
        return getattr(self._client, name)

    def _note(self, text: str) -> None:
        if "<lastmod>" not in text:
            return
        for entry in URL_ENTRY.findall(text):
            loc, mod = LOC.search(entry), LASTMOD.search(entry)
            if loc and mod:
                self.lastmod[loc.group(1).replace("&amp;", "&")] = mod.group(1)[:10]

    def get(self, url, *args, **kw):
        text = self._client.get(url, *args, **kw)
        if isinstance(text, str):
            self._note(text)
        return text

    def get_bytes(self, url, *args, **kw):
        raw = self._client.get_bytes(url, *args, **kw)
        try:
            self._note((gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw).decode("utf-8", "replace"))
        except (OSError, EOFError):
            pass
        return raw


def full_due(con, key: str, today: dt.date) -> bool:
    row = con.execute("SELECT value FROM setting WHERE key=?", (f"full_read:{key}",)).fetchone()
    return not row or (today - dt.date.fromisoformat(row[0])).days >= FULL_DAYS


def note_full(con, key: str, today: dt.date) -> None:
    con.execute("INSERT OR REPLACE INTO setting (key, value) VALUES (?, ?)", (f"full_read:{key}", today.isoformat()))


def unchanged(con, shop_id: int, products, lastmod: dict[str, str]) -> set[str]:
    """Product ids whose page has not changed since WijnKoop last read it."""
    if not lastmod:
        return set()
    seen = {r[0]: r[1] for r in con.execute(
        "SELECT shop_product_id, MAX(last_seen) FROM offer WHERE shop_id=? AND is_current=1 GROUP BY shop_product_id",
        (shop_id,))}
    skipped = {r[0]: r[1] for r in con.execute(
        "SELECT shop_product_id, seen_on FROM skipped_page WHERE shop_id=?", (shop_id,))}
    out = set()
    for url, pid in products:
        mod = lastmod.get(url)
        last = seen.get(pid) or skipped.get(pid)
        if mod and last and last > mod:  # read on a later day than its last change
            out.add(pid)
    return out


def carry_forward(con, shop_id: int, pids: set[str], today: str) -> int:
    """The unchanged wines count as seen today, with the price they had: last_seen moves on and the
    price history gets today's line, as if the page had been read."""
    if not pids:
        return 0
    n = 0
    for pid in pids:
        row = con.execute("SELECT * FROM offer WHERE shop_id=? AND shop_product_id=? AND is_current=1 "
                          "ORDER BY id DESC LIMIT 1", (shop_id, pid)).fetchone()
        if not row:
            con.execute("UPDATE skipped_page SET seen_on=? WHERE shop_id=? AND shop_product_id=?", (today, shop_id, pid))
            continue
        con.execute("UPDATE offer SET last_seen=?, gone_since=NULL WHERE id=?", (today, row["id"]))
        con.execute(
            """INSERT INTO price_history (offer_id, seen_on, price, promotion, was_price, effective_price_per_75cl,
                                          in_stock)
               VALUES (?,?,?,?,?,?,?) ON CONFLICT(offer_id, seen_on) DO NOTHING""",
            (row["id"], today, row["price"], row["promotion"], row["was_price"], row["effective_price_per_75cl"],
             row["in_stock_online"]))
        n += 1
    con.commit()
    return n
