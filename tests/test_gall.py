from pathlib import Path

from wijnkoop import db
from wijnkoop.collect import save_product
from wijnkoop.shops import gall

FIX = Path(__file__).parent / "fixtures"
URL = "https://www.gall.nl/catena-adrianna-malbec-fortuna-terrae-rood-75cl-100374.html"


def test_parse_url():
    info = gall.parse_url(URL)
    assert info["product_id"] == "100374"
    assert info["colour"] == "red"
    assert info["volume_cl"] == 75
    assert gall.parse_url("https://www.gall.nl/x-vors-37.5cl-110280.html")["volume_cl"] == 37.5


def test_sitemap():
    urls = gall.product_urls_from_sitemap((FIX / "sitemap.xml").read_text())
    assert len(urls) == 3
    wines = [u for u in urls if gall.parse_url(u)["colour"]]
    assert len(wines) == 2


def test_parse_product():
    p = gall.parse_product(URL, (FIX / "gall-product.html").read_text())
    assert p.title == "Catena Adrianna Malbec Fortuna Terrae"
    assert p.price == 80.0
    assert p.promotion == "2e fles 50% korting"
    assert p.awards_count == 12
    assert p.in_stock_online is False
    assert p.specs["country"] == "Argentinië"
    assert p.specs["vintage"] == "2020"
    assert p.specs["producer"] == "Bodega Catena Zapata"


def test_save(tmp_path):
    con = db.connect(tmp_path / "t.db")
    con.execute("INSERT INTO shop (name) VALUES ('Gall & Gall')")
    p = gall.parse_product(URL, (FIX / "gall-product.html").read_text())
    save_product(con, 1, p, "2026-11-11")
    save_product(con, 1, p, "2026-11-11")  # same day twice: no duplicates
    rows = con.execute("SELECT * FROM wine_list").fetchall()
    assert len(rows) == 1
    assert rows[0]["vintage"] == 2020
    assert rows[0]["price_per_75cl"] == 80.0
    assert con.execute("SELECT COUNT(*) FROM price_history").fetchone()[0] == 1
