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


def test_progress_and_detailed_report(con):
    seen = []
    r = upload.import_upload(con, "mundus.txt", RESULTS, today=TODAY,
                             progress=lambda step, **d: seen.append((step, d)))
    steps = list(dict.fromkeys(s for s, _ in seen))
    assert steps == ["read", "find", "link", "scores", "report", "done"]
    last_link = [d for s, d in seen if s == "link"][-1]
    assert (last_link["done"], last_link["total"], last_link["linked"]) == (3, 3, 2)
    log = r["upload"]["log"]
    assert (log["records"], log["stored"], log["skipped"], log["linked"], log["not_linked"]) == (3, 3, 0, 2, 1)
    assert (log["competition_from"], log["year_from"]) == ("the file", "the file")
    twice = b"Wine;Medal\nBodegas Olarra Crianza 2021;Silver\nBodegas Olarra Crianza 2021;Silver\n"
    again = upload.import_upload(con, "mv.csv", twice, competition="Mundus Vini", year=2025, today=TODAY)
    assert (again["upload"]["log"]["skipped"], again["upload"]["log"]["year_from"]) == (1, "you")
    assert [s["step"] for s in log["steps"]] == list(upload.STEPS.values())
    assert upload.upload_report(con, r["upload"]["id"])["upload"]["log"]["linked"] == 2  # kept with the upload
    rows = upload.report_rows(con, r["upload"]["id"])
    assert len(rows) == 3 and all(len(x) == len(upload.REPORT_COLUMNS) for x in rows)
    status = {x[1]: (x[7], x[12]) for x in rows}
    assert status["Bodegas Olarra Crianza"] == ("linked", "confirmed")
    assert status["Some Unknown Winery Tinto"] == ("not in your shops", "")


def test_upload_page_shows_progress_and_offers_the_report(con, tmp_path):
    from datasette.app import Datasette

    con.close()
    ds = Datasette([str(tmp_path / "wijnkoop.db")], plugins_dir=str(ROOT / "datasette" / "plugins"),
                   template_dir=str(ROOT / "datasette" / "templates"))
    from datasette.plugins import pm

    ui = next(m for m in pm.get_plugins() if getattr(m, "__name__", "") == "wijnkoop_ui.py")
    seen = []

    async def run():
        page = await ds.client.get("/upload")
        token = re.search(r'"x-csrftoken": "([^"]+)"', page.text).group(1)
        real = ui.award_upload.import_upload

        def spy(*a):  # what the page would see halfway
            def progress(step, **d):
                a[-1](step, **d)
                if step == "link":
                    seen.append(ui._progress_text(ui.UPLOAD_PROGRESS["abc123def"]))
            return real(*a[:-1], progress)

        ui.award_upload.import_upload = spy
        try:
            r = await ds.client.post("/upload?filename=mundus.txt&job=abc123def", content=RESULTS,
                                     headers={"x-csrftoken": token, "content-type": "application/octet-stream"},
                                     cookies={"ds_csrftoken": page.cookies.get("ds_csrftoken")})
        finally:
            ui.award_upload.import_upload = real
        after = await ds.client.get("/upload/progress?job=abc123def")
        report = await ds.client.get(f"/upload/{r.json()['id']}")
        csv_file = await ds.client.get(f"/upload/{r.json()['id']}/report.csv")
        return after, report, csv_file

    after, report, csv_file = asyncio.run(run())
    now = seen[-1]["steps"]
    assert [s["state"] for s in now] == ["done", "done", "now", "todo", "todo"]
    assert "3 of 3" in now[2]["detail"] and "2 linked" in now[2]["detail"]
    assert after.json() == {"step": None}  # forgotten once done
    assert "Linked to a wine in your shops" in report.text and "report.csv" in report.text
    assert "Time taken:" in report.text
    lines = csv_file.text.lstrip("﻿").splitlines()
    assert lines[0].startswith("producer;wine in the file;") and len(lines) == 4
    assert "attachment" in csv_file.headers["content-disposition"]


def test_a_shared_word_is_not_the_same_producer(con):
    """'Cape Mentelle Heritage Cabernet Sauvignon' was linked to 'Cape Dream Merlot-Cabernet Sauvignon' (76%)."""
    from wijnkoop.awards import importer

    for i, (title, producer) in enumerate([("Cape Dream Merlot-Cabernet Sauvignon", None),
                                           ("Cape Mentelle Sauvignon Blanc Semillon", "Cape Mentelle")], 10):
        p = _wine(i, title, 9.0)
        p.specs["producer"] = producer
        p.colour = "white" if "Blanc" in title else "red"
        save_product(con, 1, p, TODAY.isoformat())
    files = [b"Producer;Wine;Vintage;Medal\nCape Mentelle;Heritage Cabernet Sauvignon;2020;Silver\n"
             b"Cape Mentelle;Sauvignon Blanc Semillon;2021;Gold\n",
             b"Wine;Vintage;Medal\nCape Mentelle Heritage Cabernet Sauvignon;2020;Silver\n"]
    for n, data in enumerate(files):
        r = upload.import_upload(con, f"iwsc{n}.csv", data, competition="IWSC", year=2025, today=TODAY)
        assert all("Cape Dream" not in m["wine"] for m in r["matched"])
    assert [m["wine"] for m in upload.upload_report(con, 2)["matched"]] == []
    assert any("Cape Mentelle Sauvignon Blanc Semillon" in m["wine"] for m in upload.upload_report(con, 1)["matched"])

    agree = importer.producers_agree
    w = importer._words
    assert not agree(w("Cape Mentelle"), "Heritage", w("Cape Dream"), "Cape Dream Merlot")
    assert not agree(w("Cape Mentelle"), "Heritage", set(), "Cape Dream Merlot-Cabernet Sauvignon")
    assert not agree(set(), "Cape Mentelle Heritage Cabernet", set(), "Cape Dream Merlot-Cabernet Sauvignon")
    assert agree(w("Herdade do Esporão"), "Reserva", w("Esporão"), "Esporão Reserva")
    assert agree(w("Bodegas Muga"), "Reserva", set(), "Muga Reserva")
    assert agree(set(), "Bodegas Muga Reserva 2021", w("Bodegas Muga"), "Muga Reserva")

    # A link made before this check is undone when the scores are computed
    award = con.execute("SELECT id FROM award WHERE name_raw='Heritage Cabernet Sauvignon'").fetchone()[0]
    dream = con.execute("SELECT id FROM wine WHERE name LIKE 'Cape Dream%'").fetchone()[0]
    con.execute("INSERT INTO award_match (award_id, wine_id, match_score, confirmed) VALUES (?,?,0.76,NULL)",
                (award, dream))
    scores.recompute(con, TODAY)
    assert not con.execute("SELECT 1 FROM award_match WHERE award_id=?", (award,)).fetchone()


def test_say_yes_or_no_to_a_link(con):
    r = upload.import_upload(con, "mundus.txt", RESULTS, today=TODAY)
    uid = r["upload"]["id"]
    olarra = next(m for m in r["matched"] if "Olarra" in m["wine"])
    assert upload.judge_match(con, uid, olarra["award_id"], olarra["wine_id"], same=False)
    rows = {m["wine"]: m for m in upload.upload_report(con, uid)["matched"]}
    assert rows["Bodegas Olarra Crianza 2021"]["check"] == "rejected"
    assert upload.upload_report(con, uid)["upload"]["linked"] == 1
    # not linked again by a later check, and shown as not counted on the wine page
    upload.relink_uploads(con)
    assert con.execute("SELECT confirmed FROM award_match WHERE award_id=?", (olarra["award_id"],)).fetchone()[0] == 0
    assert upload.judge_match(con, uid, olarra["award_id"], olarra["wine_id"], same=True)
    assert upload.upload_report(con, uid)["upload"]["linked"] == 2
    assert not upload.judge_match(con, uid + 1, olarra["award_id"], olarra["wine_id"], same=True)  # another upload


def test_report_buttons_say_yes_or_no(con, tmp_path, monkeypatch):
    from datasette.app import Datasette

    r = upload.import_upload(con, "mundus.txt", RESULTS, today=TODAY)
    uid, m = r["upload"]["id"], r["matched"][0]
    con.close()
    ds = Datasette([str(tmp_path / "wijnkoop.db")], plugins_dir=str(ROOT / "datasette" / "plugins"),
                   template_dir=str(ROOT / "datasette" / "templates"))
    from datasette.plugins import pm

    plugin = next(p for p in pm.get_plugins() if getattr(p, "__name__", "") == "wijnkoop_ui.py")
    rescored = []
    monkeypatch.setattr(plugin, "_rescore_soon", lambda: rescored.append(1))

    async def run():
        page = await ds.client.get(f"/upload/{uid}")
        token = re.search(r'name="csrftoken" value="([^"]+)"', page.text).group(1)
        cookies = {"ds_csrftoken": page.cookies.get("ds_csrftoken")}
        post = await ds.client.post(f"/upload/{uid}", data={"csrftoken": token, "award": m["award_id"],
                                                            "wine": m["wine_id"], "verdict": "other"}, cookies=cookies)
        after = await ds.client.get(f"/upload/{uid}")
        quick = await ds.client.post(f"/upload/{uid}", data={"csrftoken": token, "award": m["award_id"],
                                                             "wine": m["wine_id"], "verdict": "same"},
                                     cookies=cookies, headers={"x-wk-quick": "1"})
        return page, post, after, quick, await ds.client.get(f"/upload/{uid}")

    page, post, after, quick, again = asyncio.run(run())
    assert "✗ No" in page.text and post.status_code == 302
    assert "not this wine, not counted" in after.text and "<b>✗ No</b>, not counted" in after.text
    assert quick.json() == {"saved": True, "verdict": "same"} and rescored == [1, 1]
    assert "<b>✓ Yes</b>, counts" in again.text
    # the price of the wine in your shops, cheapest shop first
    links = page.text[page.text.index('id="links"'):]
    assert re.search(r"€\d+\.\d\d", links) and "<th class=\"num\">Price</th>" in page.text


def test_estate_and_wine_apart():
    from wijnkoop.normalize import split_estate

    assert split_estate("Herdade do Esporão Reserva Tinto", "Esporão") == ("Esporão", "Reserva Tinto")
    assert split_estate("Bodegas Muga Reserva", "Bodegas Muga") == ("Bodegas Muga", "Reserva")
    assert split_estate("Cape Dream Merlot", "Cape Mentelle") == ("Cape Mentelle", "Cape Dream Merlot")
    assert split_estate("Muga", "Bodegas Muga") == ("Bodegas Muga", "Muga")  # never an empty wine
    assert split_estate("Rioja Reserva", None) == (None, "Rioja Reserva")
    # a results file: with a producer column, or with the estate at the start of the name
    assert upload.file_parts("Bodegas Olarra", "Cerro Anon Crianza") == ("Bodegas Olarra", "Cerro Anon Crianza")
    assert upload.file_parts("Esporão", "Esporão Reserva") == ("Esporão", "Reserva")
    assert upload.file_parts(None, "Bodegas Muga Rioja Reserva", "Bodegas Muga") == ("Bodegas Muga", "Rioja Reserva")
    assert upload.file_parts(None, "Rioja Reserva", "Bodegas Muga") == (None, "Rioja Reserva")


def test_wine_name_in_the_tables(con):
    row = con.execute("SELECT producer, wine_name FROM wine_list WHERE wine LIKE 'Bodegas Muga%'").fetchone()
    assert tuple(row) == ("Bodegas Muga", "Reserva")
    assert con.execute("SELECT wine_name FROM cuvee WHERE name='Bodegas Muga Reserva'").fetchone()[0] == "Reserva"
    con.execute("UPDATE wine SET wine_name=NULL")
    assert db.fill_wine_names(con) >= 4
    assert con.execute("SELECT COUNT(*) FROM wine WHERE wine_name IS NULL").fetchone()[0] == 0


def _answers(con):
    return [r[0] for r in con.execute("SELECT m.confirmed FROM award_match m JOIN award a ON a.id = m.award_id "
                                      "WHERE a.name_raw = 'Cerro Anon Crianza'")]


PROBABLE = b"Producer;Wine;Vintage;Medal\nBodegas Olarra;Cerro Anon Crianza;2021;Silver\n"


def test_a_match_to_check_stays_until_answered_and_is_not_asked_twice(con):
    first = upload.import_upload(con, "a.csv", PROBABLE, competition="IWSC", year=2025, today=TODAY)
    assert first["upload"]["log"]["review"] == 1
    todo = upload.to_check(con)
    assert len(todo) == 1 and todo[0]["files"] == 1 and todo[0]["price"] == 12.0
    assert todo[0]["score"] is not None  # IWSC silver, on the 100-point scale
    assert (todo[0]["file_estate"], todo[0]["file_wine"]) == ("Bodegas Olarra", "Cerro Anon Crianza")
    assert (todo[0]["shop_estate"], todo[0]["shop_wine"]) == ("Bodegas Olarra", "Crianza")
    upload.relink_uploads(con)
    scores.recompute(con, TODAY)
    assert len(upload.to_check(con)) == 1  # still waiting: nothing but your Yes or No ends it

    # the same award in a second file: in the queue already, so not a second task
    second = upload.import_upload(con, "b.csv", PROBABLE, competition="IWSC", year=2025, today=TODAY)
    assert second["upload"]["log"]["review_known"] == 1
    todo = upload.to_check(con)
    assert len(todo) == 1 and todo[0]["files"] == 2 and todo[0]["results"] == ["IWSC 2025 Silver"]

    # one answer for both files, and for a file loaded later
    assert upload.judge_match(con, None, todo[0]["award_id"], todo[0]["wine_id"], same=False)
    assert upload.to_check(con) == []
    assert _answers(con) == [0] * 2
    third = upload.import_upload(con, "c.csv", PROBABLE, competition="IWSC", year=2025, today=TODAY)
    assert third["upload"]["log"]["answered_before"] == 1
    assert upload.to_check(con) == []
    assert _answers(con) == [0] * 3


def test_check_page(con, tmp_path, monkeypatch):
    from datasette.app import Datasette

    upload.import_upload(con, "a.csv", PROBABLE, competition="IWSC", year=2025, today=TODAY)
    m = upload.to_check(con)[0]
    con.close()
    ds = Datasette([str(tmp_path / "wijnkoop.db")], plugins_dir=str(ROOT / "datasette" / "plugins"),
                   template_dir=str(ROOT / "datasette" / "templates"))
    from datasette.plugins import pm

    plugin = next(p for p in pm.get_plugins() if getattr(p, "__name__", "") == "wijnkoop_ui.py")
    monkeypatch.setattr(plugin, "_rescore_soon", lambda: None)

    async def run():
        home = await ds.client.get("/")
        page = await ds.client.get("/upload/check")
        filtered = await ds.client.get("/upload/check?eur_from=15&sort=points")
        token = re.search(r'name="csrftoken" value="([^"]+)"', page.text).group(1)
        post = await ds.client.post("/upload/check", data={"csrftoken": token, "award": m["award_id"],
                                                           "wine": m["wine_id"], "verdict": "same"},
                                    cookies={"ds_csrftoken": page.cookies.get("ds_csrftoken")},
                                    headers={"x-wk-quick": "1"})
        return home, page, post, await ds.client.get("/upload/check"), filtered

    home, page, post, after, filtered = asyncio.run(run())
    assert "No links to check within these limits" in filtered.text and 'value="15"' in filtered.text
    assert '<option value="points" selected>' in filtered.text
    assert "/upload/check" in home.text
    links = page.text[page.text.index('id="links"'):]
    assert "<th>In the results file</th><th>In your shops · <a href=\"/upload/check?sort=unlike\"" in links
    assert "<dt>Estate</dt><dd>Bodegas Olarra</dd>" in links and "<dt>Wine</dt><dd><b>Cerro Anon Crianza</b> 2021" in links
    assert ">Crianza</a> 2021" in links and "€12.00" in links
    assert post.json() == {"saved": True, "verdict": "same", "left": 0}
    assert "Nothing to check" in after.text
    assert 'name="pts_from"' in page.text and 'name="eur_to"' in page.text and 'name="alike_from"' in page.text
    assert 'data-label="Points" class="num">' in links


def test_check_list_filters_and_sorts():
    rows = [
        {"award_id": 1, "match_score": 0.95, "score": 93.0, "price": 22.0, "shop_wine": "Barolo"},
        {"award_id": 2, "match_score": 0.80, "score": 88.0, "price": 9.5, "shop_wine": "Albariño"},
        {"award_id": 3, "match_score": 0.70, "score": None, "price": 12.0, "shop_wine": "Chianti"},
        {"award_id": 4, "match_score": 0.88, "score": 90.0, "price": None, "shop_wine": "Douro"},
    ]
    ids = lambda out: [r["award_id"] for r in out]
    assert ids(upload.pick_to_check(rows)) == [1, 2, 3, 4]  # as to_check gave them
    assert ids(upload.pick_to_check(rows, price=(9, 15))) == [2, 3]
    assert ids(upload.pick_to_check(rows, points=(89, None))) == [1, 4]  # no points: left out
    assert ids(upload.pick_to_check(rows, alike=(80, 90))) == [2, 4]
    assert ids(upload.pick_to_check(rows, price=(9, 15), points=(85, 95))) == [2]
    assert ids(upload.pick_to_check(rows, sort="points")) == [1, 4, 2, 3]
    assert ids(upload.pick_to_check(rows, sort="cheap")) == [2, 3, 1, 4]
    assert ids(upload.pick_to_check(rows, sort="dear")) == [1, 3, 2, 4]
    assert ids(upload.pick_to_check(rows, sort="unlike")) == [3, 2, 4, 1]
    assert ids(upload.pick_to_check(rows, sort="name")) == [2, 1, 3, 4]
