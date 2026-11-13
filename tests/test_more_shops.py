from pathlib import Path

from wijnkoop import db
from wijnkoop.awards.importer import Matcher
from wijnkoop.collect import collect_shop, save_product
from wijnkoop.deals import parse_deal
from wijnkoop.reference import parse_award_text
from wijnkoop.shops import SHOPS, henribloem, okhuysen
from wijnkoop.shops.gall import GallProduct

REAL = Path(__file__).parent / "fixtures" / "real"
TOSO = "https://henribloem.nl/product/pascual-toso-malbec-2024/"
SEKT = "https://henribloem.nl/product/meulenhof-riesling-sekt-nm/"
FIFTY = "https://okhuysen.nl/product/5050-rosso-di-toscana-94428"
LUGANA = "https://okhuysen.nl/product/lugana-93901"
TODAY = "2026-11-13"


def _page(name):
    return (REAL / f"{name}.html").read_text()


def test_shops_registered():
    assert {"gall", "vindict", "henribloem", "okhuysen"} <= set(SHOPS)


def test_henribloem_product():
    p = henribloem.parse_product(TOSO, _page("henribloem-pascual-toso-malbec-2024"))
    assert (p.title, p.colour, p.volume_cl, p.price, p.was_price, p.in_stock_online) == (
        "Pascual Toso Malbec", "red", 75.0, 7.95, None, True)
    assert p.specs["vintage"] == "2024" and p.specs["producer"] == "Pascual Toso"
    assert p.specs["country"] == "Argentinië" and p.specs["region"] == "Mendoza"
    # 6+ bottles cost 7.50 each: a deal on an order of six
    assert p.promotion == "6 flessen voor €45,00"
    deal = parse_deal(p.promotion, p.price)
    assert deal.kind == "bundle_price" and round(deal.factor * 7.95, 2) == 7.5
    assert henribloem.is_wine_product(p)
    q = henribloem.parse_product(SEKT, _page("henribloem-meulenhof-riesling-sekt-nm"))
    assert (q.title, q.colour, q.in_stock_online, q.category) == ("Meulenhof Riesling Sekt", "white", False,
                                                                  "mousserend")
    assert "vintage" not in q.specs


def test_henribloem_sitemap_and_not_wine():
    xml = ("<urlset><url><loc>https://henribloem.nl/product/pascual-toso-malbec-2024/</loc>"
           "<image:loc>https://henribloem.nl/wp-content/uploads/x.jpg</image:loc></url>"
           "<url><loc>https://henribloem.nl/rode-wijn/</loc></url></urlset>")
    assert henribloem.product_urls(xml) == [TOSO]
    pakket = GallProduct(url="u", product_id="x", title="Domaine La Colombette pakket", colour="red", price=39.0,
                         category="proefpakket-maand")
    assert not henribloem.is_wine_product(pakket)


def test_okhuysen_product():
    p = okhuysen.parse_product(FIFTY, _page("okhuysen-94428"))
    assert (p.product_id, p.title, p.colour, p.volume_cl) == (
        "94428", "Avignonesi & Capannelle 50&50 - Rosso di Toscana", "red", 75.0)
    assert (p.price, p.was_price) == (109.0, 142.5)
    assert p.in_stock_online is False and p.specs["presale"] == "yes"  # delivered in spring 2027
    assert p.specs["vintage"] == "2022" and p.specs["country"] == "Italië" and p.specs["region"] == "Toscane"
    assert p.expert_awards == [("James Suckling", "94 punten")]
    a = parse_award_text(*p.expert_awards[0])
    assert a["competition"][0] == "Suckling" and a["points"] == 94
    q = okhuysen.parse_product(LUGANA, _page("okhuysen-93901"))
    assert (q.title, q.colour, q.price, q.was_price, q.in_stock_online) == (
        "Palazzo Maffei Lugana", "white", 16.45, 18.25, True)
    assert q.specs["vintage"] == "2025" and okhuysen.is_wine_product(q)


def test_okhuysen_ratings():
    assert okhuysen._awards(["Jamessuckling.com : 94", "Robert Parker : 93-95", "Jancis Robinson : 17/20",
                             "geen score"]) == [
        ("James Suckling", "94 punten"), ("Robert Parker", "93 punten"), ("Jancis Robinson", "17/20")]


class FakeOkhuysen:
    delay = 0

    def get(self, url):
        if url == okhuysen.SITEMAP:
            return (f"<urlset><url><loc>{FIFTY}</loc></url><url><loc>{LUGANA}</loc></url>"
                    "<url><loc>https://okhuysen.nl/rode-wijn</loc></url>"
                    "<url><loc>https://okhuysen.nl/product/wijnazijn-van-de-rode-wijn-90001</loc></url></urlset>")
        if url.endswith("94428"):
            return _page("okhuysen-94428")
        if url.endswith("93901"):
            return _page("okhuysen-93901")
        return '<html><h1>Wijnazijn van de rode wijn</h1><p class="h4">Kleur / Soort</p><p>Azijn</p></html>'


def test_collect_okhuysen_and_award_on_offer(tmp_path):
    con = db.connect(tmp_path / "t.db")
    assert collect_shop(okhuysen, con=con, client=FakeOkhuysen()) == {"ok": 3, "failed": 0}
    assert con.execute("SELECT COUNT(*) FROM offer").fetchone()[0] == 2
    assert con.execute("SELECT COUNT(*) FROM skipped_page").fetchone()[0] == 1
    row = con.execute("""SELECT c.short, a.points, w.vintage FROM award a JOIN competition c ON c.id = a.competition_id
                         JOIN award_match m ON m.award_id = a.id JOIN wine w ON w.id = m.wine_id""").fetchone()
    assert (row["short"], row["points"], row["vintage"]) == ("Suckling", 94, 2022)


def test_same_wine_at_gall_and_henribloem(tmp_path):
    con = db.connect(tmp_path / "t.db")
    hb_id = con.execute("SELECT id FROM shop WHERE name='Henri Bloem'").fetchone()[0]
    g = GallProduct(url="https://www.gall.nl/pascual-toso-malbec-rood-75cl-100600.html", product_id="100600",
                    title="Pascual Toso Malbec", colour="red", volume_cl=75, price=8.49, in_stock_online=True,
                    specs={"vintage": "2024", "producer": "Pascual Toso"})
    save_product(con, 1, g, TODAY)
    p = henribloem.parse_product(TOSO, _page("henribloem-pascual-toso-malbec-2024"))
    save_product(con, hb_id, p, TODAY, Matcher(con, other_than_shop=hb_id))
    assert con.execute("SELECT COUNT(DISTINCT wine_id) FROM offer").fetchone()[0] == 1
