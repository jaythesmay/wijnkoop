"""Round 2 of the audit: reading only what changed, and noticing a shop that changed its site."""
import datetime as dt

from wijnkoop import collect, db, health, incremental, mail
from wijnkoop.collect import collect_shop
from wijnkoop.shops import gall


class Shop:
    """A small shop whose sitemap carries <lastmod>."""
    KEY, SHOP_NAME, BASE = "testshop", "Test shop", "https://shop.example"

    def __init__(self, lastmod):
        self.lastmod = lastmod

    def list_products(self, client):
        xml = "<urlset>" + "".join(
            f"<url><loc>{self.BASE}/w/{i}</loc><lastmod>{m}</lastmod></url>" for i, m in enumerate(self.lastmod)) + "</urlset>"
        client.get(self.BASE + "/sitemap.xml", xml=xml)
        return [(f"{self.BASE}/w/{i}", str(i)) for i in range(len(self.lastmod))]

    def parse_product(self, url, text):
        i = url.rsplit("/", 1)[1]
        return gall.GallProduct(url=url, product_id=i, title=f"Wijn {i} Rood", colour="red", volume_cl=75,
                                price=None if text == "broken" else 10.0, in_stock_online=True,
                                specs={"vintage": "2022"})


class Client:
    delay = 0

    def __init__(self, page="ok"):
        self.asked, self.page = [], page

    def get(self, url, xml=None):
        self.asked.append(url)
        return xml if xml is not None else self.page


def test_unchanged_pages_are_not_read_again(tmp_path, monkeypatch):
    con = db.connect(tmp_path / "t.db")
    monkeypatch.setattr("wijnkoop.awards.importer.match_awards", lambda con: None)
    shop = Shop(["2026-01-01", "2026-01-01", "2026-01-01"])
    first = Client()
    monkeypatch.setattr(collect, "make_client", lambda s: first)
    collect_shop(shop, con=con)
    assert len(first.asked) == 4  # sitemap + 3 pages: the first run reads everything
    # A week later, one page changed since. Make the earlier read look a day older than its last change.
    con.execute("UPDATE offer SET last_seen = '2026-01-02'")
    con.execute("INSERT OR REPLACE INTO setting (key, value) VALUES ('full_read:testshop', ?)",
                (dt.date.today().isoformat(),))
    con.commit()
    shop.lastmod = ["2025-12-01", "2026-03-01", "2025-12-01"]
    second = Client()
    monkeypatch.setattr(collect, "make_client", lambda s: second)
    r = collect_shop(shop, con=con)
    assert second.asked == ["https://shop.example/sitemap.xml", "https://shop.example/w/1"]
    assert r["ok"] == 3
    today = dt.date.today().isoformat()
    assert con.execute("SELECT COUNT(*) FROM offer WHERE last_seen=?", (today,)).fetchone()[0] == 3
    assert con.execute("SELECT COUNT(*) FROM price_history WHERE seen_on=?", (today,)).fetchone()[0] == 3


def test_a_full_read_every_four_weeks(tmp_path):
    con = db.connect(tmp_path / "t.db")
    today = dt.date(2026, 11, 14)
    assert incremental.full_due(con, "x", today)
    incremental.note_full(con, "x", today - dt.timedelta(days=10))
    assert not incremental.full_due(con, "x", today)
    assert incremental.full_due(con, "x", today + dt.timedelta(days=30))


def test_recorder_reads_lastmod_from_gzipped_sitemaps():
    import gzip

    class Raw:
        def get_bytes(self, url):
            return gzip.compress(b"<urlset><url><loc>https://a/x</loc><lastmod>2026-11-01T10:00:00Z</lastmod></url></urlset>")

    rec = incremental.SitemapRecorder(Raw())
    rec.get_bytes("https://a/sitemap.xml.gz")
    assert rec.lastmod == {"https://a/x": "2026-11-01"}


def test_a_shop_that_changed_its_site_gets_a_warning(tmp_path, monkeypatch):
    con = db.connect(tmp_path / "t.db")
    monkeypatch.setattr("wijnkoop.awards.importer.match_awards", lambda con: None)
    shop = Shop(["2026-01-01"] * 25)
    monkeypatch.setattr(collect, "make_client", lambda s: Client(page="broken"))
    collect_shop(shop, con=con)
    assert "25 of 25 pages" in health.warning(con, "testshop")
    text, html = mail.render([], dt.date(2026, 11, 18), warnings=[("Test shop", "check it")])
    assert "Shops to check" in text and "Test shop" in html
    con.execute("DELETE FROM setting WHERE key LIKE 'full_read:%'")
    monkeypatch.setattr(collect, "make_client", lambda s: Client(page="ok"))
    collect_shop(shop, con=con)
    assert health.warning(con, "testshop") is None
