"""Round 4 of the audit: target prices with alerts, your own rating and note, the cellar."""
import asyncio
import datetime as dt
from pathlib import Path

from wijnkoop import db, mail, personal, scores
from wijnkoop.collect import save_product
from wijnkoop.shops import gall

ROOT = Path(__file__).parent.parent
TODAY = dt.date(2026, 11, 14)


def _setup(tmp_path, price=12.0):
    con = db.connect(tmp_path / "wijnkoop.db")
    p = gall.GallProduct(url="https://www.gall.nl/barolo-1.html", product_id="1", title="Barolo Molino Rood",
                         colour="red", volume_cl=75, price=price, in_stock_online=True, specs={"vintage": "2021"})
    save_product(con, 1, p, TODAY.isoformat())
    scores.recompute(con, TODAY)
    return con, con.execute("SELECT id FROM wine").fetchone()[0]


def _price(con, value):
    con.execute("UPDATE offer_score SET bottle_price_at_6 = ?", (value,))
    con.commit()


def test_target_price_alerts_once_and_again_after_a_rise(tmp_path):
    con, wine = _setup(tmp_path)
    personal.set_target(con, wine, "10,50")
    assert con.execute("SELECT target_price FROM watch WHERE wine_id=?", (wine,)).fetchone()[0] == 10.5
    assert personal.check_alerts(con, TODAY) == []  # 12 is above the target
    _price(con, 10.0)
    first = personal.check_alerts(con, TODAY)
    assert [(a["price"], a["target"]) for a in first] == [(10.0, 10.5)]
    assert personal.check_alerts(con, TODAY) == []  # told already at this price
    _price(con, 9.0)
    assert len(personal.check_alerts(con, TODAY)) == 1  # cheaper still
    _price(con, 11.0)
    personal.check_alerts(con, TODAY)
    _price(con, 9.0)
    assert len(personal.check_alerts(con, TODAY)) == 1  # above the target in between: alerts again
    assert len(personal.recent_alerts(con, 7, TODAY)) == 3


def test_push_goes_to_ntfy(tmp_path):
    sent = []
    alerts = [{"wine": "Barolo 2021", "price": 9.0, "target": 10.0, "shop": "Gall & Gall", "url": "https://x"}]
    assert personal.push(alerts, "https://ntfy.sh/topic", post=lambda url, **kw: sent.append((url, kw))) == 1
    assert sent[0][0] == "https://ntfy.sh/topic" and "Barolo 2021" in sent[0][1]["content"].decode()
    assert personal.push(alerts, url="") == 0


def test_alerts_in_the_mail():
    text, html = mail.render([], TODAY, alerts=[{"wine": "Barolo 2021", "price": 9.0, "target": 10.0,
                                                 "shop": "Gall & Gall", "url": "https://x"}])
    assert "Your target prices reached" in text and "Barolo 2021" in html


def test_rating_note_and_cellar(tmp_path):
    con, wine = _setup(tmp_path)
    assert personal.set_mine(con, wine, "4", " lovely with lamb ") == {"rating": 4, "note": "lovely with lamb"}
    assert personal.mine(con, wine)["rating"] == 4
    personal.set_mine(con, wine, "", "")
    assert personal.mine(con, wine) == {"rating": None, "note": None}
    cid = personal.add_to_cellar(con, wine, "6", "11,95", "2027", today=TODAY)
    c = personal.cellar(con, TODAY)
    assert c["bottles"] == 6 and c["value"] == 71.7 and c["items"][0]["drink_soon"]
    personal.set_bottles(con, cid, "0")
    assert personal.cellar(con, TODAY)["items"] == []


def test_pages(tmp_path):
    from datasette.app import Datasette

    con, wine = _setup(tmp_path)
    personal.set_target(con, wine, "10")
    personal.add_to_cellar(con, wine, 3, 12, 2030, today=TODAY)
    ds = Datasette([str(tmp_path / "wijnkoop.db")], plugins_dir=str(ROOT / "datasette" / "plugins"),
                   template_dir=str(ROOT / "datasette" / "templates"))

    async def run():
        return {p: await ds.client.get(p) for p in ["/cellar", f"/wine/{wine}", "/watchlist"]}

    pages = asyncio.run(run())
    assert all(r.status_code == 200 for r in pages.values())
    assert "Barolo Molino" in pages["/cellar"].text and "3 bottles" in pages["/cellar"].text
    assert "Add to cellar" in pages[f"/wine/{wine}"].text and "3 in your cellar" in pages[f"/wine/{wine}"].text
    assert "Target prices" in pages["/watchlist"].text
