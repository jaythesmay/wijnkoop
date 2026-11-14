"""The page that shows what WijnKoop is doing: processes, disk, running and waiting work."""
import asyncio
import datetime as dt
import os
from pathlib import Path

from wijnkoop import activity, db
from wijnkoop.process import process_token

ROOT = Path(__file__).parent.parent


def test_what_a_process_is():
    assert activity.what_is(["/usr/bin/python", "-m", "wijnkoop", "scheduler"])[0] == "scheduler"
    assert activity.what_is(["python", "-m", "wijnkoop", "collect", "--job", "7"]) == (
        "job", "Shop run started from the app (run 7)")
    assert activity.what_is(["/usr/local/bin/datasette", "serve", "/data/wijnkoop.db"])[0] == "web"
    assert activity.what_is(["/opt/pw-browsers/chromium/chrome", "--headless"])[0] == "browser"


def test_snapshot_shows_running_and_waiting_work(tmp_path, monkeypatch):
    from wijnkoop import config

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    con = db.connect(tmp_path / "wijnkoop.db")
    now = dt.datetime.now()
    me = process_token()  # this test plays a shop run that is reading Gall
    con.execute("INSERT INTO run_log (job, started, pages_ok, note, process) VALUES ('gall', ?, 12, 'reading', ?)",
                ((now - dt.timedelta(minutes=3)).isoformat(timespec="seconds"), me))
    con.execute("INSERT INTO app_job (shops, requested, process) VALUES ('gall,vinio', ?, ?)",
                (now.isoformat(timespec="seconds"), me))
    con.execute("INSERT INTO run_log (job, started, finished, pages_ok, note) VALUES ('awards', ?, ?, 4, 'done')",
                ((now - dt.timedelta(hours=2)).isoformat(timespec="seconds"),
                 (now - dt.timedelta(hours=1)).isoformat(timespec="seconds")))
    con.commit()
    snap = activity.snapshot(con, [{"text": "Upload iwsc.xlsx: waiting for the database", "waiting": True}])
    w = snap["work"]
    assert [r["job"] for r in w["running"]] == ["Gall & Gall: shop run"] and w["running"][0]["pid"] == os.getpid()
    assert w["queue"][0]["what"].startswith("Upload iwsc.xlsx") and "waiting for the database" in w["queue"][0]["why"]
    assert any(q["what"] == "Vinio: shop run" and "before it" in q["why"] for q in w["queue"])
    assert w["recent"][0]["job"] == "Award check" and w["recent"][0]["took"] == "1 h 00 min"
    mine = next(p for p in snap["sample"]["processes"] if os.getpid() in p["pids"])
    assert "Gall & Gall: shop run (reading)" in mine["doing"] and mine["rss"] > 0
    assert {f["name"] for f in snap["files"]["files"]} >= {"wijnkoop.db"}
    assert snap["machine"]["cpus"] >= 1 and snap["machine"]["disk_free"] > 0


def test_activity_pages(tmp_path):
    from datasette.app import Datasette

    db.connect(tmp_path / "wijnkoop.db").close()
    ds = Datasette([str(tmp_path / "wijnkoop.db")], plugins_dir=str(ROOT / "datasette" / "plugins"),
                   template_dir=str(ROOT / "datasette" / "templates"))

    async def run():
        return await ds.client.get("/activity"), await ds.client.get("/activity.json")

    page, data = asyncio.run(run())
    assert page.status_code == 200 and "Waiting, in the order it will start" in page.text
    d = data.json()
    assert {"sample", "machine", "files", "work"} <= set(d) and isinstance(d["work"]["queue"], list)


def test_container_log_leaves_out_requests_that_went_well(tmp_path, monkeypatch):
    import logging

    from datasette.app import Datasette

    db.connect(tmp_path / "wijnkoop.db").close()
    root = Path(__file__).parent.parent
    ds = Datasette([str(tmp_path / "wijnkoop.db")], plugins_dir=str(root / "datasette" / "plugins"),
                   template_dir=str(root / "datasette" / "templates"))
    asyncio.run(ds.invoke_startup())
    seen = []

    class Keep(logging.Handler):
        def emit(self, record):
            seen.append(record.getMessage())

    access = logging.getLogger("uvicorn.access")
    access.addHandler(handler := Keep())
    access.setLevel(logging.INFO)
    fmt = '%s - "%s %s HTTP/%s" %d'
    try:
        for status in (200, 302, 404, 500):
            access.info(fmt, "192.168.1.5:5000", "GET", "/activity.json", "1.1", status)
        assert seen == ['192.168.1.5:5000 - "GET /activity.json HTTP/1.1" 404',
                        '192.168.1.5:5000 - "GET /activity.json HTTP/1.1" 500']
        monkeypatch.setenv("WIJNKOOP_ACCESS_LOG", "all")
        access.info(fmt, "192.168.1.5:5000", "GET", "/update", "1.1", 200)
        assert seen[-1].endswith('"GET /update HTTP/1.1" 200')
        monkeypatch.setenv("WIJNKOOP_ACCESS_LOG", "none")
        access.info(fmt, "192.168.1.5:5000", "GET", "/update", "1.1", 500)
        assert len(seen) == 3
        assert sum(type(f).__name__ == "QuietAccessLog" for f in access.filters) == 1
    finally:
        access.removeHandler(handler)


def test_activity_and_read_shops_now_agree(tmp_path):
    """Loes's case: a run of three shops; Vino.com is being read, Colaris and Vanouds de Zwaan were
    paused by the shops (HTTP 429). Both pages say the same: one working, two paused."""
    from wijnkoop import jobs

    con = db.connect(tmp_path / "wijnkoop.db")
    now = dt.datetime.now()
    ts = lambda m: (now - dt.timedelta(minutes=m)).isoformat(timespec="seconds")
    later = (now + dt.timedelta(hours=2)).isoformat(timespec="seconds")
    con.execute("INSERT INTO app_job (shops, requested, process) VALUES (?, ?, ?)",
                ("colaris,vanoudsdezwaan,vinocom", ts(60), process_token()))
    for key, started in (("colaris", 55), ("vanoudsdezwaan", 40)):
        con.execute("INSERT INTO run_log (job, started, pages_ok, note, wait_until) VALUES (?, ?, 12, ?, ?)",
                    (key, ts(started), "paused: the shop answered HTTP 429", later))
    con.execute("INSERT INTO run_log (job, started, pages_ok, process) VALUES ('vinocom', ?, 30, ?)",
                (ts(20), process_token()))
    con.commit()

    states = {s["key"]: s["state"] for s in jobs.shop_status(con, now)["shops"]}
    assert (states["vinocom"], states["colaris"], states["vanoudsdezwaan"]) == ("running", "paused", "paused")
    work = activity.work(con, now)
    assert [r["job"] for r in work["running"]] == ["Vino.com: shop run"]
    waiting = [q["what"] for q in activity.queue(con, now)]
    assert "Colaris: shop run, paused" in waiting and "Vanouds de Zwaan: shop run, paused" in waiting
    assert not any(w.startswith("Vino.com") for w in waiting)
    # on Read shops now the paused shops show once, in their run, not also on their own
    status = jobs.shop_status(con, now)
    assert status["paused_alone"] == [] and len(status["paused"]) == 2
