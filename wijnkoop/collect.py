"""Store shop products in the database."""
import datetime as dt
import logging

from . import db
from .fetch import NotAllowed, PoliteClient
from .normalize import wine_key
from .shops import gall

log = logging.getLogger("wijnkoop")


def _int(value):
    try:
        return int(str(value).strip()[:4])
    except (TypeError, ValueError):
        return None


def save_product(con, shop_id: int, p: gall.GallProduct, today: str):
    vintage = _int(p.specs.get("vintage"))
    alcohol = gall._to_float(p.specs.get("alcohol"))
    key = wine_key(p.title, vintage)
    con.execute(
        """INSERT INTO wine (norm_key, name, producer, vintage, colour, country, region, grapes, style, alcohol)
           VALUES (?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(norm_key) DO UPDATE SET
             producer=COALESCE(excluded.producer, producer),
             colour=COALESCE(excluded.colour, colour),
             country=COALESCE(excluded.country, country),
             region=COALESCE(excluded.region, region),
             grapes=COALESCE(excluded.grapes, grapes),
             style=COALESCE(excluded.style, style),
             alcohol=COALESCE(excluded.alcohol, alcohol)""",
        (key, p.title, p.specs.get("producer"), vintage, p.colour, p.specs.get("country"),
         p.specs.get("region"), p.specs.get("grapes"), p.specs.get("style"), alcohol),
    )
    wine_id = con.execute("SELECT id FROM wine WHERE norm_key=?", (key,)).fetchone()["id"]
    per75 = round(p.price * 75 / p.volume_cl, 2) if p.price and p.volume_cl else None
    stock = None if p.in_stock_online is None else int(p.in_stock_online)
    con.execute(
        """INSERT INTO offer (shop_id, wine_id, shop_product_id, url, title, volume_cl, price,
                              price_per_75cl, promotion, in_stock_online, awards_count, first_seen, last_seen)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(shop_id, shop_product_id) DO UPDATE SET
             wine_id=excluded.wine_id, url=excluded.url, title=excluded.title,
             volume_cl=excluded.volume_cl, price=excluded.price, price_per_75cl=excluded.price_per_75cl,
             promotion=excluded.promotion, in_stock_online=excluded.in_stock_online,
             awards_count=excluded.awards_count, last_seen=excluded.last_seen""",
        (shop_id, wine_id, p.product_id, p.url, p.title, p.volume_cl, p.price, per75,
         p.promotion, stock, p.awards_count, today, today),
    )
    offer_id = con.execute(
        "SELECT id FROM offer WHERE shop_id=? AND shop_product_id=?", (shop_id, p.product_id)
    ).fetchone()["id"]
    con.execute(
        """INSERT INTO price_history (offer_id, seen_on, price, promotion) VALUES (?,?,?,?)
           ON CONFLICT(offer_id, seen_on) DO UPDATE SET price=excluded.price, promotion=excluded.promotion""",
        (offer_id, today, p.price, p.promotion),
    )


def _open_run(con, resume: bool):
    """Return (run_id, skip_since_date, ok, failed). Reuses a recent unfinished run when resuming."""
    if resume:
        row = con.execute(
            """SELECT id, started, pages_ok, pages_failed FROM run_log
               WHERE job='gall' AND finished IS NULL AND started >= ?
               ORDER BY started DESC LIMIT 1""",
            ((dt.datetime.now() - dt.timedelta(days=8)).isoformat(timespec="seconds"),),
        ).fetchone()
        if row:
            log.info("Gall: resuming the run that started %s", row["started"])
            return row["id"], row["started"][:10], row["pages_ok"] or 0, row["pages_failed"] or 0
    # Older unfinished runs will never be resumed: mark them.
    con.execute(
        "UPDATE run_log SET note='interrupted' WHERE job='gall' AND finished IS NULL"
    )
    started = dt.datetime.now().isoformat(timespec="seconds")
    run_id = con.execute("INSERT INTO run_log (job, started) VALUES ('gall', ?)", (started,)).lastrowid
    return run_id, None, 0, 0


def collect_gall(limit: int | None = None, con=None, resume: bool = True, client=None) -> dict:
    con = con or db.connect()
    con.execute("INSERT OR IGNORE INTO shop (name, website) VALUES (?, ?)", (gall.SHOP_NAME, gall.BASE))
    shop_id = con.execute("SELECT id FROM shop WHERE name=?", (gall.SHOP_NAME,)).fetchone()["id"]
    run_id, skip_since, ok, failed = _open_run(con, resume and not limit)
    con.commit()

    client = client or PoliteClient(gall.BASE)
    log.info("Gall: waiting %.0f s between pages", client.delay)
    urls = []
    for sm in gall.child_sitemaps(client.get(gall.SITEMAP_INDEX)):
        urls += gall.product_urls_from_sitemap(client.get(sm))
    # Only wines: the colour word sits in the address (rood / wit / rose).
    wine_urls = [u for u in urls if (gall.parse_url(u) or {}).get("colour")]
    log.info("Gall: %d products in sitemap, %d look like wine", len(urls), len(wine_urls))
    if limit:
        wine_urls = wine_urls[:limit]
    total = len(wine_urls)
    if skip_since:
        done = {
            r[0] for r in con.execute(
                "SELECT shop_product_id FROM offer WHERE shop_id=? AND last_seen >= ?", (shop_id, skip_since)
            )
        }
        wine_urls = [u for u in wine_urls if gall.parse_url(u)["product_id"] not in done]
        log.info("Gall: %d pages already read in this run, %d to go", total - len(wine_urls), len(wine_urls))
    already = total - len(wine_urls)
    if skip_since:
        ok = already  # pages read before the interruption count as done
    con.execute("UPDATE run_log SET note=? WHERE id=?", (f"{already} of {total} pages read", run_id))
    con.commit()

    today = dt.date.today().isoformat()
    for i, url in enumerate(wine_urls, already + 1):
        try:
            product = gall.parse_product(url, client.get(url))
            save_product(con, shop_id, product, today)
            ok += 1
        except NotAllowed:
            log.warning("robots.txt does not allow %s, skipped", url)
        except Exception as exc:  # keep going; one bad page must not stop the run
            failed += 1
            log.warning("Failed %s: %s", url, exc)
        if i % 25 == 0:
            con.execute("UPDATE run_log SET pages_ok=?, pages_failed=?, note=? WHERE id=?",
                        (ok, failed, f"{i} of {total} pages read", run_id))
            con.commit()
            log.info("Gall: %d / %d pages done", i, total)
    con.execute(
        "UPDATE run_log SET finished=?, pages_ok=?, pages_failed=?, note=? WHERE id=?",
        (dt.datetime.now().isoformat(timespec="seconds"), ok, failed, f"{total} of {total} pages read", run_id),
    )
    con.commit()
    log.info("Gall: finished, %d ok, %d failed", ok, failed)
    return {"ok": ok, "failed": failed}
