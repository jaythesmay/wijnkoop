import gzip
from pathlib import Path

from wijnkoop.reference import parse_award_text
from wijnkoop.shops import SHOPS, grandcru, grapedistrict

REAL = Path(__file__).parent / "fixtures" / "real"
CARMES = "https://www.grandcruwijnen.nl/2018-chateau-les-carmes-haut-brion"
FREREJEAN = "https://www.grandcruwijnen.nl/champagne-frerejean-freres-brut-grande-reserve"
TINTO = "https://www.grapedistrict.nl/tinto-negro-limestone-block-malbec"


def _page(name):
    return (REAL / f"{name}.html").read_text()


def test_registered():
    assert SHOPS["grandcru"] is grandcru and SHOPS["grapedistrict"] is grapedistrict


def test_grandcru_product_with_critic_scores():
    p = grandcru.parse_product(CARMES, _page("grandcru-2018-chateau-les-carmes-haut-brion"))
    assert (p.title, p.colour, p.volume_cl, p.price, p.in_stock_online) == (
        "Château Les Carmes Haut-Brion", "red", 75.0, 164.95, True)
    assert p.specs["vintage"] == "2018" and p.specs["producer"] == "Chateau Les Carmes Haut Brion"
    assert p.specs["region"] == "Bordeaux" and p.specs["drink_window"] == "2026-2058"
    assert p.expert_awards == [("Parker", "97 punten"), ("James Suckling", "97 punten"), ("Vinous", "97 punten")]
    assert [parse_award_text(*a)["competition"][0] for a in p.expert_awards] == ["Parker", "Suckling", "Vinous"]
    assert grandcru.is_wine_product(p)
    q = grandcru.parse_product(FREREJEAN, _page("grandcru-champagne-frerejean"))
    assert (q.colour, q.price, q.specs["region"]) == ("white", 42.95, "Champagne")


def test_grandcru_sitemap_keeps_product_pages():
    xml = ("<urlset><url><loc>https://www.grandcruwijnen.nl/wijnen</loc></url>"
           f"<url><loc>{CARMES}</loc><image:image><image:loc>x.png</image:loc></image:image></url>"
           "<url><loc>https://www.grandcruwijnen.nl/wat-is-een-recoltant-manipulant</loc></url></urlset>")
    assert grandcru.product_urls(xml) == [CARMES]


def test_grapedistrict_product_and_rating():
    p = grapedistrict.parse_product(TINTO, _page("grapedistrict-tinto-negro-limestone-block-malbec"))
    assert (p.title, p.colour, p.price, p.in_stock_online) == (
        "Tinto Negro Limestone Block Malbec", "red", 15.99, False)
    assert p.specs["vintage"] == "2018" and p.specs["country"] == "Argentinië"
    assert p.expert_awards == [("De Grote Hamersma 2019", "91.5 punten")]
    a = parse_award_text(*p.expert_awards[0])
    assert a["competition"][0] == "Hamersma" and a["points"] == 91.5
    assert grapedistrict.rating("17/20 punten - Jancis Robinson") == ("Jancis Robinson", "17/20")
    assert grapedistrict.rating("93/100 - Falstaff") == ("Falstaff", "93 punten")


class FakeClient:
    def get(self, url):
        return "<sitemapindex><sitemap><loc>https://www.grapedistrict.nl/sitemap/x-1.xml.gz</loc></sitemap></sitemapindex>"

    def get_bytes(self, url):
        return gzip.compress((f"<urlset><url><loc>{TINTO}</loc></url>"
                              "<url><loc>https://www.grapedistrict.nl/rood/</loc></url></urlset>").encode())


def test_grapedistrict_gzipped_sitemap():
    assert grapedistrict.list_products(FakeClient()) == [(TINTO, "tinto-negro-limestone-block-malbec")]
