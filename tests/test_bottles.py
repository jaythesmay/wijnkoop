"""Bottle sizes: a half bottle or a magnum is compared per 75 cl, and shows its real size and price."""
import asyncio
import datetime as dt
from pathlib import Path

import pytest

from wijnkoop import db, scores
from wijnkoop.bottles import bottle_size, fix_volume, volume_from_text
from wijnkoop.collect import save_product
from wijnkoop.shops.gall import GallProduct

ROOT = Path(__file__).parent.parent
TODAY = dt.date(2026, 11, 16)


@pytest.mark.parametrize("text, cl", [
    ("Taylor's 10 Years Old Tawny Port 37,5cl", 37.5),
    ("https://www.gall.nl/taylors-tawny-port-37-5cl-123.html", 37.5),
    ("Sauternes 375 ml", 37.5),
    ("Chablis halve fles", 37.5),
    ("https://www.henribloem.nl/wijn/chateau-x-2018-0-375l/", 37.5),
    ("Bollinger Special Cuvée Magnum", 150.0),
    ("Rioja Reserva 1,5 L", 150.0),
    ("https://shop.nl/bourgogne-2022-1-5-liter/", 150.0),
    ("Dubbele Magnum Côtes du Rhône", 300.0),
    ("Moët & Chandon Impérial Brut", None),  # a normal bottle
    ("Rioja Crianza 6x75cl", None),  # a pack, not a size
    ("https://www.vino.com/nl/dettaglio/barolo-6x075-l.html", None),
    ("Rioja Crianza 2021", None),
])
def test_size_from_name_or_address(text, cl):
    assert volume_from_text(text) == cl


def test_the_shop_size_wins_unless_it_is_the_assumed_75():
    assert fix_volume(75.0, "Tawny Port 37,5cl") == 37.5
    assert fix_volume(None, "Champagne Magnum") == 150.0
    assert fix_volume(50.0, "Tokaji Magnum") == 50.0  # the shop said 50 cl
    assert fix_volume(75.0, "Rioja Reserva") == 75.0


def test_size_label():
    assert bottle_size(75) == "" and bottle_size(None) == ""
    assert bottle_size(37.5) == "half bottle of 37.5 cl"
    assert bottle_size(150) == "magnum of 1.5 l"
    assert bottle_size(50) == "bottle of 50 cl"


def _wine(i, title, price, volume=75.0, awards=(("Mundus Vini 2025", "Goud"),)):
    return GallProduct(url=f"https://www.gall.nl/w-{i}.html", product_id=str(i), title=title, colour="red",
                       volume_cl=volume, price=price, promotion=None, in_stock_online=True, category="Port",
                       expert_awards=list(awards),
                       specs={"vintage": None, "country": "Portugal", "producer": title.split()[0],
                              "grapes": "Touriga Nacional"})


@pytest.fixture
def con(tmp_path):
    con = db.connect(tmp_path / "wijnkoop.db")
    for p in [_wine(1, "Taylors Tawny Port 10 Years 37,5cl", 10.0),
              _wine(2, "Grahams Tawny Port 10 Years", 18.0),
              _wine(3, "Niepoort Ruby Port Magnum", 30.0)]:
        save_product(con, 1, p, TODAY.isoformat())
    scores.recompute(con, TODAY)
    yield con
    con.close()


def _row(con, name):
    return con.execute("SELECT * FROM best_buys WHERE wine LIKE ?", (f"%{name}%",)).fetchone()


def test_prices_are_per_75cl(con):
    half, normal, magnum = _row(con, "Taylors"), _row(con, "Grahams"), _row(con, "Niepoort")
    assert half["volume_cl"] == 37.5 and magnum["volume_cl"] == 150.0
    assert half["price_per_bottle_at_6"] == 20.0 and half["price_per_bottle_as_sold"] == 10.0
    assert half["normal_price"] == 20.0
    assert magnum["price_per_bottle_at_6"] == 15.0 and magnum["price_per_bottle_as_sold"] == 30.0
    assert normal["price_per_bottle_at_6"] == normal["price_per_bottle_as_sold"] == 18.0
    # the same award: the half bottle (20 per 75 cl) is no longer the best buy, the magnum is
    assert magnum["price_as_93"] < normal["price_as_93"] < half["price_as_93"]
    # an order of 6 is 6 bottles as sold
    assert con.execute("SELECT order_of_6 FROM offer_score os JOIN offer o ON o.id = os.offer_id "
                       "WHERE o.title LIKE 'Taylors%'").fetchone()[0] >= 60.0


def test_a_half_bottle_stored_as_75cl_is_corrected(con):
    con.execute("UPDATE offer SET volume_cl = 75, price_per_75cl = price, effective_price_per_75cl = price "
                "WHERE title LIKE 'Taylors%'")
    con.execute("UPDATE price_history SET effective_price_per_75cl = 10 WHERE offer_id = "
                "(SELECT id FROM offer WHERE title LIKE 'Taylors%')")
    con.commit()
    scores.recompute(con, TODAY)
    o = con.execute("SELECT * FROM offer WHERE title LIKE 'Taylors%'").fetchone()
    assert (o["volume_cl"], o["price_per_75cl"], o["effective_price_per_75cl"]) == (37.5, 20.0, 20.0)
    assert {r[0] for r in con.execute("SELECT effective_price_per_75cl FROM price_history WHERE offer_id = ?",
                                      (o["id"],))} == {20.0}
    assert _row(con, "Taylors")["price_per_bottle_at_6"] == 20.0


def test_pages_show_the_real_size(con, tmp_path):
    from datasette.app import Datasette

    wine_id = _row(con, "Taylors")["wine_id"]
    con.close()
    ds = Datasette([str(tmp_path / "wijnkoop.db")], plugins_dir=str(ROOT / "datasette" / "plugins"),
                   template_dir=str(ROOT / "datasette" / "templates"))

    async def run():
        return await ds.client.get(f"/wine/{wine_id}"), await ds.client.get("/")

    wine, home = asyncio.run(run())
    assert wine.status_code == 200
    assert "per 75 cl at 6" in wine.text and "Half bottle of 37.5 cl: €10.00 per bottle" in wine.text
    assert "€20.00" in wine.text
    assert home.status_code == 200
