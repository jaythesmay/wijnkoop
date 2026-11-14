"""Reading shops from the app: which shops make sense to read, no shop read twice at once."""
import asyncio
import datetime as dt
import re
import subprocess
import time
from pathlib import Path

import pytest

from wijnkoop import cli, config, db, jobs
from wijnkoop.collect import collect_gall
from wijnkoop.process import is_alive, process_token
from wijnkoop.shops import SHOPS

from test_resume import FakeClient

ROOT = Path(__file__).parent.parent
NOW = dt.datetime(2026, 11, 19, 15, 0)  # a Thursday; the weekly run is Monday 01:00


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUN_WEEKDAY", 0)
    monkeypatch.setattr(config, "RUN_HOUR", 1)
    con = db.connect(tmp_path / "wijnkoop.db")
    yield con
    con.close()


def _dead_token():
    p = subprocess.Popen(["true"])
    p.wait()
    return f"{p.pid}:1"


def _run(con, job, hours_ago, finished=True, process=None, note=None, now=NOW):
    started = (now - dt.timedelta(hours=hours_ago + 1)).isoformat(timespec="seconds")
    end = (now - dt.timedelta(hours=hours_ago)).isoformat(timespec="seconds") if finished else None
    con.execute("INSERT INTO run_log (job, started, finished, pages_ok, process, note) VALUES (?,?,?,?,?,?)",
                (job, started, end, 10, process, note))
    con.commit()


def _state(con, key, now=NOW):
    return next(s for s in jobs.shop_status(con, now)["shops"] if s["key"] == key)


def test_processes():
    assert is_alive(process_token())
    assert not is_alive(_dead_token())
    pid, _, ticks = process_token().partition(":")
    assert not is_alive(f"{pid}:{int(ticks) + 1}")  # the same number, another process
    assert not is_alive(None) and not is_alive("")


def test_what_makes_sense(con):
    assert _state(con, "gall")["state"] == "useful" and _state(con, "gall")["advice"] == "a first run reads all its wines"
    _run(con, "gall", hours_ago=3)
    s = _state(con, "gall")
    assert s["state"] == "fresh" and "3 hours ago" in s["advice"]
    _run(con, "vindict", hours_ago=24 * 5)
    assert _state(con, "vindict")["state"] == "useful"
    # Sunday evening: the weekly run of Monday 01:00 is close
    assert _state(con, "vindict", dt.datetime(2026, 11, 22, 20, 0))["state"] == "soon"
    _run(con, "vinocom", hours_ago=24 * 5)
    _run(con, "vinocom", hours_ago=1, finished=False, process=process_token(), note="40 of 900 pages read")
    s = _state(con, "vinocom")
    assert s["state"] == "running" and s["advice"] == "40 of 900 pages read"
    _run(con, "henribloem", hours_ago=1, finished=False, process=_dead_token(), note="40 of 900 pages read")
    s = _state(con, "henribloem")
    assert s["state"] == "resuming" and "carries on by itself" in s["advice"]  # a restart broke it off
    _run(con, "okhuysen", hours_ago=1, finished=False, note="stopped by you")
    s = _state(con, "okhuysen")
    assert s["state"] == "useful" and "you stopped" in s["advice"] and s["carry_on"]
    assert set(jobs.shop_status(con, NOW)["useful"]) == set(SHOPS) - {"gall", "vinocom", "henribloem"}


def test_broken_runs_carry_on_by_themselves(con, monkeypatch):
    # The weekly run of this week read Gall; then a run from the app broke off (an update)
    _run(con, "gall", hours_ago=24 * 2)
    _run(con, "gall", hours_ago=2, finished=False, process=_dead_token(), note="reading")
    assert jobs.resumable_run(con, "gall", NOW) and cli.run_is_due(con, NOW, "gall")
    # stopped with the button: not by itself
    con.execute("UPDATE run_log SET note='stopped by you' WHERE finished IS NULL")
    assert not jobs.resumable_run(con, "gall", NOW) and not cli.run_is_due(con, NOW, "gall")
    # too old to carry on
    con.execute("UPDATE run_log SET note='reading' WHERE finished IS NULL")
    assert not jobs.resumable_run(con, "gall", NOW + dt.timedelta(days=9))

    # A run from the app whose process died is started again, and skips what it had read
    monkeypatch.setattr(jobs.dt, "datetime", type("D", (dt.datetime,), {"now": staticmethod(lambda: NOW)}))
    monkeypatch.setattr("wijnkoop.awards.importer.match_awards", lambda con: None)
    job_id = con.execute("INSERT INTO app_job (shops, requested, process) VALUES ('vindict,vinocom', ?, ?)",
                         ((NOW - dt.timedelta(hours=3)).isoformat(timespec="seconds"), _dead_token())).lastrowid
    _run(con, "vindict", hours_ago=1)  # read before the restart
    jobs.close_dead_jobs(con)  # the page does this: it must not close a broken-off run
    assert [j["id"] for j in jobs.shop_status(con, NOW)["dead_jobs"]] == [job_id]
    started = []
    assert jobs.resume_dead_jobs(con, start=started.append) == [job_id] == started
    read = []
    jobs.run_job(con, job_id, lambda shop, con: read.append(shop.KEY))
    assert read == ["vinocom"]


def test_request_leaves_out_busy_and_fresh_shops(con, monkeypatch):
    monkeypatch.setattr(jobs.dt, "datetime", type("D", (dt.datetime,), {"now": staticmethod(lambda: NOW)}))
    _run(con, "gall", hours_ago=3)
    _run(con, "vinocom", hours_ago=1, finished=False, process=process_token())
    r = jobs.request_run(con, ["gall", "vinocom", "vindict"])
    assert r["shops"] == ["vindict"] and r["busy"] == ["vinocom"] and r["skipped"] == ["gall"]
    # vindict now waits in that run: asking again does not start it twice
    again = jobs.request_run(con, ["vindict"])
    assert again["job_id"] is None and again["busy"] == ["vindict"]
    forced = jobs.request_run(con, ["gall"], force=True)
    assert forced["shops"] == ["gall"]


def test_job_reads_its_shops_and_skips_one_read_elsewhere(con, monkeypatch):
    monkeypatch.setattr("wijnkoop.awards.importer.match_awards", lambda con: None)
    job_id = jobs.request_run(con, ["gall", "vindict"])["job_id"]
    _run(con, "vindict", hours_ago=0, finished=False, process=process_token())  # the scheduler took it
    read = []
    assert jobs.run_job(con, job_id, lambda shop, con: read.append(shop.KEY)) == ["gall"]
    j = con.execute("SELECT * FROM app_job WHERE id=?", (job_id,)).fetchone()
    assert j["finished"] and "vindict already running elsewhere" in j["note"]


def test_shop_run_notes_its_process_and_clears_it(con):
    try:
        collect_gall(con=con, client=FakeClient(fail_after=1))
    except KeyboardInterrupt:
        pass
    r = con.execute("SELECT finished, process FROM run_log").fetchone()
    assert r["finished"] is None and r["process"] is None  # broke off: not running, carries on later
    collect_gall(con=con, client=FakeClient())
    r = con.execute("SELECT finished, process FROM run_log").fetchone()
    assert r["finished"] and r["process"] is None


def test_stop(con):
    p = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        job_id = jobs.request_run(con, ["gall"])["job_id"]
        con.execute("UPDATE app_job SET process=? WHERE id=?", (process_token(p.pid), job_id))
        con.commit()
        assert jobs.stop_job(con, job_id)
        p.wait(timeout=5)
        assert con.execute("SELECT note FROM app_job WHERE id=?", (job_id,)).fetchone()[0] == "stopped"
    finally:
        p.kill()


def test_short_commands(con, monkeypatch):
    read = []
    monkeypatch.setattr(cli, "collect_shop", lambda shop, limit=None, con=None: read.append(shop.KEY))
    monkeypatch.setattr(db, "DB_PATH", Path(con.execute("PRAGMA database_list").fetchone()[2]))
    cli.main(["collect", "--shop", "vindict,vinocom"])
    assert read == ["vindict", "vinocom"]
    _run(con, "gall", hours_ago=24 * 30)
    read.clear()
    cli.main(["collect", "--new"])
    assert "gall" not in read and "vindict" in read
    with pytest.raises(SystemExit):
        cli.main(["collect", "--shop", "nope"])


def test_update_page(con, tmp_path, monkeypatch):
    from datasette.app import Datasette

    launched = []
    monkeypatch.setattr(jobs, "launch", launched.append)
    monkeypatch.setattr(jobs, "SOON_HOURS", 0)  # whatever day the test runs
    _run(con, "gall", hours_ago=24 * 6, now=dt.datetime.now())
    _run(con, "vindict", hours_ago=1, finished=False, process=_dead_token(), now=dt.datetime.now())  # broke off
    _run(con, "vinocom", hours_ago=1, finished=False, note="stopped by you", now=dt.datetime.now())
    con.close()
    ds = Datasette([str(tmp_path / "wijnkoop.db")], plugins_dir=str(ROOT / "datasette" / "plugins"),
                   template_dir=str(ROOT / "datasette" / "templates"))

    async def run():
        page = await ds.client.get("/update")
        token = re.search(r'name="csrftoken" value="([^"]+)"', page.text).group(1)
        cookies = {"ds_csrftoken": page.cookies.get("ds_csrftoken")}
        r = await ds.client.post("/update", data={"csrftoken": token, "shop": "gall"}, cookies=cookies)
        after = await ds.client.get("/update?started=gall")
        return page, r, after

    page, r, after = asyncio.run(run())
    assert page.status_code == 200 and "Read now" in page.text and "Gall &amp; Gall" in page.text
    assert "Going on now" in page.text and "carries on soon" in page.text and "carries on by itself" in page.text
    assert "Carry on</button>" in page.text and "location.reload()" in page.text
    assert r.status_code == 302 and "started=gall" in r.headers["location"]
    assert len(launched) == 1
    assert "Going on now" in after.text and "Started: Gall &amp; Gall" in after.text
    assert "location.reload()" in after.text


class Refusing(FakeClient):
    """Every second wine page answers 403, as a shop that blocks a visitor would."""

    def get(self, url):
        import httpx

        if url.endswith("100002.html"):
            request = httpx.Request("GET", url)
            raise httpx.HTTPStatusError("Client error '403 Forbidden' for url " + url, request=request,
                                        response=httpx.Response(403, request=request))
        return super().get(url)


def test_failed_pages_keep_their_reason(con):
    collect_gall(con=con, client=Refusing())
    r = con.execute("SELECT pages_ok, pages_failed FROM run_log").fetchone()
    assert (r["pages_ok"], r["pages_failed"]) == (2, 1)
    s = _state(con, "gall", dt.datetime.now())
    assert s["failures"] == [{"reason": "HTTP 403 Forbidden", "n": 1,
                              "example": "https://www.gall.nl/wijn-b-wit-75cl-100002.html"}]


def test_failure_reason_has_no_address():
    from wijnkoop.collect import failure_reason

    assert failure_reason(ValueError("could not convert 'abc' at https://x.nl/a")) == \
        "ValueError: could not convert 'abc' at <page>"


def test_limits_make_runs_wait(con, monkeypatch):
    # No shop needs a browser any more; the limit still works for one that would.
    for key in ("wijnvoordeel", "wijnbeurs"):
        monkeypatch.setattr(SHOPS[key], "NEEDS_BROWSER", True)
    browser = [k for k in SHOPS if jobs.needs_browser(k)]
    plain = [k for k in SHOPS if not jobs.needs_browser(k)]
    assert jobs.limits(con) == {"max_runs": 2, "max_browser_runs": 1}
    assert jobs.slot_free(con, plain[0]) and jobs.slot_free(con, browser[0])
    _run(con, browser[0], hours_ago=0, finished=False, process=process_token())
    assert not jobs.slot_free(con, browser[1])  # one browser run at a time
    assert jobs.slot_free(con, plain[0])
    _run(con, plain[1], hours_ago=0, finished=False, process=process_token())
    assert not jobs.slot_free(con, plain[0])  # two runs at a time
    jobs.set_limits(con, "3", "2")
    assert jobs.limits(con) == {"max_runs": 3, "max_browser_runs": 2}
    assert jobs.slot_free(con, plain[0]) and jobs.slot_free(con, browser[1])
    jobs.set_limits(con, "0", "abc")  # nonsense is ignored
    assert jobs.limits(con) == {"max_runs": 3, "max_browser_runs": 2}


def test_job_waits_for_a_free_place(con, monkeypatch):
    monkeypatch.setattr("wijnkoop.awards.importer.match_awards", lambda con: None)
    jobs.set_limits(con, "1", "1")
    blocker = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        _run(con, "vindict", hours_ago=0, finished=False, process=process_token(blocker.pid))
        job_id = jobs.request_run(con, ["gall"])["job_id"]
        sleeps = []

        def fake_sleep(s):
            sleeps.append(s)
            if len(sleeps) == 1:  # while waiting, the other run ends
                assert con.execute("SELECT note FROM app_job WHERE id=?", (job_id,)).fetchone()[0] == \
                    "waiting for a free place"
                assert _state(con, "gall", dt.datetime.now())["advice"] == "waiting until fewer shops are being read"
                blocker.kill()
                blocker.wait()

        monkeypatch.setattr(jobs.time, "sleep", fake_sleep)
        read = []
        assert jobs.run_job(con, job_id, lambda shop, con: read.append(shop.KEY)) == ["gall"]
        assert sleeps and read == ["gall"]
    finally:
        blocker.kill()
