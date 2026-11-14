"""A pace per shop: slowing down and pausing when a shop answers 429, and carrying on later."""
import datetime as dt
import sqlite3
import time

import httpx
import pytest

from wijnkoop import cli, collect, db, jobs, pace
from wijnkoop.collect import collect_shop
from wijnkoop.fetch import PoliteClient
from wijnkoop.pace import ShopSaysWait
from wijnkoop.shops import vanoudsdezwaan, vindict, wijnvoordeel

from test_browser_shops import GAILLARD, TINAZZI, _page


@pytest.fixture
def con(tmp_path):
    con = db.connect(tmp_path / "wijnkoop.db")
    yield con
    con.close()


def _client(answers, monkeypatch, robots="User-agent: *\nAllow: /\n"):
    """A PoliteClient whose shop answers with these (status, headers) one after the other."""
    answers = list(answers)
    slept = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=robots)
        status, headers = answers.pop(0)
        return httpx.Response(status, headers=headers, text="{}")

    real = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    c = PoliteClient("https://shop.example", min_delay=5)
    return c, slept


def test_retry_after():
    assert pace.retry_after("120") == 120
    assert pace.retry_after(None) is None and pace.retry_after("soon") is None
    now = dt.datetime(2026, 11, 14, 8, 0, tzinfo=dt.timezone.utc)
    assert pace.retry_after("Sat, 14 Nov 2026 09:00:00 GMT", now) == 3600


def test_a_429_waits_and_slows_down(monkeypatch):
    c, slept = _client([(429, {}), (429, {"retry-after": "90"}), (200, {})], monkeypatch)
    assert c.get("https://shop.example/products/a.js") == "{}"
    assert c.refusals == 2 and c.last_status == 429
    assert slept[-2:] == [60, 120] or slept[-2] >= 60  # at least a minute, or what the shop asks
    assert max(slept) >= 90
    assert c.delay > 5  # the rest of the run goes slower


def test_three_refusals_pause_the_run(monkeypatch):
    c, _ = _client([(429, {})] * 3, monkeypatch)
    with pytest.raises(ShopSaysWait) as e:
        c.get("https://shop.example/products/a.js")
    assert e.value.status == 429


def test_a_long_retry_after_pauses_right_away(monkeypatch):
    c, slept = _client([(429, {"retry-after": "7200"})], monkeypatch)
    with pytest.raises(ShopSaysWait) as e:
        c.get("https://shop.example/products/a.js")
    assert e.value.retry_after == 7200 and not [s for s in slept if s >= 60]


def test_pace_eases_after_calm_pages(monkeypatch):
    c, _ = _client([(429, {})] + [(200, {})] * (pace.CALM_PAGES + 1), monkeypatch)
    c.get("https://shop.example/products/a.js")
    slow = c.delay
    for _ in range(pace.CALM_PAGES):
        c.get("https://shop.example/products/a.js")
    assert 5 <= c.delay < slow


def test_normal_pace_per_shop():
    assert pace.normal(vindict) == 10  # its robots.txt says 10
    assert pace.normal(vindict, robots_delay=30) == 30  # a longer crawl-delay read at the start wins
    assert pace.normal(vanoudsdezwaan) == 10  # answered 429 at 5 s


@pytest.fixture(autouse=True)
def html_pages(monkeypatch):
    """These tests read Wijnvoordeel's rendered pages, as before it was read through its product service."""
    monkeypatch.delattr(wijnvoordeel, "prefetch")
    monkeypatch.delattr(wijnvoordeel, "fetch_page")


class PausingClient:
    """The shop answers the second page with "too many requests" the first time."""
    delay = 5
    robots_delay = 0
    closed = False

    def __init__(self, refuse=True):
        self.refuse = refuse
        self.refusals = 0
        self.last_status = None

    def get(self, url):
        if url.endswith("products.xml") or url.endswith("sitemap.xml"):
            return f"<urlset><url><loc>{GAILLARD}</loc></url><url><loc>{TINAZZI}</loc></url></urlset>"
        if "tinazzi" in url and self.refuse:
            self.refusals += 3
            self.last_status = 429
            raise ShopSaysWait(429)
        return _page("wijnvoordeel-chateau-gaillard" if "gaillard" in url else "wijnvoordeel-tinazzi-rose")

    def close(self):
        self.closed = True


def test_a_paused_run_waits_then_carries_on_slower(con, monkeypatch):
    monkeypatch.setattr("wijnkoop.awards.importer.match_awards", lambda con: None)
    fake = PausingClient()
    monkeypatch.setattr(collect, "make_client", lambda shop: fake)
    r = collect_shop(wijnvoordeel, con=con)
    assert r["ok"] == 1 and r["failed"] == 0 and r["paused"] > dt.datetime.now()
    run = con.execute("SELECT * FROM run_log WHERE job='wijnvoordeel'").fetchone()
    assert run["finished"] is None and run["process"] is None and run["wait_until"]
    assert "paused" in run["note"] and "429" in run["note"]
    assert con.execute("SELECT COUNT(*) FROM failed_page").fetchone()[0] == 0  # not the page's fault
    learned = pace.learned(con, "wijnvoordeel")
    assert learned["delay"] > 5 and learned["pauses"] == 1 and learned["last_status"] == 429

    now = dt.datetime.now()
    assert jobs.paused_run(con, "wijnvoordeel", now) and not jobs.resumable_run(con, "wijnvoordeel", now)
    assert not cli.run_is_due(con, now, "wijnvoordeel")  # the scheduler leaves it alone
    s = next(s for s in jobs.shop_status(con, now)["shops"] if s["key"] == "wijnvoordeel")
    assert s["state"] == "paused" and "too many requests" in s["advice"] and "useful" != s["state"]
    assert s["pace"]["now"] == learned["delay"] and "429" in s["pace"]["learned"]

    later = dt.datetime.fromisoformat(run["wait_until"]) + dt.timedelta(minutes=1)
    assert jobs.resumable_run(con, "wijnvoordeel", later) and cli.run_is_due(con, later, "wijnvoordeel")

    # After the pause: the same run carries on with the page it did not get, at the learned pace
    calm = PausingClient(refuse=False)
    monkeypatch.setattr(collect, "make_client", lambda shop: calm)
    assert collect_shop(wijnvoordeel, con=con) == {"ok": 2, "failed": 0}
    assert calm.delay == learned["delay"]
    run = con.execute("SELECT * FROM run_log WHERE job='wijnvoordeel'").fetchone()
    assert run["finished"] and run["wait_until"] is None
    # a run without refusals eases the pace back towards normal
    assert pace.learned(con, "wijnvoordeel")["delay"] < learned["delay"]
    assert pace.learned(con, "wijnvoordeel")["pauses"] == 0


def test_pauses_get_longer(con):
    c = PausingClient()
    first = pace.pause(con, wijnvoordeel, c, ShopSaysWait(429))
    second = pace.pause(con, wijnvoordeel, c, ShopSaysWait(429))
    now = dt.datetime.now()
    assert (first - now).total_seconds() < (second - now).total_seconds()
    asked = pace.pause(con, wijnvoordeel, c, ShopSaysWait(429, retry_after=10 * 24 * 3600))
    assert (asked - now).total_seconds() <= pace.LONGEST_PAUSE + 5


def test_saving_limits_does_not_wait_for_the_database(tmp_path):
    con = db.connect(tmp_path / "wijnkoop.db")
    other = sqlite3.connect(tmp_path / "wijnkoop.db")
    other.execute("BEGIN IMMEDIATE")  # a shop run saving its scores holds the database
    other.execute("INSERT INTO setting (key, value) VALUES ('x', '1')")
    start = time.monotonic()
    assert jobs.set_limits(con, "4", "2") == {"max_runs": 4, "max_browser_runs": 2}
    assert time.monotonic() - start < 2
    assert jobs.limits(con) == {"max_runs": 4, "max_browser_runs": 2}
    assert (tmp_path / "limits.json").exists()
    other.rollback()
