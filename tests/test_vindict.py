import datetime as dt
from pathlib import Path

from wijnkoop import cli, config, db, scores
from wijnkoop.awards.importer import Matcher
from wijnkoop.collect import collect_shop, save_product
from wijnkoop.reference import parse_award_text
from wijnkoop.shops import gall, vindict
from wijnkoop.shops.gall import GallProduct

REAL = Path(__file__).parent / "fixtures" / "real"
CLEMENT = "https://www.vindict.nl/product/1130060111/isabelle-et-pierre-clement-menetou-salon-tradition-rouge-2023"
COUDOULET = "https://www.vindict.nl/product/1158050111/domaine-coudoulet-pays-doc-viognier-de-fontgaline-2025"
TODAY = "2026-11-13"


def _page(name):
    return (REAL / f"vindict-{name}.html").read_text()


def test_parse_product():
    p = vindict.parse_product(CLEMENT, _page("1130060111"))
    assert p.title == "Isabelle et Pierre Clément Menetou-Salon Tradition Rouge"
    assert (p.product_id, p.colour, p.volume_cl, p.price, p.was_price, p.in_stock_online) == (
        "1130060111", "red", 75.0, 21.35, None, True)
    assert p.specs["vintage"] == "2023" and p.specs["producer"] == "Domaine Clément"
    assert p.specs["country"] == "Frankrijk" and p.specs["grapes"] == "100% Pinot Noir"
    # the press rating is for the 2022 vintage
    assert p.expert_awards == [("Perswijn - september 2024", "15.5/20 Goed tot zeer goed Stevig, mooie zuren, fijn pinot",
                                2022)]
    assert vindict.is_wine_product(p)
    banyuls = GallProduct(url="u", product_id="1", title="Cave de l'Abbé Rous Banyuls", colour="red", price=12.95,
                          category="Banyuls", specs={"alcohol": "16,00%"})
    limoncello = GallProduct(url="u", product_id="2", title="Venegazzu Limoncello Negroni", colour=None, price=19.95,
                             category="Likeur")
    assert vindict.is_wine_product(banyuls) and not vindict.is_wine_product(limoncello)
    q = vindict.parse_product(COUDOULET, _page("1158050111"))
    assert (q.price, q.was_price, q.colour) == (9.95, 11.6, "white")
    assert q.expert_awards == [("De Grote Hamersma", "90.6 punten", 2024)]


def test_ratings_out_of_10_and_20():
    assert vindict.ten_to_hundred("9-") == 90.6 and vindict.ten_to_hundred("8") == 88
    assert vindict.ten_to_hundred("10") == 95 and vindict.ten_to_hundred("x") is None
    a = parse_award_text("Perswijn - september 2024", "15.5/20 Goed tot zeer goed")
    assert a["competition"][0] == "Perswijn" and a["points"] == 88.8 and a["year"] == 2024
    assert parse_award_text("De Grote Hamersma", "90.6 punten")["competition"][0] == "Hamersma"


def test_sitemap():
    xml = ("<urlset><url><loc><![CDATA[https://www.vindict.nl/rood]]></loc></url>"
           f"<url><loc>{CLEMENT}</loc></url><url><loc><![CDATA[{COUDOULET}]]></loc></url></urlset>")
    assert vindict.product_urls_from_sitemap(xml) == [CLEMENT, COUDOULET]


def test_same_wine_in_two_shops_and_award_of_other_vintage(tmp_path):
    con = db.connect(tmp_path / "t.db")
    gall_id = con.execute("SELECT id FROM shop WHERE name='Gall & Gall'").fetchone()[0]
    vd_id = con.execute("SELECT id FROM shop WHERE name='Vindict'").fetchone()[0]
    # Gall sells the same wine under a shorter name
    g = GallProduct(url="https://www.gall.nl/clement-menetou-salon-rouge-75cl-100500.html", product_id="100500",
                    title="Clément Menetou-Salon Tradition Rouge", colour="red", volume_cl=75, price=23.99,
                    in_stock_online=True, specs={"vintage": "2023", "producer": "Domaine Clément"})
    save_product(con, gall_id, g, TODAY)
    p = vindict.parse_product(CLEMENT, _page("1130060111"))
    save_product(con, vd_id, p, TODAY, Matcher(con, other_than_shop=vd_id))
    offers = con.execute("SELECT shop_id, wine_id FROM offer").fetchall()
    assert len(offers) == 2 and offers[0]["wine_id"] == offers[1]["wine_id"]
    assert con.execute("SELECT match_score FROM cuvee_alias").fetchone()[0] >= 88
    # The Perswijn rating of 2022 went to a 2022 wine row of the same cuvee, not to the 2023 on sale
    row = con.execute("""SELECT w.vintage, a.vintage AS rated FROM award_match m JOIN award a ON a.id = m.award_id
                         JOIN wine w ON w.id = m.wine_id""").fetchone()
    assert (row["vintage"], row["rated"]) == (2022, 2022)
    scores.recompute(con, dt.date(2026, 11, 13))
    b = con.execute("SELECT shop, score, other_vintages_awarded, deal_reason FROM best_buys ORDER BY shop").fetchall()
    assert [r["shop"] for r in b] == ["Gall & Gall", "Vindict"]
    assert all(r["score"] is None and r["other_vintages_awarded"] == 1 for r in b)
    assert b[1]["deal_reason"] == "cheapest shop"


def test_different_wines_of_one_producer_stay_apart(tmp_path):
    con = db.connect(tmp_path / "t.db")
    vd_id = con.execute("SELECT id FROM shop WHERE name='Vindict'").fetchone()[0]
    g = GallProduct(url="https://www.gall.nl/x-75cl-100501.html", product_id="100501",
                    title="Domaine Coudoulet Picpoul de Pinet", colour="white", volume_cl=75, price=8.99,
                    specs={"vintage": "2025", "producer": "Domaine Coudoulet"})
    save_product(con, 1, g, TODAY)
    p = vindict.parse_product(COUDOULET, _page("1158050111"))
    save_product(con, vd_id, p, TODAY, Matcher(con, other_than_shop=vd_id))
    assert con.execute("SELECT COUNT(DISTINCT wine_id) FROM offer").fetchone()[0] == 2
    assert con.execute("SELECT COUNT(*) FROM cuvee_alias").fetchone()[0] == 0


class FakeVindict:
    delay = 0

    def __init__(self):
        self.pages = []

    def get(self, url):
        if url == vindict.SITEMAP:
            return (f"<urlset><url><loc>{CLEMENT}</loc></url><url><loc>{COUDOULET}</loc></url>"
                    "<url><loc>https://www.vindict.nl/product/176541016/wijnproeverij-amsterdam</loc></url></urlset>")
        self.pages.append(url)
        if "1130060111" in url:
            return _page("1130060111")
        if "1158050111" in url:
            return _page("1158050111")
        return "<html><h1>Wijnproeverij</h1><table><tr><td>Dranksoort</td><td>Proeverij</td></tr></table></html>"


def test_collect_shop_and_schedule(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUN_WEEKDAY", 0)
    monkeypatch.setattr(config, "RUN_HOUR", 1)
    con = db.connect(tmp_path / "t.db")
    friday = dt.datetime(2026, 11, 13, 11, 0)
    assert not cli.run_is_due(con, friday, "vindict")  # fresh install waits
    con.execute("INSERT INTO run_log (job, started, finished) VALUES ('gall', '2026-11-09T01:00:00', '2026-11-09T09:00:00')")
    assert cli.run_is_due(con, friday, "vindict")  # a new shop starts right away
    assert not cli.run_is_due(con, friday, "gall")
    client = FakeVindict()
    assert collect_shop(vindict, con=con, client=client) == {"ok": 3, "failed": 0}
    assert con.execute("SELECT COUNT(*) FROM offer").fetchone()[0] == 2
    assert con.execute("SELECT COUNT(*) FROM skipped_page").fetchone()[0] == 1
    assert not cli.run_is_due(con, friday, "vindict")
    assert con.execute("SELECT job FROM run_log ORDER BY id DESC").fetchone()[0] == "vindict"
