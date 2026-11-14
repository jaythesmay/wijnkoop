import datetime as dt

import pytest

from wijnkoop import db, ui, vivino
from wijnkoop.collect import save_product
from wijnkoop.shops.gall import GallProduct

TODAY = dt.date(2026, 11, 13)


def brave(results):
    return {"type": "search", "web": {"results": results}}


CATENA = brave([
    {"title": "Catena Zapata Malbec Argentino | Vivino", "url": "https://www.vivino.com/en/catena-zapata-malbec-argentino/w/1869?year=2020",
     "description": "Malbec from Mendoza. Average rating 4.3 based on 2,150 ratings.", "extra_snippets": []},
    {"title": "Catena Malbec | Vivino", "url": "https://www.vivino.com/US/en/catena-mendoza-malbec/w/1873",
     "description": "Rated 3.9 out of 5", "rating": {"ratingValue": 3.9, "bestRating": 5, "reviewCount": 80000}},
    {"title": "Catena Zapata Malbec Argentino 2020 - wine.com", "url": "https://www.wine.com/x", "description": "4.5 stars"},
])


class FakeHttp:
    def __init__(self, payload):
        self.payload, self.calls = payload, []

    def post(self, url, json=None, headers=None):
        self.calls.append((url, json, headers))
        payload = self.payload

        class R:
            def raise_for_status(self):
                pass

            def json(self):
                return payload
        return R()


def test_candidates_read_structured_and_snippet_ratings():
    c = vivino.candidates(CATENA)
    assert [x["title"] for x in c] == ["Catena Zapata Malbec Argentino", "Catena Malbec"]  # wine.com left out
    assert (c[0]["rating"], c[0]["ratings_count"], c[0]["how"]) == (4.3, 2150, "snippet")
    assert (c[1]["rating"], c[1]["ratings_count"], c[1]["how"]) == (3.9, 80000, "structured")
    assert c[0]["url"] == "https://www.vivino.com/en/catena-zapata-malbec-argentino/w/1869"
    match, score = vivino.best_match("Catena Zapata Malbec Argentino", c)
    assert match["rating"] == 4.3 and score >= 90
    assert vivino.best_match("Chateau Margaux", c)[0] is None


def test_look_up_stores_rating_and_counts_searches(tmp_path, monkeypatch):
    con = db.connect(tmp_path / "t.db")
    g = GallProduct(url="https://www.gall.nl/catena-75cl-100700.html", product_id="100700",
                    title="Catena Zapata Malbec Argentino", colour="red", volume_cl=75, price=29.99,
                    in_stock_online=True, specs={"vintage": "2020", "producer": "Catena Zapata"})
    save_product(con, 1, g, TODAY.isoformat())
    wine_id = con.execute("SELECT id FROM wine").fetchone()[0]
    http = FakeHttp(CATENA)
    row = vivino.look_up(con, "secret", wine_id, client=http, today=TODAY)
    assert row["rating"] == 4.3 and row["vivino_title"] == "Catena Zapata Malbec Argentino"
    url, body, headers = http.calls[0]
    assert url == vivino.ENDPOINT == "https://api.tavily.com/search"
    assert body["query"] == "Catena Zapata Malbec Argentino vivino" and body["include_domains"] == ["vivino.com"]
    assert body["search_depth"] == "basic" and not body["include_raw_content"]  # 1 credit, never the page itself
    assert headers["Authorization"] == "Bearer secret"
    assert vivino.searches_this_month(con, TODAY) == 1
    # the wine page shows it, apart from the award score
    data = ui.wine(con, wine_id)
    assert data["vivino"]["rating"]["rating"] == 4.3
    # nothing found: stored as such, so it is not searched again
    vivino.look_up(con, "secret", wine_id, client=FakeHttp(brave([])), today=TODAY)
    assert con.execute("SELECT how, rating FROM vivino_rating").fetchone()[:] == ("no result", None)
    assert ui.wine(con, wine_id)["vivino"]["rating"] is None
    assert ui.wine(con, wine_id)["vivino"]["search"].startswith("https://www.vivino.com/search/wines?q=Catena+Zapata")
    monkeypatch.setattr(vivino, "MONTHLY_LIMIT", 2)
    with pytest.raises(RuntimeError):
        vivino.look_up(con, "secret", wine_id, client=http, today=TODAY)


def tavily(results):
    return {"query": "x", "results": results, "images": [], "response_time": 0.5}


def test_tavily_results():
    result = tavily([
        {"title": "Guidalberto 2021 | Vivino", "url": "https://www.vivino.com/NL/nl/tenuta-san-guido-guidalberto/w/1158063?year=2021",
         "content": "Tenuta San Guido Guidalberto 2021. Rood van Toscane, Italië. 4,2 (3.456 beoordelingen). Prijs € 42,95",
         "score": 0.9},
        {"title": "Tenuta San Guido Guidalberto - Vivino", "url": "https://www.vivino.com/US/en/tenuta-san-guido-guidalberto/w/1158063",
         "content": "Guidalberto · Toscana · 4.1 · 21,034 ratings", "score": 0.8},
        {"title": "Guidalberto | Vivino", "url": "https://www.vivino.com/explore?q=guidalberto", "content": "4.5 stars"},
    ])
    c = vivino.candidates(result)
    assert len(c) == 2  # only wine pages
    assert (c[0]["rating"], c[0]["ratings_count"], c[0]["price"], c[0]["vintage"]) == (4.2, 3456, 42.95, 2021)
    assert (c[1]["rating"], c[1]["ratings_count"], c[1]["vintage"]) == (4.1, 21034, None)
    assert vivino._rating_from_text("Average rating 4.3 based on 2,150 ratings.") == (4.3, 2150)
    assert vivino._rating_from_text("2019 · 1,234 ratings")[0] is None  # a year is not a rating


def test_key_file(tmp_path):
    path = tmp_path / "tavily.ini"
    assert vivino.load_key(path) is None
    vivino.save_key(" abc123 ", path)
    assert vivino.load_key(path) == "abc123"
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_prices_in_euros_only():
    assert vivino._price_from_text("Average rating 4.3 based on 2,150 ratings. Buy for € 29,95 per bottle") == 29.95
    assert vivino._price_from_text("Shipping €4.95. Average price: €18.50") == 18.5  # next to "price" wins
    assert vivino._price_from_text("Prijs 12,99 € bij 6 flessen") == 12.99
    assert vivino._price_from_text("Average price $25.99, 4.1 stars, 20% off") is None
    assert vivino._price_from_text("€1 shipping") is None  # too low to be a bottle
    result = brave([
        {"title": "Catena Zapata Malbec Argentino | Vivino", "url": "https://www.vivino.com/nl/x/w/1869",
         "description": "Rated 4.3 out of 5", "product": {"offers": [{"price": "31.50", "priceCurrency": "EUR"}]}},
        {"title": "Catena Malbec | Vivino", "url": "https://www.vivino.com/US/en/y/w/1873",
         "description": "Rated 3.9 out of 5", "product": {"offers": {"price": "19.99", "priceCurrency": "USD"}}},
    ])
    c = vivino.candidates(result)
    assert [x["price"] for x in c] == [31.5, None]


def test_look_up_stores_price_and_old_tables_get_the_column(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(vivino.SCHEMA.replace(",\n    price REAL", ""))  # a database from before prices
    old.close()
    con = db.connect(path)
    assert "price" in {r[1] for r in con.execute("PRAGMA table_info(vivino_rating)")}
    g = GallProduct(url="https://www.gall.nl/catena-75cl-100700.html", product_id="100700",
                    title="Catena Zapata Malbec Argentino", colour="red", volume_cl=75, price=29.99,
                    in_stock_online=True, specs={"vintage": "2020", "producer": "Catena Zapata"})
    save_product(con, 1, g, TODAY.isoformat())
    wine_id = con.execute("SELECT id FROM wine").fetchone()[0]
    priced = brave([{"title": "Catena Zapata Malbec Argentino | Vivino", "url": "https://www.vivino.com/nl/x/w/1869",
                     "description": "Malbec uit Mendoza. Gemiddelde prijs € 27,50."}])
    row = vivino.look_up(con, "secret", wine_id, client=FakeHttp(priced), today=TODAY)
    assert row["price"] == 27.5 and row["rating"] is None
    assert ui.wine(con, wine_id)["vivino"]["rating"]["price"] == 27.5


def test_searches_leave_out_vintage_and_bottle_size():
    from wijnkoop.vivino import query_for, search_link, search_name

    assert search_name("Taylor's 10 Years Old Tawny Port 37,5cl") == "Taylor's 10 Years Old Tawny Port"
    assert search_name("Catena Zapata Malbec Argentino 2020") == "Catena Zapata Malbec Argentino"
    assert search_name("Bollinger Special Cuvée Magnum 1,5 L") == "Bollinger Special Cuvée"
    assert search_name("Chablis Premier Cru halve fles") == "Chablis Premier Cru"
    assert search_link("Muga Reserva 2019", 2019) == "https://www.vivino.com/search/wines?q=Muga+Reserva"
    assert query_for("Muga Reserva", 2019) == "Muga Reserva vivino"


def _shop(con, i, title, price=12.0):
    g = GallProduct(url=f"https://www.gall.nl/w-{i}.html", product_id=str(i), title=title, colour="red", volume_cl=75,
                    price=price, in_stock_online=True, specs={"vintage": "2020", "producer": title.split()[0]})
    save_product(con, 1, g, TODAY.isoformat())
    con.commit()
    return con.execute("SELECT wine_id FROM offer WHERE shop_product_id=?", (str(i),)).fetchone()[0]


def importer_wine(con, cuvee_id, vintage):
    from wijnkoop.awards import importer

    return importer.Matcher(con).wine_for(cuvee_id, vintage)


def test_weekly_look_ups_and_light_score(tmp_path, monkeypatch):
    from wijnkoop import scores

    con = db.connect(tmp_path / "t.db")
    catena = _shop(con, 1, "Catena Zapata Malbec Argentino", 29.99)
    other = _shop(con, 2, "Trivento Malbec")
    watched = _shop(con, 3, "Muga Reserva")
    con.execute("INSERT INTO watch (wine_id) VALUES (?)", (watched,))
    scores.recompute(con, TODAY)
    # the watchlist first, then wines without awards
    assert vivino.wines_to_look_up(con, 10, TODAY)[0] == watched
    monkeypatch.setattr(vivino, "load_key", lambda path=None: "secret")
    assert vivino.weekly_due(con, dt.datetime(2026, 11, 13, 9))
    monkeypatch.setattr(vivino, "WEEKLY", 2)
    r = vivino.run_weekly(con, client=FakeHttp(CATENA), today=TODAY)
    assert r["looked_up"] == 2 and vivino.searches_this_month(con, TODAY) == 2
    assert not vivino.weekly_due(con, dt.datetime.now())
    vivino.run_weekly(con, client=FakeHttp(CATENA), today=TODAY)  # the rest; checked ones are not searched again
    assert vivino.searches_this_month(con, TODAY) == 3

    def overall():
        scores.recompute(con, TODAY)
        return con.execute("SELECT overall, gold, best_award FROM wine_score WHERE wine_id=?", (catena,)).fetchone()

    # 4.3 counts as 93; 2,150 ratings for this vintage: as much as a big critic
    row = con.execute("SELECT rating_vintage, ratings_count FROM vivino_rating WHERE wine_id=?", (catena,)).fetchone()
    assert tuple(row) == (2020, 2150)  # ?year=2020 in the result's address: this vintage
    s = overall()
    assert round(s[0], 1) == round((1.0 * 93 + 0.5 * 88) / 1.5, 1) and s[1] == 0 and s[2].startswith("Vivino 4.3")
    data = ui.wine(con, catena)["vivino"]
    assert data["counts"] and data["points"] == 93 and data["weight_pct"] == 100
    # a rating of the wine as a whole (all vintages), with many ratings: a bit less
    con.execute("UPDATE vivino_rating SET rating_vintage = NULL WHERE wine_id=?", (catena,))
    assert round(overall()[0], 1) == round((0.6 * 93 + 0.5 * 88) / 1.1, 1)
    # few ratings for this vintage, many for another vintage of the same wine: also a bit less
    con.execute("UPDATE vivino_rating SET rating_vintage = 2020, ratings_count = 40 WHERE wine_id=?", (catena,))
    cuvee = con.execute("SELECT cuvee_id FROM wine WHERE id=?", (catena,)).fetchone()[0]
    older = importer_wine(con, cuvee, 2018)
    con.execute("INSERT INTO vivino_rating (wine_id, rating, ratings_count, rating_vintage, checked_on) "
                "VALUES (?, 4.1, 3000, 2018, ?)", (older, TODAY.isoformat()))
    assert round(overall()[0], 1) == round((0.6 * 93 + 0.5 * 88) / 1.1, 1)
    # few everywhere: very lightly; fewer than 10: not at all
    con.execute("UPDATE vivino_rating SET ratings_count = 30 WHERE wine_id=?", (older,))
    assert round(overall()[0], 1) == round((0.15 * 93 + 0.5 * 88) / 0.65, 1)
    assert "only 40 ratings" in ui.wine(con, catena)["vivino"]["why"]
    con.execute("UPDATE vivino_rating SET ratings_count = 5")
    assert overall()[0] is None and not ui.wine(con, catena)["vivino"]["counts"]
    assert other in [r[0] for r in con.execute("SELECT wine_id FROM vivino_rating")]


def test_a_refused_key_stops_the_week(tmp_path):
    import httpx

    con = db.connect(tmp_path / "t.db")
    for i in range(3):
        _shop(con, i, f"Wine{i} Malbec")

    class Refused:
        calls = 0

        def post(self, url, json=None, headers=None):
            Refused.calls += 1
            req = httpx.Request("GET", url)
            raise httpx.HTTPStatusError("429", request=req, response=httpx.Response(429, request=req))

    r = vivino.run_weekly(con, key="secret", client=Refused(), today=TODAY)
    assert (r["looked_up"], r["failed"], Refused.calls) == (0, 1, 1)
    assert "429" in con.execute("SELECT note FROM run_log WHERE job='vivino'").fetchone()[0]


def test_wine_page_says_how_vivino_counts(tmp_path):
    import asyncio
    from pathlib import Path

    from datasette.app import Datasette
    from wijnkoop import scores

    con = db.connect(tmp_path / "wijnkoop.db")
    wine_id = _shop(con, 1, "Catena Zapata Malbec Argentino", 29.99)
    vivino.look_up(con, "secret", wine_id, client=FakeHttp(CATENA), today=TODAY)
    con.execute("UPDATE vivino_rating SET price = 35.0")
    scores.recompute(con, TODAY)
    con.close()
    root = Path(__file__).parent.parent
    ds = Datasette([str(tmp_path / "wijnkoop.db")], plugins_dir=str(root / "datasette" / "plugins"),
                   template_dir=str(root / "datasette" / "templates"))
    page = asyncio.run(ds.client.get(f"/wine/{wine_id}"))
    assert page.status_code == 200
    assert "counts as 93 points with 100% weight (2,150 ratings for this vintage)" in page.text
    assert "a reference price, not an offer" in page.text and "% cheaper here" in page.text


def _one_wine(tmp_path):
    con = db.connect(tmp_path / "wijnkoop.db")
    g = GallProduct(url="https://www.gall.nl/catena-75cl-100700.html", product_id="100700",
                    title="Catena Zapata Malbec Argentino", colour="red", volume_cl=75, price=29.99,
                    in_stock_online=True, specs={"vintage": "2020", "producer": "Catena Zapata"})
    save_product(con, 1, g, TODAY.isoformat())
    con.commit()
    return con


def test_status_for_the_update_page(tmp_path, monkeypatch):
    con = _one_wine(tmp_path)
    monkeypatch.setattr(vivino, "load_key", lambda path=None: None)
    st = vivino.status(con)
    assert not st["has_key"] and st["next"] is None and st["waiting"] == 1 and st["used"] == 0
    assert st["limit"] == vivino.MONTHLY_LIMIT and not st["running"]

    monkeypatch.setattr(vivino, "load_key", lambda path=None: "secret")
    assert vivino.status(con)["next"] == "as soon as the shop runs are done"
    r = vivino.run_weekly(con, client=FakeHttp(CATENA))
    assert r["looked_up"] == 1 and r["rated"] == 1
    run = con.execute("SELECT * FROM run_log WHERE job='vivino'").fetchone()
    assert run["finished"] and run["process"] is None and run["pages_ok"] == 1
    st = vivino.status(con)
    assert st["rated"] == 1 and st["waiting"] == 0 and st["used"] == 1 and st["last"]["note"].startswith("1 of 1")
    assert st["next"] == (dt.datetime.fromisoformat(run["started"]) + dt.timedelta(days=7)).strftime("%A %d %B")
    assert not vivino.weekly_due(con, dt.datetime.now())

    # a look-up going on now: not started a second time by the scheduler
    from wijnkoop.process import process_token

    con.execute("INSERT INTO run_log (job, started, process, note, pages_ok) VALUES ('vivino', ?, ?, ?, 3)",
                ("2026-11-14T10:00:00", process_token(), "looking up 20 wines"))
    assert vivino.running(con)["pages_ok"] == 3
    assert not vivino.weekly_due(con, dt.datetime(2027, 1, 1)) and vivino.status(con)["next"] is None


def test_update_page_shows_vivino_and_starts_it(tmp_path, monkeypatch):
    import asyncio
    import re
    from pathlib import Path

    from datasette.app import Datasette
    from datasette.plugins import pm

    root = Path(__file__).parent.parent
    con = _one_wine(tmp_path)
    con.close()
    monkeypatch.setattr(vivino, "load_key", lambda path=None: "secret")
    started = []
    monkeypatch.setattr(vivino, "start_in_background", lambda: started.append(1))
    ds = Datasette([str(tmp_path / "wijnkoop.db")], plugins_dir=str(root / "datasette" / "plugins"),
                   template_dir=str(root / "datasette" / "templates"))
    assert next(p for p in pm.get_plugins() if getattr(p, "__name__", "") == "wijnkoop_ui.py")

    async def run():
        page = await ds.client.get("/update")
        token = re.search(r'name="csrftoken" value="([^"]+)"', page.text).group(1)
        post = await ds.client.post("/update", data={"csrftoken": token, "vivino": "1"},
                                    cookies={"ds_csrftoken": page.cookies.get("ds_csrftoken")})
        return page, post, await ds.client.get(post.headers["location"])

    page, post, after = asyncio.run(run())
    assert 'id="vivino"' in page.text and "Look up now" in page.text
    assert f"Searches in {dt.date.today():%B}: <b>0</b> of {vivino.MONTHLY_LIMIT}" in page.text
    assert "1 wine on sale still to look up" in page.text
    assert post.status_code == 302 and post.headers["location"] == "/update?vivino=started#vivino"
    assert started == [1] and "Started: the Vivino ratings are looked up" in after.text
