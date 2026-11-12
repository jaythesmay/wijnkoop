import datetime as dt
from pathlib import Path

from wijnkoop import config, db, mail, scores
from wijnkoop.collect import save_product
from wijnkoop.shops import gall

REAL = Path(__file__).parent / "fixtures" / "real"
URL = "https://www.gall.nl/veuve-clicquot-la-grande-dame-brut-wit-75cl-100137.html"


class FakeSMTP:
    sent = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def login(self, user, password):
        self.user, self.password = user, password

    def send_message(self, msg):
        FakeSMTP.sent.append((self.host, self.port, self.user, self.password, msg))


def test_overview_mail(tmp_path):
    con = db.connect(tmp_path / "t.db")
    p = gall.parse_product(URL, (REAL / "gall-100137.html").read_text())
    p.promotion = "2e fles 50% korting"
    save_product(con, 1, p, "2026-11-10")
    scores.recompute(con, dt.date(2026, 11, 10))
    settings = {"smtp_host": "smtp.gmail.com", "smtp_port": "465", "username": "me@example.com",
                "app_password": "abcd efgh ijkl mnop", "to": "me+w@example.com", "app_url": "http://nas:8001"}
    assert mail.send_overview(con, dt.date(2026, 11, 11), settings, smtp_class=FakeSMTP) == "sent"
    host, port, user, password, msg = FakeSMTP.sent[-1]
    assert (host, port, user, password) == ("smtp.gmail.com", 465, "me@example.com", "abcdefghijklmnop")
    assert msg["To"] == "me+w@example.com"
    body = msg.get_body(("plain",)).get_content()
    # the new award is the headline; the same wine is not repeated under deals
    assert "New awards for wines in the shop" in body
    assert body.count("Veuve Clicquot La Grande Dame Brut 2018") == 1
    assert "2e fles 50% korting" in body
    assert "http://nas:8001/wijnkoop/weekly_overview" in body
    html = msg.get_body(("html",)).get_content()
    assert "<table" in html and "Zilver met 90 punten" in html


def test_settings_file_and_due(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MAIL_SETTINGS", tmp_path / "mail.ini")
    monkeypatch.setattr(config, "MAIL_WEEKDAY", 2)
    monkeypatch.setattr(config, "MAIL_HOUR", 8)
    con = db.connect(tmp_path / "t.db")
    wed = dt.datetime(2026, 11, 18, 9, 0)
    assert not mail.mail_due(con, wed)  # no settings: never due
    mail.save_settings("me@example.com", "secret", "me+w@example.com", "http://nas:8001")
    assert (tmp_path / "mail.ini").stat().st_mode & 0o077 == 0  # only readable by the app
    assert mail.load_settings()["to"] == "me+w@example.com"
    assert mail.mail_due(con, wed)
    con.execute("INSERT INTO run_log (job, started, finished) VALUES ('mail', '2026-11-18T08:00:05', "
                "'2026-11-18T08:00:09')")
    assert not mail.mail_due(con, wed)
    assert not mail.mail_due(con, dt.datetime(2026, 11, 24, 20, 0))
    assert mail.mail_due(con, dt.datetime(2026, 11, 26, 7, 30))  # NAS was off on Wednesday: catch up


def test_mail_gives_up_after_three_failures(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MAIL_SETTINGS", tmp_path / "mail.ini")
    mail.save_settings("me@example.com", "wrong", "me@example.com")
    con = db.connect(tmp_path / "t.db")
    wed = dt.datetime(2026, 11, 18, 12, 0)
    for hour in (8, 9, 10):
        assert mail.mail_due(con, wed)
        con.execute("INSERT INTO run_log (job, started) VALUES ('mail', ?)", (f"2026-11-18T{hour:02d}:00:00",))
    assert not mail.mail_due(con, wed)
    assert mail.mail_due(con, dt.datetime(2026, 11, 25, 8, 30))
