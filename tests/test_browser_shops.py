import json
from pathlib import Path

from wijnkoop import collect, db
from wijnkoop.collect import collect_shop
from wijnkoop.reference import parse_award_text
from wijnkoop.shops import SHOPS, wijnbeurs, wijnvoordeel

REAL = Path(__file__).parent / "fixtures" / "real"
GAILLARD = "https://www.wijnvoordeel.nl/products/chateau-gaillard-de-la-gorce-saint-emilion-grand-cru"
TINAZZI = "https://www.wijnvoordeel.nl/products/tinazzi-san-giorgio-primitivo-rose"
OLARRA = "https://www.wijnbeurs.nl/products/olarra-clasico-rioja-gran-reserva-kist"


def _page(name):
    return (REAL / f"{name}.html").read_text()


def test_registered_and_no_browser_needed():
    assert SHOPS["wijnvoordeel"] is wijnvoordeel and SHOPS["wijnbeurs"] is wijnbeurs
    assert not wijnvoordeel.NEEDS_BROWSER and not wijnbeurs.NEEDS_BROWSER
    assert wijnbeurs.SITEMAP == "https://www.wijnbeurs.nl/products.xml"


def test_rendered_wijnvoordeel_page():
    p = wijnvoordeel.parse_product(GAILLARD, _page("wijnvoordeel-chateau-gaillard"))
    assert (p.title, p.colour, p.price, p.in_stock_online) == (
        "Château Gaillard de la Gorce Saint Emilion Grand Cru", "red", 9.99, True)
    assert (p.specs["vintage"], p.specs["country"], p.specs["region"], p.specs["box"]) == (
        "2024", "Frankrijk", "Bordeaux", "6")
    assert p.expert_awards == [("concours international lyon goud", "")]
    assert parse_award_text(*p.expert_awards[0])["medal"] == "Gold"
    assert wijnvoordeel.is_wine_product(p)
    q = wijnvoordeel.parse_product(TINAZZI, _page("wijnvoordeel-tinazzi-rose"))
    assert (q.colour, q.price, q.specs["region"]) == ("rosé", 5.99, "Salento")


def test_rendered_wijnbeurs_case_is_skipped_but_medals_read():
    p = wijnbeurs.parse_product(OLARRA, _page("wijnbeurs-olarra-kist"))
    assert p.price == 114.99 and p.specs["vintage"] == "2019"
    medals = [parse_award_text(*a) for a in p.expert_awards]
    assert {(m["competition"][0], m["medal"]) for m in medals if m} >= {("Mundus Vini", "Gold"),
                                                                       ("Berliner Wein Trophy", "Gold")}
    assert not wijnbeurs.is_wine_product(p)  # a wooden case, not one bottle


def test_colour_from_name_and_grapes():
    c = wijnvoordeel.colour_for
    assert c("Hidden Treasures Chardonnay", "Chardonnay") == "white"
    assert c("Bosio Moscato d'Asti", None) == "white"
    assert c("El Real Tinto", None) == "red"
    assert c("Domaine X Côtes de Provence Rosé", "Grenache, Cinsault") == "rosé"
    assert c("Some Blend", "Merlot, Chardonnay") is None
    assert wijnvoordeel.medal_from_image("https://x/media/m/u/mundus-vini-goud_2.png?w=1") == "mundus vini goud"


SERVICE = json.loads((REAL / "wijnvoordeel-service.json").read_text())
CONFIG = {"public": {"default": {"commerce-endpoint": "https://www.wijnvoordeel.nl/api/x/graphql",
                                 "headers": {"all": {"Store": "wijnvoordeel_nl"},
                                             "cs": {"Magento-Store-View-Code": "wijnvoordeel_nl"}}}}}
SHERRY = "https://www.wijnvoordeel.nl/products/emilio-hidalgo-px-sherry"
MOZART = "https://www.wijnvoordeel.nl/products/chateau-des-mille-anges-mozart"
CASE12 = "https://www.wijnvoordeel.nl/products/pierre-baptiste-chardonnay-12-flessen"


class FakeService:
    """The shop as WijnKoop now reads it: sitemap, config.json and the product service."""
    delay = 0

    def __init__(self):
        self.asked, self.headers = [], []

    def get(self, url, headers=None):
        self.asked.append(url)
        self.headers.append(headers)
        if url.endswith("products.xml"):
            return "<urlset>" + "".join(f"<url><loc>{u}</loc></url>" for u in (SHERRY, MOZART, CASE12)) + "</urlset>"
        if url.endswith("config.json"):
            return json.dumps(CONFIG)
        return json.dumps(SERVICE)


def test_product_service_gives_the_whole_shop_in_one_request():
    client = FakeService()
    got = wijnvoordeel.prefetch(client)
    assert set(got) == {"emilio-hidalgo-px-sherry", "chateau-des-mille-anges-mozart", "pierre-baptiste-chardonnay-12-flessen"}
    assert len(client.asked) == 2 and "/api/x/graphql?query=" in client.asked[1]
    assert client.headers[1]["Magento-Store-View-Code"] == "wijnvoordeel_nl" and client.headers[1]["Store"]
    sherry = wijnvoordeel.parse_product(SHERRY, got["emilio-hidalgo-px-sherry"])
    assert (sherry.volume_cl, sherry.specs["alcohol"], sherry.in_stock_online) == (50.0, "15 %", True)
    mozart = wijnvoordeel.parse_product(MOZART, got["chateau-des-mille-anges-mozart"])
    assert mozart.colour == "red" and mozart.price and mozart.specs["country"]
    case = wijnvoordeel.parse_product(CASE12, got["pierre-baptiste-chardonnay-12-flessen"])
    assert case.in_stock_online is False and case.specs.get("presale") == "yes"


def test_collect_reads_these_shops_without_a_browser(tmp_path, monkeypatch):
    fake = FakeService()
    monkeypatch.setattr(collect, "make_client", lambda shop: fake)
    con = db.connect(tmp_path / "t.db")
    result = collect_shop(wijnvoordeel, con=con)
    assert result["failed"] == 0 and len(fake.asked) == 3  # sitemap, config.json, one service request
    assert con.execute("SELECT COUNT(*) FROM offer").fetchone()[0] >= 1


def test_make_client_is_always_plain(monkeypatch):
    from wijnkoop.fetch import PoliteClient

    monkeypatch.setattr(PoliteClient, "__init__", lambda self, base, **kw: None)
    assert isinstance(collect.make_client(wijnbeurs), PoliteClient)
    from wijnkoop.collect import failure_reason

    assert failure_reason(ValueError("could not convert 'abc' at https://x.nl/a")) == \
        "ValueError: could not convert 'abc' at <page>"
