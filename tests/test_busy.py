"""The web app waits for a shop run that is saving, instead of "database is locked"."""
import asyncio
import re
import sqlite3
import threading
import time
from pathlib import Path

from wijnkoop import db, scores
from wijnkoop.awards import upload

ROOT = Path(__file__).parent.parent
RESULTS = b"MUNDUS VINI 2025 RESULTS\nGOLD\nBodegas Muga Reserva 2021\n"


def _ds(path):
    from datasette.app import Datasette

    return Datasette([str(path)], plugins_dir=str(ROOT / "datasette" / "plugins"),
                     template_dir=str(ROOT / "datasette" / "templates"))


def test_web_writes_wait_for_another_writer(tmp_path, monkeypatch):
    path = tmp_path / "wijnkoop.db"
    db.connect(path).close()
    ds = _ds(path)
    other = sqlite3.connect(path, check_same_thread=False)
    other.execute("BEGIN IMMEDIATE")  # a shop run saving its scores

    def finish():
        time.sleep(6)  # longer than Datasette's own 5 seconds
        other.commit()

    threading.Thread(target=finish).start()

    async def run():
        database = ds.get_database("wijnkoop")
        timeout = await database.execute_write_fn(lambda c: c.execute("PRAGMA busy_timeout").fetchone()[0])
        await database.execute_write_fn(lambda c: c.execute("INSERT INTO watch (wine_id) VALUES (1)"))
        return timeout

    assert asyncio.run(run()) == db.BUSY_TIMEOUT * 1000
    other.close()


def test_upload_keeps_its_awards_when_scores_must_wait(tmp_path, monkeypatch):
    con = db.connect(tmp_path / "wijnkoop.db")

    def locked(con, today=None):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(scores, "recompute", locked)
    from wijnkoop import config

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    r = upload.import_upload(con, "mundus.txt", RESULTS, competition="Mundus Vini", year=2025)
    assert r["upload"]["awards"] == 1
    assert con.execute("SELECT COUNT(*) FROM award WHERE source='upload'").fetchone()[0] == 1


def test_busy_upload_says_so(tmp_path, monkeypatch):
    path = tmp_path / "wijnkoop.db"
    db.connect(path).close()

    def locked(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(upload, "import_upload", locked)
    ds = _ds(path)

    async def run():
        page = await ds.client.get("/upload")
        token = re.search(r'"x-csrftoken": "([^"]+)"', page.text).group(1)
        return await ds.client.post("/upload?filename=m.txt", content=RESULTS,
                                    headers={"x-csrftoken": token}, cookies={"ds_csrftoken": page.cookies.get("ds_csrftoken")})

    r = asyncio.run(run())
    assert r.status_code == 503 and "busy saving a shop run" in r.json()["error"]
