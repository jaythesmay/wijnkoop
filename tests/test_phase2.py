import datetime as dt
import sqlite3
from pathlib import Path

import pytest

from wijnkoop import db, deals, reference, scores
from wijnkoop.collect import mark_gone, save_product
from wijnkoop.shops import gall

FIX = Path(__file__).parent / "fixtures"
REAL = FIX / "real"
URLS = {
    "100137": "https://www.gall.nl/veuve-clicquot-la-grande-dame-brut-wit-75cl-100137.html",
    "100242": "https://www.gall.nl/lussory-sparkling-wit-75cl-100242.html",
    "100307": "https://www.gall.nl/taittinger-brut-prestige-wit-75cl-100307.html",
}


def page(pid):
    return gall.parse_product(URLS[pid], (REAL / f"gall-{pid}.html").read_text())


# --- Gall pages as they really look (saved from the NAS) ---------------------------

def test_real_page_grande_dame():
    p = page("100137")
    assert p.title == "Veuve Clicquot La Grande Dame Brut"
    assert p.specs["vintage"] == "2018"
    assert p.price == 210.0
    assert p.member_price == 189.0          # Gall Premium price, not a deal
    assert p.was_price is None
    assert p.promotion is None
    assert p.category == "Mousserend/Champagne"
    assert p.is_wine
    assert p.awards_count == 3
    assert p.expert_awards == [("International Wine and Spirit competition 2024", "Zilver met 90 punten")]


def test_real_page_alcohol_free_is_not_wine():
    p = page("100242")
    assert p.promotion == "2e fles 50% korting"
    assert p.price == 8.99
    assert p.shop_rating == 4.3 and p.shop_rating_count == 7
    assert not p.is_wine


def test_real_page_out_of_stock_no_vintage():
    p = page("100307")
    assert p.in_stock_online is False
    assert "vintage" not in p.specs
    assert p.promotion == "2e fles 50% korting"
    assert p.awards_count == 1


def test_not_wine_words():
    assert not gall.GallProduct(url="", product_id="1", title="Affligem Belgisch Wit Abdijbier 0.0").is_wine
    assert gall.GallProduct(url="", product_id="1", title="La Vieille Ferme Original Rouge").is_wine


# --- Deals --------------------------------------------------------------------------

@pytest.mark.parametrize("text,price,kind,factor", [
    ("2e fles 50% korting", 10, "nth_bottle", 0.75),
    ("3e fles gratis 100% korting", 10, "nth_bottle", round(4 / 6, 4)),
    ("6 halen 5 betalen", 10, "buy_x_pay_y", round(5 / 6, 4)),
    ("3 halen, 2 betalen", 10, "buy_x_pay_y", round(4 / 6, 4)),
    ("2 voor 15", 10, "bundle_price", 0.75),
    ("3 flessen voor € 25,00", 10, "bundle_price", round(50 / 60, 4)),
    ("10% korting", 10, "percent", 0.9),
    ("1+1 gratis", 10, "buy_x_pay_y", 0.5),
    ("2e halve prijs", 10, "nth_bottle", 0.75),
])
def test_parse_deal(text, price, kind, factor):
    d = deals.parse_deal(text, price)
    assert d.kind == kind
    assert d.factor == pytest.approx(factor)


def test_order_of_six():
    assert deals.order_of_six(8.0, 4.95, 55) == (52.95, 4.95)   # 48 euro: below free delivery
    assert deals.order_of_six(10.0, 4.95, 55) == (60.0, 0.0)


# --- Award text ---------------------------------------------------------------------

def test_award_text():
    a = reference.parse_award_text("International Wine and Spirit competition 2024", "Zilver met 90 punten")
    assert a["competition"][0] == "IWSC" and a["year"] == 2024
    assert a["medal"] == "Silver" and a["points"] == 90
    b = reference.parse_award_text("93 punten James Suckling")
    assert b["competition"][0] == "Suckling" and b["points"] == 93 and b["medal"] is None
    c = reference.parse_award_text("Decanter World Wine Awards 2023 Gold")
    assert c["competition"][0] == "DWWA" and c["medal"] == "Gold"
    d = reference.parse_award_text("Jancis Robinson 17/20")
    assert d["points"] == 92.5
    assert reference.parse_award_text("Lekker bij kaas") is None
    assert reference.award_score("Bronze", None) is None
    assert reference.award_score("Gold", None) == 93
    assert reference.award_score("Silver", 90) == 90


# --- Saving, vintages, gone ---------------------------------------------------------

def gall_id(con):
    return con.execute("SELECT id FROM shop WHERE name='Gall & Gall'").fetchone()[0]


def test_save_real_page_with_award(tmp_path):
    con = db.connect(tmp_path / "t.db")
    shop = gall_id(con)
    save_product(con, shop, page("100137"), "2026-11-12")
    save_product(con, shop, page("100137"), "2026-11-12")
    assert con.execute("SELECT COUNT(*) FROM award").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM award_match").fetchone()[0] == 1
    row = con.execute("SELECT * FROM wine_list").fetchone()
    assert row["vintage"] == 2018
    save_product(con, shop, page("100242"), "2026-11-12")  # alcohol-free: kept but hidden
    assert len(con.execute("SELECT * FROM wine_list").fetchall()) == 1
    kinds = [r[0] for r in con.execute("SELECT kind FROM event ORDER BY id")]
    assert kinds.count("new_award") == 1 and kinds.count("new_offer") == 2


def test_vintage_change_starts_new_offer(tmp_path):
    con = db.connect(tmp_path / "t.db")
    shop = gall_id(con)
    p = page("100137")
    save_product(con, shop, p, "2026-11-01")
    p.specs["vintage"] = "2019"
    p.price = 220.0
    save_product(con, shop, p, "2026-11-08")
    offers = con.execute("SELECT vintage_key, is_current, price FROM offer ORDER BY id").fetchall()
    assert [tuple(o) for o in offers] == [("2018", 0, 210.0), ("2019", 1, 220.0)]
    ev = con.execute("SELECT old_value, new_value FROM event WHERE kind='vintage_change'").fetchone()
    assert tuple(ev) == ("2018", "2019")
    # history of the old vintage is kept separately
    assert con.execute("SELECT COUNT(*) FROM price_history").fetchone()[0] == 2
    # A page that no longer shows the vintage keeps the known one.
    del p.specs["vintage"]
    save_product(con, shop, p, "2026-11-15")
    assert con.execute("SELECT COUNT(*) FROM offer").fetchone()[0] == 2


def test_gone_and_back(tmp_path):
    con = db.connect(tmp_path / "t.db")
    shop = gall_id(con)
    save_product(con, shop, page("100137"), "2026-11-01")
    save_product(con, shop, page("100307"), "2026-11-01")
    assert mark_gone(con, shop, {"100137"}, "2026-11-08") == 1
    assert con.execute("SELECT gone FROM wine_list WHERE wine LIKE 'Taittinger%'").fetchone()[0] == "yes"
    save_product(con, shop, page("100307"), "2026-11-15")
    assert con.execute("SELECT gone FROM wine_list WHERE wine LIKE 'Taittinger%'").fetchone()[0] == "no"
    kinds = [r[0] for r in con.execute("SELECT kind FROM event WHERE kind IN ('gone','back')")]
    assert kinds == ["gone", "back"]


def test_price_change_events(tmp_path):
    con = db.connect(tmp_path / "t.db")
    shop = gall_id(con)
    p = page("100307")
    save_product(con, shop, p, "2026-11-01")
    p.price, p.promotion, p.in_stock_online = 49.0, None, True
    save_product(con, shop, p, "2026-11-08")
    kinds = {r[0] for r in con.execute("SELECT kind FROM event")}
    assert {"price_drop", "promotion_end", "back_in_stock"} <= kinds


# --- Upgrading the phase 1 database on the NAS -------------------------------------

def make_phase1_db(path):
    ns = {}
    src = (FIX / "phase1_db.py.txt").read_text()
    exec(compile(src.replace("from .config import DB_PATH", "DB_PATH = None"), "phase1", "exec"), ns)
    con = sqlite3.connect(path)
    con.executescript(ns["SCHEMA"])
    con.execute("INSERT INTO shop (name, website) VALUES ('Gall & Gall', 'https://www.gall.nl')")
    con.execute("INSERT INTO wine (norm_key, name, producer, colour) VALUES "
                "('veuve clicquot la grande dame brut|nv', 'Veuve Clicquot La Grande Dame Brut', "
                "'Veuve Clicquot', 'white')")
    con.execute("INSERT INTO offer (shop_id, wine_id, shop_product_id, url, title, volume_cl, price, "
                "price_per_75cl, promotion, in_stock_online, awards_count, first_seen, last_seen) VALUES "
                f"(1, 1, '100137', '{URLS['100137']}', 'Veuve Clicquot La Grande Dame Brut', 75, 200, 200, "
                "'2e fles 50% korting', 1, 3, '2026-11-09', '2026-11-09')")
    con.execute("INSERT INTO price_history (offer_id, seen_on, price, promotion) "
                "VALUES (1, '2026-11-09', 200, '2e fles 50% korting')")
    con.execute("INSERT INTO run_log (job, started, finished, pages_ok) "
                "VALUES ('gall', '2026-11-09T01:00:00', '2026-11-11T20:00:00', 2438)")
    con.commit()
    con.close()


def test_upgrade_phase1_database(tmp_path):
    path = tmp_path / "old.db"
    make_phase1_db(path)
    con = db.connect(path)
    assert con.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    o = con.execute("SELECT * FROM offer").fetchone()
    assert o["id"] == 1 and o["vintage_key"] == "nv" and o["vintage_checked"] == 0
    assert o["effective_price_per_75cl"] == 200.0  # phase 1 promotions are not trusted
    h = con.execute("SELECT * FROM price_history").fetchone()
    assert h["offer_id"] == 1 and h["effective_price_per_75cl"] is None
    assert con.execute("SELECT cuvee_id FROM wine").fetchone()[0] is not None
    assert con.execute("SELECT pages_ok FROM run_log").fetchone()[0] == 2438
    assert con.execute("PRAGMA foreign_key_check").fetchall() == []
    # First phase 2 run: the vintage gets filled in on the same offer, no vintage-change event.
    save_product(con, 1, page("100137"), "2026-11-16")
    assert con.execute("SELECT COUNT(*) FROM offer").fetchone()[0] == 1
    assert con.execute("SELECT vintage_key FROM offer").fetchone()[0] == "2018"
    assert con.execute("SELECT COUNT(*) FROM event WHERE kind='vintage_change'").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM price_history WHERE offer_id=1").fetchone()[0] == 2
    con.close()
    db.connect(path).close()  # opening again changes nothing


# --- Scores ---------------------------------------------------------------------------

def test_overall_formula():
    assert scores.overall([]) is None
    # one IWSC silver 90 points: (1.0*90 + 0.5*88) / 1.5
    assert scores.overall([(90, 1.0)]) == round((90 + 44) / 1.5, 1)


def test_recompute_scores_and_deals(tmp_path):
    con = db.connect(tmp_path / "t.db")
    shop = gall_id(con)
    p = page("100137")
    p.promotion = "2e fles 50% korting"
    save_product(con, shop, p, "2026-11-12")
    save_product(con, shop, page("100307"), "2026-11-12")
    scores.recompute(con, dt.date(2026, 11, 12))
    row = con.execute("SELECT * FROM best_buys WHERE wine LIKE 'Veuve%'").fetchone()
    assert row["score"] == round((90 + 44) / 1.5, 1)
    assert row["best_award"] == "IWSC 2024 Silver 90 pts"
    assert row["price_per_bottle_at_6"] == 157.5
    assert row["deal"] == "yes" and "below normal price" in row["deal_reason"]
    assert row["price_6_bottles_incl_delivery"] == 945.0
    assert row["value"] is not None
    # Taittinger: out of stock but still on the site; no awards details -> no score
    t = con.execute("SELECT * FROM best_buys WHERE wine LIKE 'Taittinger%'").fetchone()
    assert t["score"] is None and t["deal"] == "yes"


def test_deal_uses_90_day_median(tmp_path):
    con = db.connect(tmp_path / "t.db")
    shop = gall_id(con)
    p = page("100307")
    start = dt.date(2026, 6, 1)
    for week in range(20):  # always "2e fles 50%": that is the normal price by now
        save_product(con, shop, p, (start + dt.timedelta(weeks=week)).isoformat())
    scores.recompute(con, start + dt.timedelta(weeks=19))
    assert con.execute("SELECT is_deal FROM offer_score").fetchone()[0] == 0


def test_first_phase2_read_is_a_baseline(tmp_path):
    path = tmp_path / "old.db"
    make_phase1_db(path)
    con = db.connect(path)
    save_product(con, 1, page("100137"), "2026-11-16")  # price and promotion differ from phase 1
    kinds = {r[0] for r in con.execute("SELECT kind FROM event")}
    assert kinds == {"new_award"}
