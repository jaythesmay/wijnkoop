"""The shops Loes also buys from through Vivino: Vanouds de Zwaan, Vinio, Colaris, Wijny, Alex Storms
and Rootring Wijnen, read from saved real pages."""
from pathlib import Path

from wijnkoop.collect import fetch_page
from wijnkoop.reference import awards_in_text, parse_award_text
from wijnkoop.shops import SHOPS, colaris, rootring, shopify, storms, vanoudsdezwaan, vinio, wijny

REAL = Path(__file__).parent / "fixtures" / "real"


def _page(name):
    return (REAL / name).read_text()


def test_registered():
    for m in (vanoudsdezwaan, vinio, colaris, wijny, storms, rootring):
        assert SHOPS[m.KEY] is m


def test_critic_scores_in_text():
    text = "'Amazing depth' Robert Parker’s 95/100.<br> - Vivino 4.2/5<br> - Wine Spectator 95/100 - 100% Sangiovese"
    assert awards_in_text(text) == [("Robert Parker / Wine Advocate", "95 punten"), ("Wine Spectator", "95 punten")]
    assert awards_in_text("Robert Parkers Wine Advocate(91-93)/100Offering up") == [
        ("Robert Parker / Wine Advocate", "91-93/100")]
    assert awards_in_text("93 punten James Suckling") == [("James Suckling", "93 punten")]
    assert awards_in_text("Jancis Robinson 17/20") == [("Jancis Robinson", "17/20")]
    assert awards_in_text("14 maanden op hout, 95 punten") == []  # no critic named
    assert parse_award_text("Robert Parker / Wine Advocate", "91-93/100")["points"] == 91


class Client:
    def __init__(self):
        self.asked = []

    def get(self, url):
        self.asked.append(url)
        return "{}"


def test_shopify_reads_the_small_product_json():
    client = Client()
    fetch_page(vinio, client, "https://www.vinio.nl/products/poggio-antico-altero-brunello-di-montalcino")
    assert client.asked == ["https://www.vinio.nl/products/poggio-antico-altero-brunello-di-montalcino.js"]
    xml = ("<urlset><url><loc>https://www.vinio.nl/</loc></url>"
           "<url><loc>https://www.vinio.nl/products/stefano-antonucci-spumante-brut-charmat</loc></url></urlset>")
    assert shopify.product_urls(xml) == ["https://www.vinio.nl/products/stefano-antonucci-spumante-brut-charmat"]


def test_vinio():
    url = "https://www.vinio.nl/products/poggio-antico-altero-brunello-di-montalcino"
    p = vinio.parse_product(url, _page("vinio-poggio-antico.json"))
    assert (p.title, p.colour, p.volume_cl, p.price, p.in_stock_online) == (
        "Poggio Antico Altero Brunello di Montalcino", "red", 75.0, 79.95, True)
    assert p.specs["vintage"] == "2013" and p.specs["producer"] == "Poggio Antico"
    assert p.specs["country"] == "Italië" and p.specs["region"] == "Toscane"
    assert [a[0] for a in p.expert_awards] == ["Robert Parker / Wine Advocate", "Wine Spectator", "Falstaff"]
    assert vinio.is_wine_product(p)


def test_vanoudsdezwaan():
    p = vanoudsdezwaan.parse_product("https://vanoudsdezwaan.nl/products/malavasi-camilla-2024",
                                     _page("zwaan-malavasi.json"))
    assert (p.title, p.colour, p.price, p.was_price) == ("Malavasi Camilla", "white", 15.40, 17.50)
    assert p.specs == {"vintage": "2024"}  # the vendor is the supplier, not the producer
    m = vanoudsdezwaan.parse_product("https://vanoudsdezwaan.nl/products/x", _page("zwaan-marsuret-magnum.json"))
    assert (m.colour, m.volume_cl) == ("white", 150.0)  # a magnum of prosecco


def test_colaris():
    url = "https://www.colaris.nl/chateau-pontet-canet-pauillac-5e-grand-cru-classe"
    p = colaris.parse_product(url, _page("colaris-pontet-canet.html"))
    assert (p.title, p.colour, p.volume_cl, p.price, p.in_stock_online) == (
        "Château Pontet-Canet Pauillac 5e Grand Cru Classé", "red", 75.0, 140.0, True)
    assert p.specs["vintage"] == "2021" and p.specs["drink_window"] == "2028-2045"
    assert p.expert_awards == [("Robert Parker / Wine Advocate", "91-93/100")]
    assert colaris.is_wine_product(p)
    box = colaris.parse_product("https://www.colaris.nl/mystery-doos-rood-a", _page("colaris-mystery-doos.html"))
    assert not colaris.is_wine_product(box)


def test_wijny():
    p = wijny.parse_product("https://www.wijny.nl/product/chateau-clerc-milon-5eme-cru-classe/",
                            _page("wijny-clerc-milon.html"))
    assert (p.title, p.colour, p.volume_cl, p.price, p.in_stock_online) == (
        "Château Clerc Milon 5éme Cru Classé", "red", 75.0, 129.95, True)
    assert p.specs["vintage"] == "2014" and p.specs["producer"] == "Baron Philippe de Rothschild"
    assert wijny.is_wine_product(p)


def test_storms():
    p = storms.parse_product("https://storms.nl/wijnen/7-coline-saperavi-en-malbec-vinaria-din-vale",
                             _page("storms-7-coline.html"))
    assert (p.title, p.colour, p.volume_cl, p.price, p.in_stock_online) == (
        "7 Coline Saperavi En Malbec Vinaria Din Vale", "red", 75.0, 7.95, True)
    assert p.specs["producer"] == "Vinaria Din Vale" and p.specs["grapes"] == "Saperavi, Malbec"
    assert p.expert_awards == [("Grote Hamersma", "89.8 punten"), ("Onno Kleyn", "90.6 punten")]
    assert [parse_award_text(*a)["competition"][0] for a in p.expert_awards] == ["Hamersma", "Onno Kleyn"]
    q = storms.parse_product("https://storms.nl/wijnen/alex-crianza", _page("storms-alex-crianza.html"))
    assert (q.price, q.specs["vintage"], q.specs["quantity_prices"]) == (6.5, "2021", "48+ 6,30, 144+ 5,80")
    listing = '<a href="/wijnen/alex-crianza">x</a><a href="/wijnen/alex-crianza?v=11498">x</a><a href="/wijnland">'
    assert storms.product_slugs(listing) == ["alex-crianza"]
    xml = ("<urlset><url><loc>https://storms.nl/wijnhuis/domaine-j-laurens</loc></url>"
           "<url><loc>https://storms.nl/wijnstreek/navarra</loc></url></urlset>")
    assert storms.estate_pages(xml) == ["https://storms.nl/wijnhuis/domaine-j-laurens"]


def test_rootring():
    p = rootring.parse_product("https://rootring-wijnen.nl/gaja-barbaresco-2021", _page("rootring-gaja-barbaresco.html"))
    assert (p.title, p.colour, p.price, p.promotion) == ("Gaja Barbaresco", "red", 284.1, "10% korting vanaf 6 flessen")
    assert p.specs["vintage"] == "2021" and p.specs["appellation"] == "Barbaresco DOCG"
    slugs = rootring.product_slugs(_page("rootring-rode-wijn-2.html"))
    assert len(slugs) == 24 and slugs[0] == "fattorie-parri-chianti-montespertoli-2022"
