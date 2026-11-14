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


def test_registered_and_need_a_browser():
    assert SHOPS["wijnvoordeel"] is wijnvoordeel and SHOPS["wijnbeurs"] is wijnbeurs
    assert wijnvoordeel.NEEDS_BROWSER and wijnbeurs.NEEDS_BROWSER
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


class FakeBrowser:
    delay = 0
    closed = False

    def get(self, url):
        if url.endswith("products.xml"):
            return f"<urlset><url><loc>{GAILLARD}</loc></url><url><loc>{TINAZZI}</loc></url></urlset>"
        return _page("wijnvoordeel-chateau-gaillard" if "gaillard" in url else "wijnvoordeel-tinazzi-rose")

    def close(self):
        self.closed = True


def test_collect_uses_a_browser_for_these_shops(tmp_path, monkeypatch):
    fake = FakeBrowser()
    monkeypatch.setattr(collect, "make_client", lambda shop: fake)
    con = db.connect(tmp_path / "t.db")
    assert collect_shop(wijnvoordeel, con=con) == {"ok": 2, "failed": 0}
    assert fake.closed
    assert con.execute("SELECT COUNT(*) FROM offer").fetchone()[0] == 2


def test_make_client_picks_the_browser(monkeypatch):
    import wijnkoop.browser as browser

    made = []
    monkeypatch.setattr(browser, "BrowserClient", lambda base, **kw: made.append(base) or "browser")
    assert collect.make_client(wijnbeurs) == "browser" and made == ["https://www.wijnbeurs.nl"]
