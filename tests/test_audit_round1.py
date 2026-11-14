"""Round 1 of the audit: bulk Shopify reads, one recompute, backups, deal rules, robots.txt."""
import datetime as dt
import json

import httpx

from wijnkoop import backup, collect, db, scores
from wijnkoop.shops import shopify, vinio


class FakeClient:
    def __init__(self, pages):
        self.pages, self.asked = pages, []

    def get(self, url):
        self.asked.append(url)
        return self.pages[url]


def _item(handle, price, available=True, compare=None):
    return {"handle": handle, "title": f"Wijn {handle} 2021", "vendor": "Producer", "product_type": "Red Wine",
            "tags": ["rood", "750ml"], "body_html": "<p>Herkomst: Italië, Toscane</p>",
            "variants": [{"title": "750 ml", "price": price, "compare_at_price": compare, "available": available}]}


def test_shopify_bulk_reads_250_per_request_and_parses_like_the_js():
    base = "https://www.vinio.nl"
    full = {"products": [_item(f"w{i}", "12.50") for i in range(250)]}
    last = {"products": [_item("last", "39.95", compare="45.50")]}
    client = FakeClient({f"{base}/products.json?limit=250&page=1": json.dumps(full),
                         f"{base}/products.json?limit=250&page=2": json.dumps(last)})
    got = vinio.prefetch(client)
    assert len(client.asked) == 2 and len(got) == 251
    p = vinio.parse_product(f"{base}/products/last", got["last"])
    assert (p.price, p.was_price, p.colour, p.in_stock_online) == (39.95, 45.5, "red", True)
    assert p.specs["vintage"] == "2021" and p.specs["country"] == "Italië"


def test_fetch_page_uses_the_bulk_first_then_reads_the_page():
    client = FakeClient({"https://www.vinio.nl/products/b.js": "{}"})
    pre = {"a": "{\"bulk\": 1}"}
    assert collect.fetch_page(vinio, client, "https://www.vinio.nl/products/a", pre, "a") == "{\"bulk\": 1}"
    assert collect.fetch_page(vinio, client, "https://www.vinio.nl/products/b", pre, "b") == "{}"
    assert client.asked == ["https://www.vinio.nl/products/b.js"]


def test_bulk_failure_falls_back_to_page_by_page():
    class Broken:
        SHOP_NAME = "X"

        @staticmethod
        def prefetch(client):
            raise ValueError("no json")

    assert collect.prefetch(Broken, None) == {}


def test_scores_once_when_no_shop_is_read(tmp_path):
    con = db.connect(tmp_path / "t.db")
    assert scores.recompute_if_due(con, busy=False) is None  # nothing new
    scores.mark_dirty(con)
    assert scores.recompute_if_due(con, busy=True) is None  # a shop is still being read
    later = dt.datetime.now() + dt.timedelta(hours=2)
    assert scores.recompute_if_due(con, busy=True, now=later) is not None  # waited long enough
    assert scores.recompute_if_due(con, busy=False) is None  # done: not again


def test_backup_and_rotation(tmp_path):
    con = db.connect(tmp_path / "t.db")
    folder = tmp_path / "backups"
    today = dt.date(2026, 11, 14)
    for days in range(60):
        (folder).mkdir(exist_ok=True)
        (folder / f"wijnkoop-{(today - dt.timedelta(days=days + 1)).isoformat()}.db").write_text("x")
    made = backup.make_backup(con, today, folder)
    assert made.exists() and made.stat().st_size > 1000
    left = sorted(f.name for f in folder.glob("*.db"))
    assert len(left) == 7 + 4
    assert not backup.backup_due(dt.datetime(2026, 11, 14, 12), folder)
    assert not backup.backup_due(dt.datetime(2026, 11, 15, 2), folder)  # before 04:00
    assert backup.backup_due(dt.datetime(2026, 11, 15, 5), folder)


def test_reference_price_ignores_was_price_and_uses_four_weeks():
    today = dt.date(2026, 11, 14)
    hist = [((today - dt.timedelta(weeks=w)).isoformat(), 10.0) for w in range(5, -1, -1)]
    assert scores._reference_price(hist, 20.0, today) == (10.0, "5-week median")
    assert scores._reference_price(hist[-2:], 20.0, today) == (20.0, "normal price")


def test_robots_404_allows_everything(monkeypatch):
    from wijnkoop import fetch

    def handler(request):
        return httpx.Response(404, text="not here")

    real = httpx.Client

    monkeypatch.setattr(fetch.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    c = fetch.PoliteClient("https://shop.example")
    assert c.allowed("https://shop.example/products/x")


def test_indexes_exist(tmp_path):
    con = db.connect(tmp_path / "t.db")
    names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"ix_offer_wine", "ix_award_match_wine", "ix_wine_cuvee"} <= names
    assert con.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL
