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

    def get(self, url, params=None, headers=None):
        self.calls.append((url, params, headers))
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
    url, params, headers = http.calls[0]
    assert url == vivino.ENDPOINT and params["q"] == "site:vivino.com Catena Zapata Malbec Argentino 2020"
    assert headers["X-Subscription-Token"] == "secret"
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


def test_key_file(tmp_path):
    path = tmp_path / "brave.ini"
    assert vivino.load_key(path) is None
    vivino.save_key(" abc123 ", path)
    assert vivino.load_key(path) == "abc123"
    assert oct(path.stat().st_mode & 0o777) == "0o600"
