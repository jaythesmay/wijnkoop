"""Loading an official results file: any format, any layout, then checking the shops' claims."""
import asyncio
import datetime as dt
import re
from pathlib import Path

import pytest

from wijnkoop import db, scores
from wijnkoop.awards import upload
from wijnkoop.collect import save_product
from wijnkoop.shops.gall import GallProduct

ROOT = Path(__file__).parent.parent
FILES = Path(__file__).parent / "fixtures" / "upload"
TODAY = dt.date(2026, 11, 16)


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    from wijnkoop import config

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    return tmp_path / "data"


def _parse(name):
    rows, kind = upload.read_file(name, (FILES / name).read_bytes())
    recs, how = upload.records_from_rows(rows)
    return [{k: v for k, v in r.items() if v and k != "raw"} for r in recs], kind, how, rows


def _short(recs):
    return [(r.get("producer"), r["name"], r.get("vintage"), r.get("medal"), r.get("points")) for r in recs]


def test_csv_with_header():
    recs, kind, how, _ = _parse("results-table.csv")
    assert kind == "CSV" and "header row" in how
    assert _short(recs) == [("Catena Zapata", "Malbec Argentino", 2020, "Gold", 95.0),
                            ("Bodegas Muga", "Rioja Reserva", 2019, "Silver", 90.0),
                            ("Pascual Toso", "Malbec", 2024, "Bronze", 86.0)]
    assert recs[0]["country"] == "Argentina"


def test_dutch_semicolon_csv():
    recs, *_ = _parse("concours-semicolon.csv")
    assert _short(recs) == [("Domaine Clément", "Menetou-Salon Tradition Rouge", 2023, "Gold", None),
                            ("Domaine Coudoulet", "Picpoul de Pinet", 2025, "Silver", None)]


def test_table_without_header():
    recs, _, how, _ = _parse("no-header.txt")
    assert "without a header" in how
    assert _short(recs)[1] == (None, "Muga Rioja Reserva", 2019, "Silver", 90.0)


def test_saved_web_page_cards():
    recs, kind, how, _ = _parse("cards.html")
    assert kind == "web page" and "blocks" in how
    assert len(recs) == 2  # the menu's "Gold Silver" is not a wine
    assert recs[0]["name"] == "Malbec Argentino Catena Zapata" and recs[0]["medal"] == "Gold"
    assert recs[0]["points"] == 95 and recs[0]["country"] == "Argentina"
    assert recs[1]["medal"] == "Silver" and recs[1]["points"] == 90


def test_text_in_sections():
    recs, _, how, rows = _parse("mundus-vini-sections.txt")
    assert "sections" in how
    assert _short(recs) == [(None, "Catena Zapata Malbec Argentino", 2020, "Gold", None),
                            (None, "Bodegas Muga Rioja Reserva", 2019, "Gold", None),
                            (None, "Pascual Toso Malbec", 2024, "Silver", None)]
    comp, year = upload.detect("mundus-vini-sections.txt", rows)
    assert comp[0] == "Mundus Vini" and year == 2025


def test_json_excel_pdf():
    recs, kind, *_ = _parse("one-wine.json")
    assert kind == "JSON" and _short(recs) == [("Catena Zapata", "Malbec Argentino", 2020, "Platinum", 97.0)]
    recs, kind, _, rows = _parse("iwsc-2025.xlsx")
    assert kind == "Excel" and _short(recs)[1] == ("Bodegas Muga", "Muga Rioja Reserva", 2019, "Silver", 91.0)
    comp, year = upload.detect("iwsc-2025.xlsx", rows)
    assert comp[0] == "IWSC" and year == 2025
    recs, kind, *_ = _parse("table.pdf")
    assert kind == "PDF"
    assert [r["medal"] for r in recs] == ["Grand Gold", "Gold", "Silver"]
    assert recs[1]["producer"] == "Bodegas Muga" and recs[1]["name"] == "Rioja Reserva"


def test_vintage_is_not_the_competition_year():
    _, _, _, rows = _parse("results-table.csv")
    assert upload.detect("results-table.csv", rows)[1] is None


def test_medal_words_inside_names():
    assert upload.medal_in("Catena Zapata Malbec Argentino 2020") is None
    assert upload.medal_in("Médaille d'Or") == "Gold"


def _wine(i, title, price, awards=()):
    return GallProduct(url=f"https://www.gall.nl/w-{i}.html", product_id=str(i), title=title, colour="red",
                       volume_cl=75, price=price, promotion=None, in_stock_online=True, category="Rood",
                       expert_awards=list(awards),
                       specs={"vintage": "2021", "country": "Spanje", "producer": " ".join(title.split()[:2]),
                              "grapes": "Tempranillo"})


@pytest.fixture
def con(tmp_path):
    con = db.connect(tmp_path / "wijnkoop.db")
    for p in [
        _wine(1, "Bodegas Muga Reserva", 10.0, awards=[("IWC 2025", "Gold 95 punten")]),
        _wine(2, "Bodegas Olarra Crianza", 12.0, awards=[("Mundus Vini 2025", "Zilver")]),
        _wine(3, "Bodegas Faustino Gran Reserva", 14.0, awards=[("Mundus Vini 2025", "Goud")]),
        _wine(4, "Cantina Tollo Rosato", 6.0),
    ]:
        save_product(con, 1, p, TODAY.isoformat())
    scores.recompute(con, TODAY)
    return con


RESULTS = b"""MUNDUS VINI 2025 RESULTS
GOLD
Bodegas Muga Reserva 2021
SILVER
Bodegas Olarra Crianza 2021
Some Unknown Winery Tinto 2022
"""


def _score(con, name):
    return con.execute("SELECT score FROM best_buys WHERE wine LIKE ?", (f"%{name}%",)).fetchone()[0]


def test_import_checks_shop_claims(con):
    olarra = _score(con, "Olarra")
    r = upload.import_upload(con, "mundus.txt", RESULTS, today=TODAY)
    assert _score(con, "Olarra") == olarra  # the same silver from the shop and the official list counts once
    u = r["upload"]
    assert u["short"] == "Mundus Vini" and u["year"] == 2025 and u["awards"] == 3 and u["linked"] == 2
    checks = {m["wine"]: m["check"] for m in r["matched"]}
    assert checks == {"Bodegas Olarra Crianza 2021": "confirmed", "Bodegas Muga Reserva 2021": "not on the shop page"}
    assert [m["wine"] for m in r["missing"]] == ["Bodegas Faustino Gran Reserva 2021"]
    # the official gold now counts for Muga
    assert con.execute("""SELECT COUNT(*) FROM award a JOIN award_match m ON m.award_id = a.id
                          WHERE a.source = 'upload' AND a.medal = 'Gold'""").fetchone()[0] == 1
    assert upload.uploads(con)[0]["filename"] == "mundus.txt"


def test_import_finds_a_different_result_and_deletes(con):
    r = upload.import_upload(con, "mv.csv", b"Wine;Medal\nBodegas Olarra Crianza 2021;Bronze\n",
                             competition="Mundus Vini", year=2025, today=TODAY)
    assert [(m["wine"], m["check"]) for m in r["matched"]] == [("Bodegas Olarra Crianza 2021", "different")]
    assert upload.delete_upload(con, r["upload"]["id"]) == 1
    assert con.execute("SELECT COUNT(*) FROM award WHERE source = 'upload'").fetchone()[0] == 0
    assert upload.uploads(con) == []


def test_unreadable_file_leaves_nothing(con):
    with pytest.raises(Exception):
        upload.import_upload(con, "broken.xlsx", b"PK\x03\x04 not really a spreadsheet xl/", today=TODAY)
    assert con.execute("SELECT COUNT(*) FROM award_upload").fetchone()[0] == 0


def test_upload_pages(con, tmp_path):
    from datasette.app import Datasette

    con.close()
    ds = Datasette([str(tmp_path / "wijnkoop.db")], plugins_dir=str(ROOT / "datasette" / "plugins"),
                   template_dir=str(ROOT / "datasette" / "templates"))

    async def run():
        page = await ds.client.get("/upload")
        assert page.status_code == 200 and "No results files loaded yet" in page.text
        token = re.search(r'"x-csrftoken": "([^"]+)"', page.text).group(1)
        cookies = {"ds_csrftoken": page.cookies.get("ds_csrftoken")}
        r = await ds.client.post("/upload?filename=mundus.txt&competition=&year=", content=RESULTS,
                                 headers={"x-csrftoken": token, "content-type": "application/octet-stream"},
                                 cookies=cookies)
        assert r.status_code == 200, r.text
        report = await ds.client.get(f"/upload/{r.json()['id']}")
        listing = await ds.client.get("/upload")
        awards = await ds.client.get("/awards")
        bad = await ds.client.post("/upload?filename=x.pdf", content=b"%PDF-1.4 garbage",
                                   headers={"x-csrftoken": token}, cookies=cookies)
        return report, listing, awards, bad

    report, listing, awards, bad = asyncio.run(run())
    assert report.status_code == 200
    assert "Mundus Vini" in report.text and "✓ confirmed" in report.text and "Silver 0" not in report.text
    assert "Bodegas Faustino Gran Reserva 2021" in report.text  # shop claim missing from the file
    assert "mundus.txt" in listing.text and 'href="/upload"' in awards.text
    assert bad.status_code == 400 and "error" in bad.json()


def test_file_is_kept_and_new_wines_are_linked_later(con, data_dir):
    r = upload.import_upload(con, "mundus 2025.txt", RESULTS, today=TODAY)
    upload_id = r["upload"]["id"]
    kept = upload.stored_file(upload_id, "mundus 2025.txt")
    assert kept.read_bytes() == RESULTS and kept.parent == data_dir / "uploads"
    assert r["upload"]["kept"] and upload.uploads(con)[0]["kept"]
    assert upload.relink_uploads(con) == 0  # nothing new since the upload
    # a later shop run finds the wine that was not in any shop yet
    save_product(con, 1, _wine(9, "Some Unknown Winery Tinto", 9.0), TODAY.isoformat())
    assert upload.relink_uploads(con) == 1
    assert upload.relink_uploads(con) == 0  # checked once per new wine
    u = upload.upload_report(con, upload_id)["upload"]
    assert u["linked"] == 3
    assert upload.delete_upload(con, upload_id) == 3
    assert not kept.exists()


def test_download_kept_file(con, tmp_path):
    from datasette.app import Datasette

    r = upload.import_upload(con, "mundus.txt", RESULTS, today=TODAY)
    con.close()
    ds = Datasette([str(tmp_path / "wijnkoop.db")], plugins_dir=str(ROOT / "datasette" / "plugins"),
                   template_dir=str(ROOT / "datasette" / "templates"))

    async def run():
        return (await ds.client.get(f"/upload/{r['upload']['id']}/file"),
                await ds.client.get(f"/upload/{r['upload']['id']}"), await ds.client.get("/upload/999/file"))

    f, page, missing = asyncio.run(run())
    assert f.status_code == 200 and f.content == RESULTS and 'filename="mundus.txt"' in f.headers["content-disposition"]
    assert "download the file" in page.text and missing.status_code == 404
