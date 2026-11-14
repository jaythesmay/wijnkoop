"""Data quality checks, and what happens when you choose an action."""
import asyncio
import datetime as dt
import json
from pathlib import Path

import pytest

from wijnkoop import award_wines, db, quality
from wijnkoop.collect import save_product
from wijnkoop.shops.gall import GallProduct

ROOT = Path(__file__).parent.parent
TODAY = "2026-11-14"


@pytest.fixture
def con(tmp_path):
    con = db.connect(tmp_path / "wijnkoop.db")
    for name in ("Gall & Gall", "Vindict", "Wijny"):
        con.execute("INSERT OR IGNORE INTO shop (name, website) VALUES (?, ?)", (name, "https://x"))
    con.commit()
    yield con
    con.close()


def _shop(con, name):
    return con.execute("SELECT id FROM shop WHERE name=?", (name,)).fetchone()[0]


def _sell(con, shop, pid, title, price=10.0, colour="red", producer=None, vintage=None, volume=75.0, grapes=None):
    specs = {k: v for k, v in (("producer", producer), ("vintage", vintage), ("grapes", grapes)) if v}
    p = GallProduct(url=f"https://shop/{shop}/{pid}", product_id=str(pid), title=title, colour=colour,
                    volume_cl=volume, price=price, in_stock_online=True, specs=specs)
    offer_id = save_product(con, _shop(con, shop), p, TODAY)
    con.commit()
    return con.execute("SELECT wine_id FROM offer WHERE id=?", (offer_id,)).fetchone()[0]


def _issues(con, check):
    return [dict(r) for r in con.execute("SELECT * FROM dq_issue WHERE check_code=? AND state='open'", (check,))]


def _award(con, name, producer=None, vintage=None, medal="Gold", year=2025, comp="IWSC", n=1):
    comp_id = con.execute("SELECT id FROM competition WHERE short=?", (comp,)).fetchone()[0]
    return con.execute("""INSERT INTO award (competition_id, year, producer_raw, name_raw, vintage, medal, source,
                                             source_url) VALUES (?,?,?,?,?,?,'upload',?)""",
                       (comp_id, year, producer, name, vintage, medal, f"upload:1:{n}")).lastrowid


def test_duplicate_found_and_merged(con):
    a = _sell(con, "Gall & Gall", 1, "Herdade do Esporao Reserva Tinto", producer="Herdade do Esporao", vintage="2021")
    b = _sell(con, "Vindict", 2, "Esporao Reserva Tinto", producer="Esporao", vintage="2021", price=12)
    con.execute("INSERT INTO watch (wine_id, added) VALUES (?, ?)", (b, TODAY))
    con.commit()
    quality.run_checks(con, look_for_awards=False)
    dup = _issues(con, "duplicate")
    assert len(dup) == 1 and dup[0]["suggestion"] == "merge"
    r = quality.resolve(con, [dup[0]["id"]])  # the suggestion
    assert r == {"done": 1, "skipped": 0, "rescore": True}
    # one wine with both shops' offers, still watched
    wines = {r[0] for r in con.execute("SELECT wine_id FROM offer")}
    assert len(wines) == 1 and con.execute("SELECT COUNT(*) FROM watch").fetchone()[0] == 1
    # the next shop run puts the second shop's product with the same wine
    again = _sell(con, "Vindict", 2, "Esporao Reserva Tinto", producer="Esporao", vintage="2021", price=11)
    assert again in wines
    quality.run_checks(con, look_for_awards=False)
    assert not _issues(con, "duplicate")


def test_keep_apart_is_remembered(con):
    _sell(con, "Gall & Gall", 1, "Quinta do Crasto LBV Port", producer="Quinta do Crasto")
    _sell(con, "Vindict", 2, "Quinta do Crasto LBV Porto", producer="Quinta do Crasto")
    quality.run_checks(con, look_for_awards=False)
    dup = _issues(con, "duplicate")
    assert dup
    quality.resolve(con, [d["id"] for d in dup], "keep")
    quality.run_checks(con, look_for_awards=False)
    assert not _issues(con, "duplicate")
    assert con.execute("SELECT state FROM dq_issue WHERE check_code='duplicate'").fetchone()[0] == "kept"
    assert quality.reopen(con, [dup[0]["id"]]) == 1 and _issues(con, "duplicate")


def test_colour_missing_set_and_held(con):
    w = _sell(con, "Gall & Gall", 1, "Domaine Y Sancerre", colour=None, grapes="Sauvignon Blanc")
    quality.run_checks(con, look_for_awards=False)
    issue = _issues(con, "colour_missing")[0]
    assert issue["suggestion"] == "white"
    quality.resolve(con, [issue["id"]])
    assert con.execute("SELECT colour FROM wine WHERE id=?", (w,)).fetchone()[0] == "white"
    _sell(con, "Gall & Gall", 1, "Domaine Y Sancerre", colour="red")  # the shop says otherwise
    assert con.execute("SELECT colour FROM wine WHERE id=?", (w,)).fetchone()[0] == "white"  # yours holds


def test_colour_conflict(con):
    w = _sell(con, "Gall & Gall", 1, "Cantina Z Bianco di Custoza", colour="red")
    quality.run_checks(con, look_for_awards=False)
    issue = _issues(con, "colour_conflict")[0]
    assert "White" in issue["detail"]
    quality.resolve(con, [issue["id"]], "set_name_colour")
    assert con.execute("SELECT colour FROM wine WHERE id=?", (w,)).fetchone()[0] == "white"


def test_not_a_wine_stays_hidden(con):
    _sell(con, "Gall & Gall", 1, "Proefpakket Italiaanse rode wijnen")
    quality.run_checks(con, look_for_awards=False)
    issue = _issues(con, "not_wine")[0]
    quality.resolve(con, [issue["id"]], None, None)
    assert con.execute("SELECT is_wine FROM cuvee").fetchone()[0] == 0
    _sell(con, "Gall & Gall", 1, "Proefpakket Italiaanse rode wijnen")
    assert con.execute("SELECT is_wine FROM cuvee").fetchone()[0] == 0


def test_price_outlier_bottle_size(con):
    for i, shop in enumerate(("Gall & Gall", "Vindict")):
        _sell(con, shop, 10 + i, "Bodega X Rioja Reserva", price=15 + i, producer="Bodega X")
    _sell(con, "Wijny", 12, "Bodega X Rioja Reserva", price=31, producer="Bodega X")  # a magnum read as 75 cl
    quality.run_checks(con, look_for_awards=False)
    issue = _issues(con, "price_outlier")[0]
    assert issue["suggestion"] == "size_150" and "Wijny" in issue["title"]
    quality.resolve(con, [issue["id"]])
    from wijnkoop.scores import recompute

    recompute(con)
    o = con.execute("SELECT volume_cl, price_per_75cl FROM offer WHERE shop_product_id='12'").fetchone()
    assert o["volume_cl"] == 150 and o["price_per_75cl"] == 15.5
    _sell(con, "Wijny", 12, "Bodega X Rioja Reserva", price=31, producer="Bodega X")
    assert con.execute("SELECT volume_cl FROM offer WHERE shop_product_id='12'").fetchone()[0] == 150


def test_producer_variant(con):
    _sell(con, "Gall & Gall", 1, "Chateau Musar Rouge", producer="Chateau Musar")
    _sell(con, "Vindict", 2, "Musar Jeune Rouge", producer="Chateau Musar Bekaa")
    quality.run_checks(con, look_for_awards=False)
    issue = _issues(con, "producer_variant")[0]
    quality.resolve(con, [issue["id"]], "same_a")
    assert {r[0] for r in con.execute("SELECT DISTINCT producer FROM cuvee")} == {json.loads(issue["data"])["a"][1]}


def test_probable_award_and_split_awards(con):
    w = _sell(con, "Gall & Gall", 1, "Bodega X Rioja Reserva", producer="Bodega X", vintage="2019")
    gold = _award(con, "Rioja Reserva", "Bodega X", 2019, "Gold", n=1)
    bronze = _award(con, "Rioja Reserva Especial", "Bodega X", 2019, "Bronze", n=2)
    con.execute("INSERT INTO award_match (award_id, wine_id, match_score, confirmed) VALUES (?, ?, 1, 1)", (gold, w))
    con.execute("INSERT INTO award_match (award_id, wine_id, match_score, confirmed) VALUES (?, ?, .8, NULL)", (bronze, w))
    con.commit()
    quality.run_checks(con, look_for_awards=False)
    split = _issues(con, "split_awards")[0]
    assert json.loads(split["data"])["best"] == gold
    probable = _issues(con, "probable_award")[0]
    assert probable["suggestion"] == "other"
    quality.resolve(con, [split["id"]])  # keep the best-fitting award
    assert con.execute("SELECT confirmed FROM award_match WHERE award_id=?", (bronze,)).fetchone()[0] == 0
    # the probable link was answered by that too: gone at the next run
    quality.run_checks(con, look_for_awards=False)
    assert not _issues(con, "probable_award")


def test_split_alias(con):
    a = _sell(con, "Gall & Gall", 1, "Tenuta Q Rosso di Montalcino", producer="Tenuta Q")
    con.execute("""INSERT INTO cuvee_alias (norm_key, cuvee_id, name, shop_id, match_score)
                   VALUES ('tenuta q rosso di montalcino vigna alta', (SELECT cuvee_id FROM wine WHERE id=?), ?, ?, 89)""",
                (a, "Tenuta Q Rosso di Montalcino Vigna Alta", _shop(con, "Vindict")))
    con.commit()
    b = _sell(con, "Vindict", 2, "Tenuta Q Rosso di Montalcino Vigna Alta", producer="Tenuta Q")
    assert a == b  # joined
    quality.run_checks(con, look_for_awards=False)
    issue = _issues(con, "split_alias")[0]
    quality.resolve(con, [issue["id"]], "split")
    wine_of = {r[0]: r[1] for r in con.execute("SELECT shop_product_id, wine_id FROM offer")}
    assert wine_of["1"] != wine_of["2"]
    again = _sell(con, "Vindict", 2, "Tenuta Q Rosso di Montalcino Vigna Alta", producer="Tenuta Q")
    assert again == wine_of["2"]  # stays apart


def test_orphans_and_bulk(con):
    for i in range(3):
        con.execute("INSERT INTO wine (norm_key, name) VALUES (?, ?)", (f"left over {i}|nv", f"Left over {i}"))
    con.commit()
    quality.run_checks(con, look_for_awards=False)
    assert len(_issues(con, "orphan")) == 3
    r = quality.resolve(con, [], None, all_open_of="orphan")
    assert r["done"] == 3 and con.execute("SELECT COUNT(*) FROM wine").fetchone()[0] == 0


def test_findings_that_are_gone_close_by_themselves(con):
    con.execute("INSERT INTO wine (norm_key, name) VALUES ('x|nv', 'X')")
    con.commit()
    quality.run_checks(con, look_for_awards=False)
    con.execute("DELETE FROM wine")
    con.commit()
    r = quality.run_checks(con, look_for_awards=False)
    assert r["gone"] == 1 and con.execute("SELECT state FROM dq_issue").fetchone()[0] == "gone"


def test_award_wines_not_sold_and_found_later(con):
    _award(con, "Douro Reserva", "Quinta Nova", 2020, "Gold", n=1)
    _award(con, "Douro Reserva", "Quinta Nova", 2020, "Silver", comp="DWWA", n=2)
    _award(con, "Something Else", "Other House", 2021, "Bronze", n=3)
    r = award_wines.not_sold(con)
    assert r["total"] == 2 and r["wines"][0]["name"] == "Quinta Nova Douro Reserva"
    assert len(r["wines"][0]["awards"]) == 2 and r["wines"][0]["gold"]
    assert award_wines.not_sold(con, gold="1")["total"] == 1
    # a shop starts selling it: the data quality run finds it
    _sell(con, "Gall & Gall", 1, "Quinta Nova Douro Reserva", producer="Quinta Nova", vintage="2020")
    quality.run_checks(con)
    r = award_wines.not_sold(con)
    assert r["total"] == 1 and r["found"] and r["found"][0]["name"] == "Quinta Nova Douro Reserva"


def test_pages(con, tmp_path, monkeypatch):
    from datasette.app import Datasette

    _sell(con, "Gall & Gall", 1, "Proefpakket Italiaanse rode wijnen")
    _award(con, "Douro Reserva", "Quinta Nova", 2020, "Gold", n=1)
    quality.run_checks(con, look_for_awards=False)
    issue = _issues(con, "not_wine")[0]
    monkeypatch.setattr(quality, "recompute_in_background", lambda: None)
    started = []
    monkeypatch.setattr(quality, "start_in_background", lambda: started.append(1))
    ds = Datasette([str(tmp_path / "wijnkoop.db")], plugins_dir=str(ROOT / "datasette" / "plugins"),
                   template_dir=str(ROOT / "datasette" / "templates"))

    async def run():
        page = await ds.client.get("/quality?check=not_wine")
        token = page.cookies.get("ds_csrftoken")
        r = await ds.client.post("/quality", data={"ids": str(issue["id"]), "act": "not_wine", "csrftoken": token},
                                 cookies={"ds_csrftoken": token}, headers={"x-wk-quick": "1"})
        run = await ds.client.post("/quality", data={"run": "1", "csrftoken": token}, cookies={"ds_csrftoken": token})
        sold = await ds.client.get("/awards/not-sold?gold=1")
        return page, r, run, sold

    page, r, run, sold = asyncio.run(run())
    assert page.status_code == 200 and "Proefpakket" in page.text and "Apply to the selected" in page.text
    assert r.json()["done"] == 1
    assert run.status_code == 302 and started == [1]
    assert sold.status_code == 200 and "Quinta Nova Douro Reserva" in sold.text


def test_strip_size():
    assert quality.strip_size("chateau x magnum") == "chateau x"
    assert quality.strip_size("chateau x 37 5 cl") == "chateau x"
    assert quality.strip_size("chateau x 1 5 l") == "chateau x"
    assert quality.strip_size("chateau x halve fles") == "chateau x"
    assert quality.strip_size("chateau x") == "chateau x"


def _cuvees(con):
    return {r[0] for r in con.execute("SELECT DISTINCT w.cuvee_id FROM offer o JOIN wine w ON w.id = o.wine_id")}


def test_size_only_merges_by_itself_and_can_be_undone(con):
    _sell(con, "Gall & Gall", 1, "Chateau Montrose Saint-Estephe", producer="Chateau Montrose", vintage="2019",
          price=12)
    _sell(con, "Vindict", 2, "Chateau Montrose Saint-Estephe Magnum", producer="Chateau Montrose", vintage="2019",
          price=26, volume=150.0)
    assert len(_cuvees(con)) == 2
    r = quality.run_checks(con, look_for_awards=False)
    assert r["size_merged"] == 1
    assert len(_cuvees(con)) == 1  # one wine, a price per bottle size
    issue = dict(con.execute("SELECT * FROM dq_issue WHERE check_code='size_only'").fetchone())
    assert issue["state"] == "done" and issue["auto"] == 1 and issue["resolution"] == "merge"
    assert not _issues(con, "duplicate")  # not also listed as a duplicate
    view = quality.overview(con, "size_only", "done")
    assert view["auto_total"] == 1 and view["issues"][0]["can_undo"]
    # Undo: two wines again, and the next run leaves them apart
    assert quality.undo(con, [issue["id"]]) == 1
    assert len(_cuvees(con)) == 2
    quality.run_checks(con, look_for_awards=False)
    assert len(_cuvees(con)) == 2
    assert con.execute("SELECT state, resolution FROM dq_issue WHERE id=?", (issue["id"],)).fetchone()[:] == \
        ("kept", "undone")


def test_size_only_not_for_boxes_or_other_colours(con):
    _sell(con, "Gall & Gall", 1, "Domaine Y Bourgogne Rouge", producer="Domaine Y")
    _sell(con, "Vindict", 2, "Domaine Y Bourgogne Rouge 6x75cl", producer="Domaine Y", price=60)
    _sell(con, "Gall & Gall", 3, "Domaine Z Cuvee", producer="Domaine Z", colour="red")
    _sell(con, "Vindict", 4, "Domaine Z Cuvee Magnum", producer="Domaine Z", colour="white", volume=150.0)
    quality.run_checks(con, look_for_awards=False)
    assert not con.execute("SELECT 1 FROM dq_issue WHERE check_code='size_only'").fetchone()


def test_issue_price_and_order(con):
    cheap = _sell(con, "Gall & Gall", 1, "Quinta A Tinto", producer="Quinta A", price=5)
    focus = _sell(con, "Gall & Gall", 2, "Quinta B Tinto", producer="Quinta B", price=9.5)
    dear = _sell(con, "Gall & Gall", 3, "Quinta C Tinto", producer="Quinta C", price=40)
    assert quality.issue_price(con, [["w", f"/wine/{focus}"], ["w", f"/wine/{dear}"]]) == 9.5
    assert quality.issue_price(con, [["x", "https://elsewhere"]]) is None
    for i, w in enumerate((dear, cheap, focus)):
        con.execute("""INSERT INTO dq_issue (check_code, key, title, links, data, state, price)
                       VALUES ('colour', ?, ?, ?, '{}', 'open', ?)""",
                    (f"k{i}", f"t{w}", json.dumps([["w", f"/wine/{w}"]]),
                     quality.issue_price(con, [["w", f"/wine/{w}"]])))
    con.commit()
    view = quality.overview(con)
    assert [d["price"] for d in view["issues"]] == [9.5, 5, 40]  # €7-15 first, dear ones last
    assert view["in_focus"] == 1 and view["issues"][0]["in_focus"]
    only = quality.overview(con, focus=True)
    assert [d["price"] for d in only["issues"]] == [9.5]


def test_compare_cuvees(con):
    a = _sell(con, "Gall & Gall", 1, "Herdade do Esporao Reserva Tinto", producer="Herdade do Esporao",
              vintage="2021", price=15)
    b = _sell(con, "Vindict", 2, "Esporao Reserva Tinto", producer="Esporao", vintage="2021", price=28, volume=150.0)
    con.execute("UPDATE offer SET image_url='https://img/1.jpg' WHERE wine_id=?", (a,))
    ca, cb = (con.execute("SELECT cuvee_id FROM wine WHERE id=?", (w,)).fetchone()[0] for w in (a, b))
    c = quality.compare_cuvees(con, ca, cb)
    left, right = c["sides"]
    assert left["image"] == "https://img/1.jpg" and right["image"] is None
    assert left["sizes"] == "75 cl" and right["sizes"] == "150 cl"
    assert right["offers"][0]["price_per_75cl"] == 14
    assert "sizes" in c["diff"] and "producer" in c["diff"] and "vintages" not in c["diff"]
    assert quality.compare_cuvees(con, ca, 99999) is None


def test_image_of():
    from wijnkoop.images import image_of

    url = "https://shop.nl/wijn/1"
    assert image_of('<meta property="og:image" content="/img/a.jpg?x=1&amp;y=2">', url) == \
        "https://shop.nl/img/a.jpg?x=1&y=2"
    ld = ('<script type="application/ld+json">{"@graph":[{"@type":"Product","image":["//cdn.x/b.png"]}]}</script>'
          '<meta property="og:image" content="https://shop.nl/logo.png">')
    assert image_of(ld, url) == "https://cdn.x/b.png"
    assert image_of('{"title":"X","featured_image":"//cdn.shopify.com/c.jpg"}', url) == "https://cdn.shopify.com/c.jpg"
    assert image_of('{"title":"X"}', url) is None
    assert image_of("<html>nothing</html>", url) is None and image_of(None, url) is None


def test_save_product_keeps_image(con):
    p = GallProduct(url="https://shop/1", product_id="1", title="Quinta D Tinto", colour="red", volume_cl=75.0,
                    price=9.0, in_stock_online=True, specs={}, image_url="https://img/d.jpg")
    offer_id = save_product(con, _shop(con, "Gall & Gall"), p, TODAY)
    assert con.execute("SELECT image_url FROM offer WHERE id=?", (offer_id,)).fetchone()[0] == "https://img/d.jpg"


def test_quality_page_compare_and_undo(con, tmp_path, monkeypatch):
    from datasette.app import Datasette

    _sell(con, "Gall & Gall", 1, "Herdade do Esporao Reserva Tinto", producer="Herdade do Esporao", vintage="2021")
    _sell(con, "Vindict", 2, "Esporao Reserva Tinto", producer="Esporao", vintage="2021", price=12)
    _sell(con, "Gall & Gall", 3, "Chateau Montrose Saint-Estephe", producer="Chateau Montrose", price=12)
    _sell(con, "Vindict", 4, "Chateau Montrose Saint-Estephe Magnum", producer="Chateau Montrose", price=26,
          volume=150.0)
    quality.run_checks(con, look_for_awards=False)
    size = con.execute("SELECT id FROM dq_issue WHERE check_code='size_only'").fetchone()[0]
    monkeypatch.setattr(quality, "recompute_in_background", lambda: None)
    ds = Datasette([str(tmp_path / "wijnkoop.db")], plugins_dir=str(ROOT / "datasette" / "plugins"),
                   template_dir=str(ROOT / "datasette" / "templates"))

    async def run():
        page = await ds.client.get("/quality?check=duplicate")
        done = await ds.client.get("/quality?check=size_only&state=done")
        token = page.cookies.get("ds_csrftoken")
        r = await ds.client.post("/quality", data={"undo": "1", "ids": str(size), "csrftoken": token},
                                 cookies={"ds_csrftoken": token}, headers={"x-wk-quick": "1"})
        return page, done, r

    page, done, r = asyncio.run(run())
    assert page.status_code == 200 and "Compare side by side" in page.text and 'data-yes="merge"' in page.text
    assert done.status_code == 200 and "by itself" in done.text and "Undo" in done.text
    assert r.json() == {"undone": 1}
