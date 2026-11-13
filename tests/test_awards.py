import datetime as dt
from pathlib import Path

from wijnkoop import db
from wijnkoop.awards import cmb, importer, iwc
from wijnkoop.collect import save_product
from wijnkoop.shops.gall import GallProduct

FIX = Path(__file__).parent / "fixtures" / "awards"


def test_iwc_list():
    html = (FIX / "iwc-2026-score95.html").read_text()
    items = iwc.parse_list(html, 2026, 95)
    assert len(items) == 20
    first = items[0]
    assert first["name"] == "Aluvia" and first["vintage"] == 2023
    assert first["producer"] == "Aluvia Vineyard" and first["country"] == "Argentina"
    assert first["medals"] == ["Gold"] and first["points"] == 95
    assert first["source_url"].endswith("beverage_details?wid=199185")
    assert iwc.has_next(html, 1) and not iwc.has_next(html, 16)
    rows = list(iwc.awards_from_list(items))
    assert sum(r["medal"] == "Trophy" for r in rows) == 5
    assert iwc.list_url(2026, 95, 2).endswith("page=2&wpcat=WineTab.S&Challenge_Year=2026&Score=95")


def test_cmb_list():
    html = (FIX / "cmb-2026-red-p2.html").read_text()
    items = cmb.parse_list(html, 2026, "red")
    assert len(items) == 24
    first = items[0]
    assert first["name"] == "Finca El Serrano" and first["vintage"] == 2023
    assert first["producer"] == "Bodegas Murviedro SA"
    assert first["country"] == "Spain" and first["region"] == "Valencia"
    assert first["medal"] == "Grand Gold" and first["colour"] == "red"
    assert cmb.has_next(html, 2)


def shop_wine(con, pid, title, producer, colour="red", vintage=None):
    p = GallProduct(url=f"https://www.gall.nl/x-{pid}.html", product_id=str(pid), title=title, colour=colour,
                    volume_cl=75, price=12.99)
    p.specs = {"producer": producer}
    if vintage:
        p.specs["vintage"] = str(vintage)
    save_product(con, 1, p, "2026-11-12")


def add_award(con, producer, name, vintage, medal="Gold", points=None, colour=None, source="iwc", found="2026-11-12"):
    comp = importer.competition_id(con, importer._comp("IWC" if source == "iwc" else "CMB"))
    importer.store_award(con, comp, {"producer": producer, "name": name, "vintage": vintage, "medal": medal,
                                     "points": points, "colour": colour, "year": 2026,
                                     "source_url": f"https://example/{producer}/{name}/{vintage}/{medal}"},
                         source, found)


def test_matching(tmp_path):
    con = db.connect(tmp_path / "t.db")
    shop_wine(con, 1, "Trivento Private Reserve Malbec", "Trivento", vintage=2025)
    shop_wine(con, 2, "Trivento Malbec", "Trivento", vintage=2025)
    shop_wine(con, 3, "Marqués de Riscal Reserva", "Marqués de Riscal", vintage=2019)
    add_award(con, "Trivento Bodegas y Viñedos S.A.", "Trivento Private Reserve Malbec", 2025, points=95)
    add_award(con, "Herederos del Marqués de Riscal SL", "Marqués de Riscal Reserva", 2018, medal="Silver",
              points=91)
    add_award(con, "Someone Else Wines", "Private Reserve Malbec", 2025)        # other producer
    add_award(con, "Trivento Bodegas y Viñedos S.A.", "Trivento Malbec", 2025, colour="white")  # colour differs
    result = importer.match_awards(con, today=dt.date(2026, 11, 12))
    assert result["linked"] == 2
    rows = con.execute(
        """SELECT a.name_raw, w.name, w.vintage, m.confirmed FROM award_match m
           JOIN award a ON a.id = m.award_id JOIN wine w ON w.id = m.wine_id ORDER BY a.id""").fetchall()
    assert [tuple(r) for r in rows] == [
        ("Trivento Private Reserve Malbec", "Trivento Private Reserve Malbec", 2025, 1),
        # other vintage: linked to that vintage, which feeds the track record
        ("Marqués de Riscal Reserva", "Marqués de Riscal Reserva", 2018, 1),
    ]
    # first import: no "new award" events
    assert con.execute("SELECT COUNT(*) FROM event WHERE kind='new_award'").fetchone()[0] == 0
    from wijnkoop import scores
    scores.recompute(con, dt.date(2026, 11, 12))
    r = con.execute("SELECT * FROM best_buys WHERE wine='Marqués de Riscal Reserva'").fetchone()
    assert r["score"] is None and r["other_vintages_awarded"] == 1 and r["other_vintages_avg"] is not None
    t = con.execute("SELECT * FROM best_buys WHERE wine='Trivento Private Reserve Malbec'").fetchone()
    assert t["score"] == round((95 + 44) / 1.5, 1)
    assert t["best_award"] == "IWC 2026 Gold 95 pts"


def test_new_award_event_after_first_import(tmp_path):
    con = db.connect(tmp_path / "t.db")
    shop_wine(con, 1, "Trivento Private Reserve Malbec", "Trivento", vintage=2025)
    add_award(con, "Other", "Something", 2020, found="2026-06-01")
    importer.match_awards(con, today=dt.date(2026, 6, 1))
    add_award(con, "Trivento Bodegas y Viñedos S.A.", "Trivento Private Reserve Malbec", 2025, points=95,
              found="2026-06-08")
    importer.match_awards(con, today=dt.date(2026, 6, 8))
    assert con.execute("SELECT COUNT(*) FROM event WHERE kind='new_award'").fetchone()[0] == 1


def test_award_calendar(tmp_path):
    assert importer.award_check_interval(dt.date(2026, 3, 10)) == 7
    assert importer.award_check_interval(dt.date(2026, 5, 25)) == 7
    assert importer.award_check_interval(dt.date(2026, 5, 2)) == 30
    assert importer.award_check_interval(dt.date(2026, 11, 12)) == 30
    assert importer.award_check_interval(dt.date(2026, 11, 25)) == 7
    con = db.connect(tmp_path / "t.db")
    now = dt.datetime(2026, 11, 12, 10)
    assert importer.awards_due(con, now)
    con.execute("INSERT INTO run_log (job, started, finished) VALUES ('awards', '2026-11-01T10:00:00', "
                "'2026-11-01T12:00:00')")
    assert not importer.awards_due(con, now)
    assert importer.awards_due(con, dt.datetime(2026, 11, 21, 10))  # busy season: weekly


def test_years_to_read(tmp_path):
    con = db.connect(tmp_path / "t.db")
    assert importer.years_to_read(con, "iwc", 2026) == [2023, 2024, 2025, 2026]
    add_award(con, "A", "B", 2020)
    con.execute("UPDATE award SET year=2023")
    # a year cut short (awards present, but not finished) is read again
    assert importer.years_to_read(con, "iwc", 2026) == [2023, 2024, 2025, 2026]
    con.execute("INSERT INTO award_import VALUES ('iwc', 2023, '2026-11-12')")
    assert importer.years_to_read(con, "iwc", 2026) == [2024, 2025, 2026]


class FakeClient:
    def __init__(self, pages):
        self.pages = pages
        self.asked = []

    def get(self, url):
        self.asked.append(url)
        return self.pages.get(url, "<html></html>")


def test_import_iwc_walks_pages(tmp_path, monkeypatch):
    con = db.connect(tmp_path / "t.db")
    html = (FIX / "iwc-2026-score95.html").read_text().replace("page=2&amp;", "nope")
    monkeypatch.setattr(iwc, "POINTS", [95, 96])
    client = FakeClient({iwc.list_url(2026, 95): html})
    assert importer.import_iwc(con, 2026, client=client) == 20
    assert client.asked == [iwc.list_url(2026, 95), iwc.list_url(2026, 96)]
    assert importer.import_iwc(con, 2026, client=client) == 0  # nothing twice


def test_matching_real_cases(tmp_path):
    """Cases seen when matching real IWC/CMB results against Loes's wine list."""
    con = db.connect(tmp_path / "t.db")
    shop_wine(con, 1, "Domaine de l'Amaurigue Rosé", "Domaine de l'Amaurigue", colour="rosé", vintage=2025)
    shop_wine(con, 2, "M. Chapoutier Côtes du Rhône", "M. Chapoutier", vintage=2024)
    shop_wine(con, 3, "Ondarre Rioja Gran Reserva", "Ondarre", vintage=2019)
    shop_wine(con, 4, "Les Eclats De Branas", "Château Branas Grand Poujeaux", vintage=2022)
    add_award(con, "Domaine de l'Amaurigue", "Domaine de l'Amaurigue", 2025, colour="rosé", source="cmb")
    add_award(con, "M. Chapoutier", "Côtes du Rhône Rouge", 2024, colour="red", source="cmb")
    add_award(con, "El Coto De Rioja", "Imaz Gran Reserva", 2019, colour="red", source="cmb")
    add_award(con, "Château Branaire Ducru", "Eclat de Branaire-Ducru", 2022, colour="red", source="cmb")
    importer.match_awards(con, today=dt.date(2026, 11, 12))
    rows = con.execute("""SELECT a.name_raw, w.name, m.confirmed FROM award_match m
                          JOIN award a ON a.id = m.award_id JOIN wine w ON w.id = m.wine_id ORDER BY a.id""")
    assert [tuple(r) for r in rows] == [
        ("Domaine de l'Amaurigue", "Domaine de l'Amaurigue Rosé", 1),
        ("Côtes du Rhône Rouge", "M. Chapoutier Côtes du Rhône", 1),
    ]


def test_colour_type_and_age_must_agree():
    c = importer.conflict
    assert c("Casa da Passarella A Descoberta Branco", None, "Casa da Passarella Dão A Descoberta Tinto", "red")
    assert c("A Descoberta Branco", None, "Casa da Passarella A Descoberta", "red")  # the wine's own colour
    assert c("Château Cheval Blanc", None, "Château Cheval Blanc Saint-Émilion", "red") is None  # a name, both
    assert c("Riesling Dry", "white", "Red Newt Cellars Riesling", "white", {"red", "newt"}) is None
    assert c("Domaine X Côtes du Rhône Rouge", None, "Domaine X Côtes du Rhône", "red") is None
    assert c("Bodegas X Rosado", None, "Bodegas X Rosé", "rosé") is None
    assert c("Weißer Burgunder Trocken", None, "Rotwein Cuvée", None)  # German
    assert c("Muga Gran Reserva", None, "Muga Reserva", "red")
    assert c("Muga Reserva", None, "Muga Riserva", "red") is None
    assert c("Taylor's 20 Year Old Tawny Port", None, "Taylor's Tawny Port 10 years", "red")
    assert c("Taylor's Tawny", None, "Taylor's Ruby", "red")
    assert c("Moët Brut Impérial", None, "Moët Impérial Demi-Sec", "white")
    assert c("Moët Brut Impérial", None, "Moët Impérial", "white") is None


def test_other_colour_is_not_linked_and_old_links_are_undone(tmp_path):
    con = db.connect(tmp_path / "t.db")
    shop_wine(con, 1, "Casa da Passarella Dão A Descoberta Tinto", "Casa da Passarella", vintage=2023)
    add_award(con, "Casa da Passarella", "A Descoberta Branco", 2025, points=95)
    add_award(con, "Casa da Passarella", "A Descoberta Tinto", 2022, points=90)
    importer.match_awards(con, today=dt.date(2026, 11, 12))
    assert [tuple(r) for r in con.execute(
        """SELECT a.name_raw, w.vintage FROM award_match m JOIN award a ON a.id = m.award_id
           JOIN wine w ON w.id = m.wine_id""")] == [("A Descoberta Tinto", 2022)]

    # A link made before this check: the Branco award on a 2025 Tinto made only to hold it
    branco = con.execute("SELECT id FROM award WHERE name_raw='A Descoberta Branco'").fetchone()[0]
    wine = importer.Matcher(con).wine_for(con.execute("SELECT cuvee_id FROM wine LIMIT 1").fetchone()[0], 2025)
    con.execute("INSERT INTO award_match (award_id, wine_id, match_score, confirmed) VALUES (?,?,1,1)",
                (branco, wine))
    con.commit()
    # A Branco from another shop now exists: the award belongs there
    p = GallProduct(url="https://www.gall.nl/x-9.html", product_id="9", title="Casa da Passarella A Descoberta Branco",
                    colour="white", volume_cl=75, price=11.99)
    p.specs = {"producer": "Casa da Passarella", "vintage": "2025"}
    save_product(con, 2, p, "2026-11-12")
    from wijnkoop import scores
    scores.recompute(con, dt.date(2026, 11, 12))
    assert con.execute("SELECT COUNT(*) FROM wine WHERE id=?", (wine,)).fetchone()[0] == 0  # the made-up 2025 Tinto
    assert con.execute(
        """SELECT w.name, w.vintage FROM award_match m JOIN wine w ON w.id = m.wine_id
           WHERE m.award_id=?""", (branco,)).fetchone()[:] == ("Casa da Passarella A Descoberta Branco", 2025)
