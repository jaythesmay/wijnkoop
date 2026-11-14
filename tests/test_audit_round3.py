"""Round 3 of the audit: price history of changes only, leaner scores, search index, upgrades once."""
import datetime as dt

from wijnkoop import db, scores, search_index, ui
from wijnkoop.collect import save_product
from wijnkoop.shops import gall


def _wine(i, price, title=None):
    return gall.GallProduct(url=f"https://www.gall.nl/w-{i}.html", product_id=str(i), title=title or f"Wijn {i} Rood",
                            colour="red", volume_cl=75, price=price, in_stock_online=True, specs={"vintage": "2022"})


def test_price_history_keeps_only_changes(tmp_path):
    con = db.connect(tmp_path / "t.db")
    start = dt.date(2026, 6, 1)
    for week, price in enumerate([10, 10, 10, 8, 8, 10]):
        save_product(con, 1, _wine(1, float(price)), (start + dt.timedelta(weeks=week)).isoformat())
    rows = [tuple(r) for r in con.execute("SELECT seen_on, price FROM price_history ORDER BY seen_on")]
    assert rows == [("2026-06-01", 10.0), ("2026-06-22", 8.0), ("2026-07-06", 10.0)]


def test_expand_gives_the_weekly_series_back():
    rows = [("2026-06-01", 10.0), ("2026-06-22", 8.0)]
    assert scores.expand(rows, "2026-07-06") == [
        ("2026-06-01", 10.0), ("2026-06-08", 10.0), ("2026-06-15", 10.0),
        ("2026-06-22", 8.0), ("2026-06-29", 8.0), ("2026-07-06", 8.0)]
    assert scores.expand(rows, "2026-07-06", start="2026-06-20")[0] == ("2026-06-20", 10.0)


def test_old_weekly_history_is_compacted_once(tmp_path):
    con = db.connect(tmp_path / "t.db")
    save_product(con, 1, _wine(1, 10.0), "2026-06-01")
    offer = con.execute("SELECT id FROM offer").fetchone()[0]
    row = con.execute("SELECT promotion, was_price, effective_price_per_75cl, in_stock FROM price_history").fetchone()
    for week in range(1, 6):  # as older versions wrote it: a line every week
        con.execute("INSERT INTO price_history (offer_id, seen_on, price, promotion, was_price, "
                    "effective_price_per_75cl, in_stock) VALUES (?, ?, 10.0, ?, ?, ?, ?)",
                    (offer, (dt.date(2026, 6, 1) + dt.timedelta(weeks=week)).isoformat(), *row))
    con.execute("DELETE FROM setting WHERE key='price_history_compacted'")
    assert db.compact_price_history(con) == 5
    assert con.execute("SELECT COUNT(*) FROM price_history").fetchone()[0] == 1
    assert db.compact_price_history(con) == 0  # once


def test_deal_median_still_works_with_changes_only(tmp_path):
    con = db.connect(tmp_path / "t.db")
    start = dt.date(2026, 3, 1)
    for week in range(20):  # 10 euro for 19 weeks, then 8
        save_product(con, 1, _wine(1, 8.0 if week == 19 else 10.0), (start + dt.timedelta(weeks=week)).isoformat())
    scores.recompute(con, start + dt.timedelta(weeks=19))
    ref = con.execute("SELECT reference_price, is_deal FROM offer_score").fetchone()
    assert tuple(ref) == (10.0, 1)


def test_connect_skips_the_schema_when_up_to_date(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    db.connect(path).close()
    called = []
    monkeypatch.setattr(db, "migrate", lambda con, p=None: called.append(1))
    db.connect(path).close()
    assert called == []


def test_search_uses_the_index_and_forgives_typos(tmp_path):
    con = db.connect(tmp_path / "t.db")
    for i, name in enumerate(["Barolo Mauro Molino Rood", "Chablis Premier Cru Wit", "Rioja Reserva Rood"], 1):
        save_product(con, 1, _wine(i, 12.0, name), "2026-11-14")
    scores.recompute(con, dt.date(2026, 11, 14))
    assert con.execute("SELECT COUNT(*) FROM search_text").fetchone()[0] == 3
    ids = search_index.candidates(con, "barollo")
    assert ids and len(ids) <= search_index.CANDIDATES
    found = ui.search(con, "barollo molino")
    assert found and "Barolo" in found[0]["wine"]
