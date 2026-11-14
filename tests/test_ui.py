import asyncio
import datetime as dt
import math
from pathlib import Path

import pytest

from wijnkoop import db, scores, ui
from wijnkoop.collect import save_product
from wijnkoop.deals import line_cost
from wijnkoop.shops.gall import GallProduct

ROOT = Path(__file__).parent.parent
TODAY = dt.date(2026, 11, 16)


def test_fit_price_default_and_her_example():
    c, d, n = scores.fit_price([(10, 93)])  # too few wines: starting rule
    assert n == 0 and round(math.exp(d) - 1, 2) == 0.35
    assert round(scores.fair_price(89, c, d), 2) == 12.0
    # "a 93 for 10 euro is maybe equal to a 94 at 12": with 35% per point the 94 is the better buy
    r93 = scores.fair_price(93, c, d) / 10
    r94 = scores.fair_price(94, c, d) / 12
    assert r94 > r93 and round(r94 / r93, 3) == 1.125


def test_fit_price_learns_and_is_clamped():
    wines = [(8 * 1.2 ** (s - 88), s) for s in [85, 86, 87, 88, 89, 90, 91, 92, 93, 94] * 3]
    c, d, n = scores.fit_price(wines)
    assert n == 30 and round(math.exp(d) - 1, 2) == 0.20
    steep = [(5 * 2.0 ** (s - 88), s) for s in [85, 86, 87, 88, 89, 90, 91, 92, 93, 94] * 3]
    assert round(math.exp(scores.fit_price(steep)[1]) - 1, 2) == 0.50


def test_verdicts():
    assert scores.verdict(0.95) == "Top buy"
    assert scores.verdict(0.75) == "Good buy"
    assert scores.verdict(0.5) == "Fair"
    assert scores.verdict(0.1) == "Pricey for its score"
    assert scores.verdict(None) is None


def test_price_as_93_her_example():
    # "a 93 for 10 euro is maybe equal to a 94 at 12": with 20% per point they are exactly equal
    assert round(scores.price_as(12, 94, 0.20), 2) == 10.0
    assert round(scores.price_as(8, 91, 0.20), 2) == 11.52
    assert scores.price_as(10, 93, 0.35) == 10


def test_nothing_better_for_less():
    items = [("a", 8, 90), ("b", 10, 93), ("c", 10, 91), ("d", 12, 92), ("e", 15, 95), ("f", 7, 88), ("g", 15, 95)]
    assert scores.nothing_better_for_less(items) == {"a", "b", "e", "f", "g"}


def test_line_cost():
    assert line_cost("2e fles 50% korting", 10, 3) == 25
    assert line_cost("6 halen 5 betalen", 10, 6) == 50
    assert line_cost("1+1 gratis", 10, 3) == 20
    assert line_cost("3 voor 25", 10, 4) == 35
    assert line_cost("10% korting", 10, 2) == 18
    assert line_cost(None, 10, 2) == 20


def _wine(i, title, price, colour="red", country="Spanje", awards=(), promotion=None, grapes="Tempranillo"):
    return GallProduct(url=f"https://www.gall.nl/w-{i}.html", product_id=str(i), title=title, colour=colour,
                       volume_cl=75, price=price, promotion=promotion, in_stock_online=True, category="Rood",
                       expert_awards=list(awards),
                       specs={"vintage": "2021", "country": country, "producer": title.split()[0] + " " + title.split()[1],
                              "grapes": grapes})


@pytest.fixture
def con(tmp_path):
    con = db.connect(tmp_path / "wijnkoop.db")
    for p in [
        _wine(1, "Bodegas Muga Reserva", 10.0, awards=[("IWC 2025", "Gold 95 punten")]),
        _wine(2, "Bodegas Olarra Crianza", 12.0, awards=[("Mundus Vini 2025", "Zilver")],
              promotion="6 halen 5 betalen"),
        _wine(3, "Domaine Laroche Chablis", 16.0, colour="white", country="Frankrijk",
              awards=[("93 punten James Suckling", "")], grapes="Chardonnay"),
        _wine(4, "Cantina Tollo Rosato", 6.0, colour="rosé", country="Italië"),
    ]:
        save_product(con, 1, p, TODAY.isoformat())
    scores.recompute(con, TODAY)
    con.row_factory = None
    return con


def _id(con, name):
    return con.execute("SELECT offer_id, wine_id FROM best_buys WHERE wine LIKE ?", (f"%{name}%",)).fetchone()


def test_scores_and_gold(con):
    rows = {r[0]: r[1:] for r in con.execute(
        "SELECT wine, score, gold_or_better, verdict, fair_price, price_quality FROM best_buys")}
    assert rows["Bodegas Muga Reserva"][1] == "yes"          # IWC gold
    assert rows["Domaine Laroche Chablis"][1] == "yes"       # 93 points from a critic
    assert rows["Bodegas Olarra Crianza"][1] == "no"         # silver
    assert rows["Cantina Tollo Rosato"][0] is None           # no award, no verdict
    vals = {r[0]: r[1:] for r in con.execute(
        "SELECT wine, price_as_93, cheaper_than_pct, verdict, nothing_better_for_less, value FROM best_buys")}
    # Muga 92.8 (IWC gold 95 pulled to 88) for EUR 10: as a 93 it costs 10 * 1.2 ** 0.2
    muga = vals["Bodegas Muga Reserva"]
    assert muga[0] == round(10 * 1.2 ** (93 - rows["Bodegas Muga Reserva"][0]), 2)
    assert muga[1] == 100 and muga[2] == "Top buy" and muga[3] == "yes"
    assert vals["Cantina Tollo Rosato"][0] is None and vals["Cantina Tollo Rosato"][1] is None


def test_home_find_search(con):
    h = ui.home(con)
    assert [c["wine"] for c in h["gold"]][0] == "Bodegas Muga Reserva"
    assert h["gold_count"] == 2 and h["wines"] == 4
    assert "Gold or better" in h["gold"][0]["tags"]
    f = ui.find(con, {"gold": "1", "max_price": "12"})
    assert [c["wine"] for c in f["cards"]] == ["Bodegas Muga Reserva"]
    assert ui.find(con, {"colour": "white"})["total"] == 1
    assert ui.find(con, {})["total"] == 3                    # wines without an award are left out
    assert ui.find(con, {"unscored": "1"})["total"] == 4
    assert ui.find(con, {"grape": "chardonnay"})["cards"][0]["wine"] == "Domaine Laroche Chablis"
    assert ui.options(con)["countries"]["Italië"] == "Italy"
    # misspelt, as typed in a shop
    assert ui.search(con, "muga reserba")[0]["wine"] == "Bodegas Muga Reserva"
    assert ui.search(con, "laroche")[0]["wine"] == "Domaine Laroche Chablis"


def test_wine_page(con):
    _, wine_id = _id(con, "Muga")
    w = ui.wine(con, wine_id)
    assert w["wine"]["name"] == "Bodegas Muga Reserva" and w["wine"]["gold"] == 1
    assert w["awards"][0]["short"] == "IWC" and w["offers"][0]["shop"] == "Gall & Gall"
    assert ui.wine(con, 9999) is None


def test_order_and_watch(con):
    muga_offer, muga_wine = _id(con, "Muga")
    olarra_offer, _ = _id(con, "Olarra")
    assert ui.toggle_watch(con, muga_wine) is True
    assert [c["wine"] for c in ui.watchlist(con)] == ["Bodegas Muga Reserva"]
    assert ui.add_to_order(con, muga_offer, 2) == 2
    ui.set_order(con, olarra_offer, 2)
    o = ui.order(con)
    shop = o["shops"][0]
    assert shop["goods"] == 44.0 and shop["delivery"] == 4.95 and shop["total"] == 48.95
    assert shop["to_free_delivery"] == 11.0 and shop["bottles_short"] == 2
    olarra = [ln for ln in shop["lines"] if ln["offer_id"] == olarra_offer][0]
    assert olarra["promo_needs"] == 6
    ui.set_order(con, olarra_offer, 6)                       # 6 halen 5 betalen: 60 for 72
    shop = ui.order(con)["shops"][0]
    assert shop["goods"] == 80.0 and shop["delivery"] == 0 and shop["saved"] == 12.0
    assert ui.nav(con) == {"order_bottles": 8, "watched": 1}
    ui.set_order(con, muga_offer, 0)
    ui.clear_order(con)
    assert ui.order(con)["shops"] == []
    assert ui.toggle_watch(con, muga_wine) is False


def test_week_keeps_row_factory(con):
    _, muga_wine = _id(con, "Muga")
    ui.toggle_watch(con, muga_wine)
    sections = ui.week(con, TODAY)
    assert con.row_factory is None
    assert sections[0]["title"] == "On your watchlist"
    assert [r["wine"] for r in sections[0]["rows"]] == ["Bodegas Muga Reserva"]
    assert sections[0]["rows"][0]["wine_id"] == muga_wine


def test_pages_render(con, tmp_path):
    from datasette.app import Datasette

    ds = Datasette([str(tmp_path / "wijnkoop.db")], plugins_dir=str(ROOT / "datasette" / "plugins"),
                   template_dir=str(ROOT / "datasette" / "templates"))
    _, wine_id = _id(con, "Muga")

    async def run():
        out = {}
        for path in ["/", "/find?gold=1", "/awards", "/find?award=IWC&award_result=95", "/wijnkoop/best_buys",
                     "/wijnkoop/award_match", "/search?q=muga", f"/wine/{wine_id}", "/week", "/watchlist", "/order",
                     "/how", "/wine/9999"]:
            r = await ds.client.get(path)
            out[path] = (r.status_code, r.text)
        return out

    pages = asyncio.run(run())
    for path, (status, text) in pages.items():
        assert status == (404 if path == "/wine/9999" else 200), path
    assert "Bodegas Muga Reserva" in pages["/"][1] and "Gold or better" in pages["/"][1]
    assert "like a 93 for" in pages["/find?gold=1"][1] and "Nothing better for less" in pages["/find?gold=1"][1]
    assert "Spain" in pages["/"][1] and "Spanje" not in pages["/"][1]
    assert "20% per point" in pages["/how"][1]
    assert "IWC 2025 Gold 95 pts" in pages["/find?award=IWC&award_result=95"][1]
    assert f'<a href="/wine/{wine_id}">{wine_id} · Bodegas Muga Reserva 2021</a>' in pages["/wijnkoop/best_buys"][1]
    assert "Bodegas Muga Reserva 2021</a>" in pages["/wijnkoop/award_match"][1]


def test_award_filters(con):
    comps = {c["short"]: c for c in ui.competitions(con)}
    assert comps["IWC"]["wines"] == 1 and comps["IWC"]["gold_wines"] == 1
    assert comps["Suckling"]["gold_wines"] == 1 and comps["Mundus Vini"]["gold_wines"] == 0
    f = ui.find(con, {"award": "IWC", "award_result": "95"})
    assert [c["wine"] for c in f["cards"]] == ["Bodegas Muga Reserva"]
    assert f["cards"][0]["matched_award"] == "IWC 2025 Gold 95 pts"
    assert ui.find(con, {"award": "IWC", "award_result": "95", "award_year": "2024"})["total"] == 0
    assert {c["wine"] for c in ui.find(con, {"award_result": "gold"})["cards"]} == {
        "Bodegas Muga Reserva", "Domaine Laroche Chablis"}
    assert ui.find(con, {"award": "Mundus Vini"})["cards"][0]["wine"] == "Bodegas Olarra Crianza"
    assert ui.options(con)["award_years"] == [2025]


def test_set_rate_recomputes(con):
    before = con.execute("SELECT price_as_93 FROM best_buys WHERE wine LIKE '%Chablis%'").fetchone()[0]
    assert ui.price_rule(con)["rate_pct"] == 20
    assert ui.set_rate(con, "30") == 30
    after = con.execute("SELECT price_as_93 FROM best_buys WHERE wine LIKE '%Chablis%'").fetchone()[0]
    assert after != before and ui.price_rule(con)["rate"] == "30"
    assert ui.set_rate(con, 99) == 50 and con.row_factory is None


def test_value_chart(con):
    _, wine_id = _id(con, "Muga")
    chart = ui.value_chart(con, "red", wine_id, 20)
    assert chart is None  # fewer than 3 award wines of this colour
    con.execute("UPDATE wine SET colour = 'red'")
    con.commit()
    chart = ui.value_chart(con, "red", wine_id, 20)
    assert chart["n"] == 3 and sum(d["me"] for d in chart["dots"]) == 1 and chart["line"].startswith("M")
    assert chart["ticks_p"] and all(chart["top"] <= t["y"] <= chart["bottom"] for t in chart["ticks_p"])
    assert all(chart["top"] <= d["y"] <= chart["bottom"] for d in chart["dots"])


def test_price_axis_reaches_the_dearest_wine_and_gives_7_to_15_room():
    scale = ui.PriceScale(3.2, 245.0)
    assert scale.lo <= 3.2 and scale.hi >= 245 and scale(scale.lo) == 0 and scale(scale.hi) == 1
    assert 0.45 <= scale(15) - scale(7) <= 0.55  # half the height for €7–15
    assert abs((scale(8) - scale(7)) - (scale(15) - scale(14))) < 1e-9  # every euro the same there
    prices = [3.2, 5, 6.99, 7, 9.5, 15, 15.01, 40, 100, 245]
    assert [scale(p) for p in prices] == sorted(scale(p) for p in prices)
    ticks = scale.ticks(250)
    assert {7, 10, 15, 100, scale.hi} <= set(ticks) and max(ticks) >= 245
    assert all((scale(b) - scale(a)) * 250 >= 13 for a, b in zip(ticks, ticks[1:]))
    # all wines dearer than the focus, or all within it
    assert ui.PriceScale(20, 80).focus is None and ui.PriceScale(20, 80).hi >= 80
    inside = ui.PriceScale(8, 12)
    assert inside.focus == (7, 15) and inside(7) == 0 and inside(15) == 1
