import datetime as dt
from pathlib import Path

from wijnkoop import cli, config, db
from wijnkoop.collect import collect_gall

FIX = Path(__file__).parent / "fixtures"
PAGE = (FIX / "gall-product.html").read_text()
SITEMAP = """<urlset>
<url><loc>https://www.gall.nl/wijn-a-rood-75cl-100001.html</loc></url>
<url><loc>https://www.gall.nl/wijn-b-wit-75cl-100002.html</loc></url>
<url><loc>https://www.gall.nl/wijn-c-rood-75cl-100003.html</loc></url>
</urlset>"""


class FakeClient:
    delay = 0

    def __init__(self, fail_after=None):
        self.pages = []
        self.fail_after = fail_after

    def get(self, url):
        if url.endswith("sitemap_index.xml"):
            return "<sitemapindex><sitemap><loc>https://www.gall.nl/sitemap_0-product.xml</loc></sitemap></sitemapindex>"
        if url.endswith("sitemap_0-product.xml"):
            return SITEMAP
        if self.fail_after is not None and len(self.pages) >= self.fail_after:
            raise KeyboardInterrupt  # simulates the NAS switching off mid-run
        self.pages.append(url)
        return PAGE.replace("Catena Adrianna Malbec Fortuna Terrae", url.rsplit("/", 1)[1])


def test_interrupted_run_resumes(tmp_path):
    con = db.connect(tmp_path / "t.db")
    first = FakeClient(fail_after=2)
    try:
        collect_gall(con=con, client=first)
    except KeyboardInterrupt:
        pass
    con.commit()
    # Simulate the offers of the 2 read pages having been committed.
    assert con.execute("SELECT COUNT(*) FROM offer").fetchone()[0] == 2
    assert con.execute("SELECT finished FROM run_log").fetchone()[0] is None

    second = FakeClient()
    collect_gall(con=con, client=second)
    assert second.pages == ["https://www.gall.nl/wijn-c-rood-75cl-100003.html"]
    rows = con.execute("SELECT * FROM run_log").fetchall()
    assert len(rows) == 1 and rows[0]["finished"] is not None
    assert rows[0]["pages_ok"] == 3


def test_run_is_due(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUN_WEEKDAY", 0)
    monkeypatch.setattr(config, "RUN_HOUR", 1)
    con = db.connect(tmp_path / "t.db")
    thursday = dt.datetime(2026, 11, 12, 7, 0)
    assert cli.run_is_due(con, thursday) is False  # fresh install waits
    con.execute("INSERT INTO run_log (job, started) VALUES ('gall', '2026-11-11T21:21:16')")
    assert cli.run_is_due(con, thursday) is True  # interrupted run: resume
    con.execute("UPDATE run_log SET finished='2026-11-12T09:00:00'")
    assert cli.run_is_due(con, thursday) is False  # done for this week
    # NAS was off on Monday 01:00; it boots Monday 07:00: catch up.
    assert cli.run_is_due(con, dt.datetime(2026, 11, 16, 7, 0)) is True
