"""What is yours: a target price per watched wine (with an alert), your own rating and note, and
your cellar (what you bought, how many bottles, until when to drink it).

Price alerts: after the scores are worked out, every watched wine with a target price is checked.
When its cheapest offer in stock (per bottle at 6, promotion included) is at or below the target,
WijnKoop notes an alert once for that price: on the watch list, in the weekly e-mail, and as a
push message on your phone when WIJNKOOP_NTFY_URL is set (a free ntfy.sh topic, e.g.
https://ntfy.sh/wijnkoop-<something only you know>; install the ntfy app and subscribe to it).
A cheaper price later alerts again; when the price goes back above the target, the next drop does too.
"""
import datetime as dt
import logging
import os

log = logging.getLogger("wijnkoop")

SCHEMA = """
CREATE TABLE IF NOT EXISTS my_wine (
    wine_id INTEGER PRIMARY KEY REFERENCES wine(id),
    rating INTEGER,          -- 1 to 5 stars
    note TEXT,
    updated TEXT
);
CREATE TABLE IF NOT EXISTS cellar (
    id INTEGER PRIMARY KEY,
    wine_id INTEGER NOT NULL REFERENCES wine(id),
    bottles INTEGER NOT NULL DEFAULT 1,
    price REAL,              -- per bottle, what you paid
    bought_on TEXT,
    drink_until INTEGER,     -- year
    note TEXT
);
CREATE INDEX IF NOT EXISTS ix_cellar_wine ON cellar(wine_id);
CREATE TABLE IF NOT EXISTS price_alert (
    id INTEGER PRIMARY KEY,
    wine_id INTEGER NOT NULL REFERENCES wine(id),
    price REAL,
    target REAL,
    shop TEXT,
    url TEXT,
    sent_on TEXT,
    pushed INTEGER DEFAULT 0
);
"""
WATCH_COLUMNS = [("target_price", "REAL"), ("alerted_price", "REAL")]


def ensure_tables(con):
    con.executescript(SCHEMA)
    have = {r[1] for r in con.execute("PRAGMA table_info(watch)")}
    for name, definition in WATCH_COLUMNS:
        if name not in have:
            con.execute(f"ALTER TABLE watch ADD COLUMN {name} {definition}")


# --- target prices and alerts ----------------------------------------------------------------

def set_target(con, wine_id: int, target) -> float | None:
    """Set (or clear, with nothing) the target price of a wine; watching it if it was not yet."""
    try:
        value = round(float(str(target).replace(",", ".")), 2) if str(target or "").strip() else None
    except ValueError:
        value = None
    if value is not None and value <= 0:
        value = None
    con.execute("INSERT INTO watch (wine_id, added) VALUES (?, ?) ON CONFLICT(wine_id) DO NOTHING",
                (wine_id, dt.datetime.now().isoformat()))
    con.execute("UPDATE watch SET target_price=?, alerted_price=NULL WHERE wine_id=?", (value, wine_id))
    con.commit()
    return value


def watched(con) -> list[dict]:
    """Every watched wine with its cheapest offer in stock now and its target."""
    rows = con.execute(
        """SELECT w.wine_id, w.target_price, w.alerted_price, wi.name || COALESCE(' ' || wi.vintage, '') AS wine,
                  (SELECT MIN(b.price_per_bottle_at_6) FROM best_buys b WHERE b.wine_id = w.wine_id
                     AND b.online_in_stock IS NOT 'no') AS cheapest,
                  (SELECT b.shop FROM best_buys b WHERE b.wine_id = w.wine_id AND b.online_in_stock IS NOT 'no'
                     ORDER BY b.price_per_bottle_at_6 LIMIT 1) AS shop
           FROM watch w JOIN wine wi ON wi.id = w.wine_id ORDER BY wi.name""").fetchall()
    return [dict(r) for r in rows]


def check_alerts(con, today: dt.date | None = None) -> list[dict]:
    """New alerts: watched wines at or below their target, cheaper than the last alert."""
    today = today or dt.date.today()
    out = []
    for w in con.execute(
            """SELECT w.wine_id, w.target_price, w.alerted_price,
                      wi.name || COALESCE(' ' || wi.vintage, '') AS wine
               FROM watch w JOIN wine wi ON wi.id = w.wine_id WHERE w.target_price IS NOT NULL""").fetchall():
        best = con.execute(
            """SELECT price_per_bottle_at_6 AS price, shop, url FROM best_buys
               WHERE wine_id = ? AND online_in_stock IS NOT 'no' AND price_per_bottle_at_6 IS NOT NULL
               ORDER BY price_per_bottle_at_6 LIMIT 1""", (w["wine_id"],)).fetchone()
        if not best or best["price"] > w["target_price"]:
            if w["alerted_price"] is not None:  # above the target again: the next drop alerts again
                con.execute("UPDATE watch SET alerted_price=NULL WHERE wine_id=?", (w["wine_id"],))
            continue
        if w["alerted_price"] is not None and best["price"] >= w["alerted_price"]:
            continue  # already told at this price
        con.execute("UPDATE watch SET alerted_price=? WHERE wine_id=?", (best["price"], w["wine_id"]))
        alert = {"wine_id": w["wine_id"], "wine": w["wine"], "price": best["price"], "target": w["target_price"],
                 "shop": best["shop"], "url": best["url"]}
        alert["id"] = con.execute(
            "INSERT INTO price_alert (wine_id, price, target, shop, url, sent_on) VALUES (?,?,?,?,?,?)",
            (w["wine_id"], best["price"], w["target_price"], best["shop"], best["url"], today.isoformat())).lastrowid
        out.append(alert)
    con.commit()
    return out


def push(alerts: list[dict], url: str | None = None, post=None) -> int:
    """Send each alert as a push message through ntfy (when WIJNKOOP_NTFY_URL is set)."""
    url = url or os.environ.get("WIJNKOOP_NTFY_URL")
    if not url or not alerts:
        return 0
    if post is None:
        import httpx

        post = httpx.post
    sent = 0
    for a in alerts:
        body = f"{a['wine']}: € {a['price']:.2f} per bottle at 6 at {a['shop']} (your target € {a['target']:.2f})"
        try:
            post(url, content=body.encode("utf-8"),
                 headers={"Title": "WijnKoop price alert", "Click": a.get("url") or "", "Tags": "wine_glass"},
                 timeout=20)
            sent += 1
        except Exception as exc:  # no internet: the alert is still on the watch list and in the e-mail
            log.warning("Push message failed: %s", exc)
    return sent


def run_alerts(con) -> list[dict]:
    alerts = check_alerts(con)
    if alerts:
        n = push(alerts)
        if n:
            con.executemany("UPDATE price_alert SET pushed=1 WHERE id=?", [(a["id"],) for a in alerts])
            con.commit()
        log.info("%d price alert%s", len(alerts), "s" if len(alerts) != 1 else "")
    return alerts


def recent_alerts(con, days: int = 7, today: dt.date | None = None) -> list[dict]:
    since = ((today or dt.date.today()) - dt.timedelta(days=days)).isoformat()
    return [dict(r) for r in con.execute(
        """SELECT a.*, wi.name || COALESCE(' ' || wi.vintage, '') AS wine FROM price_alert a
           JOIN wine wi ON wi.id = a.wine_id WHERE a.sent_on >= ? ORDER BY a.sent_on DESC, a.id DESC""", (since,))]


# --- your own rating and note ----------------------------------------------------------------

def mine(con, wine_id: int) -> dict:
    r = con.execute("SELECT rating, note FROM my_wine WHERE wine_id=?", (wine_id,)).fetchone()
    return dict(r) if r else {"rating": None, "note": None}


def set_mine(con, wine_id: int, rating, note) -> dict:
    stars = int(rating) if str(rating or "").isdigit() and 1 <= int(rating) <= 5 else None
    note = (note or "").strip()[:2000] or None
    if stars is None and note is None:
        con.execute("DELETE FROM my_wine WHERE wine_id=?", (wine_id,))
    else:
        con.execute("""INSERT INTO my_wine (wine_id, rating, note, updated) VALUES (?,?,?,?)
                       ON CONFLICT(wine_id) DO UPDATE SET rating=excluded.rating, note=excluded.note,
                       updated=excluded.updated""", (wine_id, stars, note, dt.datetime.now().isoformat()))
    con.commit()
    return {"rating": stars, "note": note}


# --- the cellar --------------------------------------------------------------------------------

def _int(value):
    return int(value) if str(value or "").strip().isdigit() else None


def _price(value):
    try:
        return round(float(str(value).replace(",", ".")), 2) if str(value or "").strip() else None
    except ValueError:
        return None


def add_to_cellar(con, wine_id: int, bottles, price=None, drink_until=None, note=None,
                  today: dt.date | None = None) -> int:
    n = _int(bottles) or 1
    until = _int(drink_until)
    cid = con.execute(
        "INSERT INTO cellar (wine_id, bottles, price, bought_on, drink_until, note) VALUES (?,?,?,?,?,?)",
        (wine_id, n, _price(price), (today or dt.date.today()).isoformat(),
         until if until and 1900 < until < 2200 else None, (note or "").strip()[:500] or None)).lastrowid
    con.commit()
    return cid


def set_bottles(con, cellar_id: int, bottles) -> None:
    n = _int(bottles)
    if not n:
        con.execute("DELETE FROM cellar WHERE id=?", (cellar_id,))
    else:
        con.execute("UPDATE cellar SET bottles=? WHERE id=?", (n, cellar_id))
    con.commit()


def cellar(con, today: dt.date | None = None) -> dict:
    """The cellar, what to drink first at the top (drink-until year soonest)."""
    year = (today or dt.date.today()).year
    rows = [dict(r) for r in con.execute(
        """SELECT c.*, wi.name || COALESCE(' ' || wi.vintage, '') AS wine, wi.colour, ws.overall AS score,
                  m.rating
           FROM cellar c JOIN wine wi ON wi.id = c.wine_id
           LEFT JOIN wine_score ws ON ws.wine_id = c.wine_id
           LEFT JOIN my_wine m ON m.wine_id = c.wine_id
           ORDER BY c.drink_until IS NULL, c.drink_until, wi.name""")]
    for r in rows:
        r["drink_soon"] = bool(r["drink_until"] and r["drink_until"] <= year + 1)
    return {"items": rows, "bottles": sum(r["bottles"] for r in rows),
            "value": round(sum((r["price"] or 0) * r["bottles"] for r in rows), 2)}


def in_cellar(con, wine_id: int) -> int:
    return con.execute("SELECT COALESCE(SUM(bottles), 0) FROM cellar WHERE wine_id=?", (wine_id,)).fetchone()[0]
