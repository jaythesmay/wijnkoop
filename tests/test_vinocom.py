import datetime as dt
from pathlib import Path

from wijnkoop import db
from wijnkoop.collect import save_product
from wijnkoop.shops import SHOPS, vinocom

FIX = Path(__file__).parent / "fixtures"
URL = "https://www.vino.com/nl/dettaglio/{}.html"


def _parse(slug, fixture):
    return vinocom.parse_product(URL.format(slug), (FIX / fixture).read_text())


def test_red_with_critics_of_another_vintage():
    p = _parse("barbaresco-docg-matthias-ca-viola-2022", "vinocom-barbaresco-ca-viola.html")
    assert p.title == "Ca' Viola Barbaresco DOCG Matthias"
    assert p.product_id == "barbaresco-docg-matthias-ca-viola-2022"
    assert (p.colour, p.volume_cl, p.price, p.was_price, p.in_stock_online) == ("red", 75.0, 26.6, 37.9, True)
    assert p.specs["vintage"] == "2022" and p.specs["producer"] == "Ca' Viola"
    assert p.specs["country"] == "Italië" and p.specs["grapes"] == "Nebbiolo"
    assert p.expert_awards == [("James Suckling", "94 punten", 2021), ("Falstaff", "94 punten", 2021)]
    assert (p.shop_rating, p.shop_rating_count) == (4.81, 32)
    assert vinocom.is_wine_product(p)


def test_sparkling_is_white_and_suggestions_are_left_out():
    p = _parse("valdobbiadene-prosecco-superiore-docg-col-de-mez-brut-soligo", "vinocom-prosecco-soligo.html")
    assert p.title == "Soligo Valdobbiadene Prosecco Superiore DOCG Col de Mez Brut"
    assert p.colour == "white" and "vintage" not in p.specs and p.price == 10.3
    assert p.expert_awards == []  # the scores further down the page belong to suggested wines


def test_rioja_and_cognac():
    p = _parse("rioja-doca-reserva-marques-de-riscal-2021", "vinocom-rioja-riscal.html")
    assert p.title == "Marqués de Riscal Rioja DOCa Reserva" and p.price == 21.3 and p.was_price == 23.7
    assert p.expert_awards == [("James Suckling", "94 punten", 2020)]  # not the Parker score of a suggestion
    c = _parse("cognac-aoc-grande-champagne-vsop-gaston-de-casteljac", "vinocom-cognac.html")
    assert c.volume_cl == 70 and not vinocom.is_wine_product(c)
    assert c.promotion is None  # a quantity price of another product on the page is not this one's


def test_sitemap_leaves_out_spirits_and_cases():
    xml = "".join(f"<url><loc>{URL.format(s)}</loc></url>" for s in [
        "barbaresco-docg-matthias-ca-viola-2022", "cognac-aoc-grande-champagne-vsop-gaston-de-casteljac",
        "black-spiced-rum-kraken", "vollereaux-brut-reserve-france-champagne-6x075-l-153949510000600750",
        "amaro-marzadro", "chablis-aoc-louis-moreau-2025"]) + "<url><loc>https://www.vino.com/nl/vino/rosso</loc></url>"
    assert [u.rsplit("/", 1)[1] for u in vinocom.product_urls(xml)] == [
        "barbaresco-docg-matthias-ca-viola-2022.html", "chablis-aoc-louis-moreau-2025.html"]


def test_quantity_price_and_helpers():
    html = ('watcParams89231.quantityDiscountItemNumber1 = "6";\n'
            'watcParams89231.quantityDiscountSalePriceDouble1 = 24.5;')
    assert vinocom._quantity_price(html, "89231") == (6, 24.5)
    assert vinocom._quantity_price(html, "1") is None
    assert vinocom._euros("1.250,00 €") == 1250.0 and vinocom._euros("37,90 €") == 37.9
    assert vinocom._volume_cl("Magnum 1,5 ℓ") == 150.0 and vinocom._volume_cl("0,375 ℓ") == 37.5


def test_registered_and_saved(tmp_path):
    assert SHOPS["vinocom"] is vinocom
    con = db.connect(tmp_path / "t.db")
    p = _parse("barbaresco-docg-matthias-ca-viola-2022", "vinocom-barbaresco-ca-viola.html")
    save_product(con, 1, p, dt.date(2026, 11, 13).isoformat())
    assert con.execute("SELECT COUNT(*) FROM award_match").fetchone()[0] == 2
