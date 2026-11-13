"""Every award can be traced to where it was read, and score ranges count as their lower end."""
import asyncio
import datetime as dt
from pathlib import Path

import pytest

from wijnkoop import db, scores, ui
from wijnkoop.collect import save_product
from wijnkoop.reference import parse_award_text
from wijnkoop.shops.gall import GallProduct

ROOT = Path(__file__).parent.parent
TODAY = dt.date(2026, 11, 13)


@pytest.mark.parametrize("source,result,points", [
    ("Robert Parker - december 2020", "96-100 punten. Krachtig en lang", 96),
    ("Robert Parker", "(93-95)", 93),
    ("Robert Parker : 93–95", "", 93),
    ("Robert Parker", "drinken 2025-2035, 92 punten", 92),
    ("Robert Parker", "100 punten", 100),
    ("James Suckling", "94/100", 94),
])
def test_ranges_count_as_their_lower_end(source, result, points):
    assert parse_award_text(source, result)["points"] == points


def _wine(awards):
    return GallProduct(url="https://www.vindict.nl/wijn/x-1/", product_id="1", title="Chateau X Pomerol",
                       colour="red", volume_cl=75, price=60.0, in_stock_online=True, expert_awards=awards,
                       specs={"vintage": "2020", "producer": "Chateau X"})


def test_old_award_is_corrected_and_traceable(tmp_path):
    con = db.connect(tmp_path / "wijnkoop.db")
    shop_id = con.execute("SELECT id FROM shop WHERE name = 'Vindict'").fetchone()[0]
    save_product(con, shop_id, _wine([("Robert Parker - december 2020", "96-100 punten. Krachtig")]), TODAY.isoformat())
    # as an older version stored it: the top of the range
    con.execute("UPDATE award SET points = 100")
    scores.recompute(con, TODAY)
    assert con.execute("SELECT points FROM award").fetchone()[0] == 96
    wine_id = con.execute("SELECT id FROM wine").fetchone()[0]
    a = ui.wine(con, wine_id)["awards"][0]
    assert a["origin"] == "Vindict page" and a["origin_url"] == "https://www.vindict.nl/wijn/x-1/"
    assert "96-100 punten" in a["raw_text"]
    assert a["found_on"] == TODAY.isoformat()
    con.close()

    from datasette.app import Datasette

    ds = Datasette([str(tmp_path / "wijnkoop.db")], plugins_dir=str(ROOT / "datasette" / "plugins"),
                   template_dir=str(ROOT / "datasette" / "templates"))
    page = asyncio.run(ds.client.get(f"/wine/{wine_id}")).text
    assert '>Vindict page</a>' in page and "96-100 punten" in page and "read 2026-11-13" in page


def test_origin_of_other_sources():
    assert ui.award_origin({"source": "upload", "source_url": "upload:4:mundus.xlsx"}) == {
        "origin": "your file mundus.xlsx", "origin_url": "/upload/4"}
    assert ui.award_origin({"source": "iwc", "source_url": "https://www.internationalwinechallenge.com/x"})[
        "origin"] == "International Wine Challenge results"
