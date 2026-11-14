"""Store shop products in the database."""
import datetime as dt
import logging
import re
import sqlite3

from . import db, health, incremental
from .incremental import SitemapRecorder
from . import pace as pace_of
from .pace import ShopSaysWait
from .fetch import NotAllowed, PoliteClient
from .bottles import fix_volume
from .deals import effective_per_75cl, parse_deal
from .normalize import cuvee_key, norm, split_estate, wine_key
from .process import process_token
from .reference import competition_id, parse_award_text
from .shops import gall

AUTO, REVIEW = 88, 75  # same limits as for awards (awards/importer.py)

log = logging.getLogger("wijnkoop")


def _int(value):
    try:
        return int(str(value).strip()[:4])
    except (TypeError, ValueError):
        return None


def log_event(con, today, kind, shop_id=None, offer_id=None, wine_id=None, old=None, new=None, detail=None):
    con.execute(
        """INSERT INTO event (happened_on, kind, shop_id, offer_id, wine_id, old_value, new_value, detail)
           VALUES (?,?,?,?,?,?,?,?)""",
        (today, kind, shop_id, offer_id, wine_id,
         None if old is None else str(old), None if new is None else str(new), detail),
    )


def _upsert_cuvee(con, p: gall.GallProduct) -> int:
    key = cuvee_key(p.title)
    producer = p.specs.get("producer")
    con.execute(
        """INSERT INTO cuvee (norm_key, name, producer, producer_key, colour, country, region, grapes,
                              category, is_wine)
           VALUES (?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(norm_key) DO UPDATE SET
             producer=COALESCE(excluded.producer, producer),
             producer_key=COALESCE(excluded.producer_key, producer_key),
             colour=COALESCE(excluded.colour, colour),
             country=COALESCE(excluded.country, country),
             region=COALESCE(excluded.region, region),
             grapes=COALESCE(excluded.grapes, grapes),
             category=COALESCE(excluded.category, category),
             is_wine=COALESCE(cuvee.manual_is_wine, excluded.is_wine)""",
        (key, p.title, producer, norm(producer) or None, p.colour, p.specs.get("country"),
         p.specs.get("region"), p.specs.get("grapes"), p.category, int(p.is_wine)),
    )
    return con.execute("SELECT id FROM cuvee WHERE norm_key=?", (key,)).fetchone()["id"]


def _resolve_cuvee(con, p: gall.GallProduct, shop_id: int, matcher=None) -> int:
    """The cuvee for this shop product. A wine already known from ANOTHER shop under a slightly
    different name is recognised (same producer, nearly the same name) and reused, so prices of
    the same wine in different shops can be compared."""
    key = cuvee_key(p.title)
    row = con.execute("SELECT cuvee_id FROM cuvee_alias WHERE norm_key=?", (key,)).fetchone()
    if row:
        return row["cuvee_id"]
    if con.execute("SELECT 1 FROM cuvee WHERE norm_key=?", (key,)).fetchone() or matcher is None:
        return _upsert_cuvee(con, p)
    cid, score = matcher.best({"producer": p.specs.get("producer"), "name": p.title, "colour": p.colour})
    if cid and score >= AUTO:
        con.execute("INSERT OR IGNORE INTO cuvee_alias (norm_key, cuvee_id, name, shop_id, match_score) "
                    "VALUES (?,?,?,?,?)", (key, cid, p.title, shop_id, round(score, 1)))
        return cid
    new_id = _upsert_cuvee(con, p)
    if cid and score >= REVIEW:
        con.execute("INSERT OR IGNORE INTO same_wine_check (cuvee_id, other_cuvee_id, match_score) VALUES (?,?,?)",
                    (new_id, cid, round(score, 1)))
    return new_id


def _wine_for(con, p: gall.GallProduct, cuvee_id: int, vintage: int | None) -> int:
    """The wine row for this cuvee and vintage; created when it does not exist yet."""
    row = con.execute("SELECT id FROM wine WHERE cuvee_id=? AND COALESCE(vintage, 0)=COALESCE(?, 0) "
                      "ORDER BY id LIMIT 1", (cuvee_id, vintage)).fetchone()
    if row and con.execute("SELECT 1 FROM cuvee_alias WHERE norm_key=? AND cuvee_id=?",
                           (cuvee_key(p.title), cuvee_id)).fetchone():
        return row["id"]  # the same wine from another shop: keep the first shop's details
    return _upsert_wine(con, p, cuvee_id, vintage) if not row else _upsert_wine_by_id(con, p, row["id"])


def _upsert_wine_by_id(con, p: gall.GallProduct, wine_id: int) -> int:
    con.execute(
        """UPDATE wine SET producer=COALESCE(?, producer), colour=COALESCE(manual_colour, ?, colour), country=COALESCE(?, country),
             region=COALESCE(?, region), grapes=COALESCE(?, grapes), style=COALESCE(?, style),
             alcohol=COALESCE(?, alcohol) WHERE id=?""",
        (p.specs.get("producer"), p.colour, p.specs.get("country"), p.specs.get("region"), p.specs.get("grapes"),
         p.specs.get("style"), gall._to_float(p.specs.get("alcohol")), wine_id))
    return wine_id


def _upsert_wine(con, p: gall.GallProduct, cuvee_id: int, vintage: int | None) -> int:
    key = wine_key(p.title, vintage)
    con.execute(
        """INSERT INTO wine (norm_key, cuvee_id, name, producer, vintage, colour, country, region, grapes,
                             style, alcohol)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(norm_key) DO UPDATE SET
             cuvee_id=excluded.cuvee_id,
             producer=COALESCE(excluded.producer, producer),
             colour=COALESCE(wine.manual_colour, excluded.colour, colour),
             country=COALESCE(excluded.country, country),
             region=COALESCE(excluded.region, region),
             grapes=COALESCE(excluded.grapes, grapes),
             style=COALESCE(excluded.style, style),
             alcohol=COALESCE(excluded.alcohol, alcohol)""",
        (key, cuvee_id, p.title, p.specs.get("producer"), vintage, p.colour, p.specs.get("country"),
         p.specs.get("region"), p.specs.get("grapes"), p.specs.get("style"),
         gall._to_float(p.specs.get("alcohol"))),
    )
    return con.execute("SELECT id FROM wine WHERE norm_key=?", (key,)).fetchone()["id"]


def _save_awards(con, p: gall.GallProduct, wine_id: int, offer_id: int, shop_id: int, today: str):
    page_vintage = _int(p.specs.get("vintage"))
    cuvee_id = con.execute("SELECT cuvee_id FROM wine WHERE id=?", (wine_id,)).fetchone()["cuvee_id"]
    for award in p.expert_awards:
        source, result = award[0], award[1]
        rated_vintage = award[2] if len(award) > 2 else None
        parsed = parse_award_text(source, result)
        if not parsed:
            continue
        # A rating of another vintage (shops show older ones too) belongs to that vintage:
        # it then counts in the track record, not in this wine's own score.
        target = wine_id
        if rated_vintage and page_vintage and rated_vintage != page_vintage and cuvee_id:
            target = _wine_for(con, p, cuvee_id, rated_vintage)
        comp_id = competition_id(con, parsed["competition"])
        cur = con.execute(
            """INSERT OR IGNORE INTO award (competition_id, year, name_raw, producer_raw, vintage, medal, points,
                                            source, source_url, raw_text, found_on)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (comp_id, parsed["year"], p.title, p.specs.get("producer"), rated_vintage, parsed["medal"],
             parsed["points"], "shop page", p.url, parsed["raw_text"], today),
        )
        if cur.rowcount:
            award_id = cur.lastrowid
            log_event(con, today, "new_award", shop_id, offer_id, target, new=parsed["raw_text"])
        else:
            award_id = con.execute(
                """SELECT id FROM award WHERE source='shop page' AND source_url=? AND competition_id=?
                   AND COALESCE(year,0)=COALESCE(?,0) AND COALESCE(medal,'')=COALESCE(?,'')
                   AND COALESCE(points,0)=COALESCE(?,0) AND COALESCE(name_raw,'')=?""",
                (p.url, comp_id, parsed["year"], parsed["medal"], parsed["points"], p.title),
            ).fetchone()["id"]
        # The shop shows it on this wine's own page: a confirmed match.
        con.execute(
            "INSERT OR IGNORE INTO award_match (award_id, wine_id, match_score, confirmed) VALUES (?,?,1.0,1)",
            (award_id, target),
        )


def _change_events(con, today, shop_id, offer_id, wine_id, current, p, stock):
    if current["price"] is not None and p.price is not None and p.price != current["price"]:
        kind = "price_drop" if p.price < current["price"] else "price_rise"
        log_event(con, today, kind, shop_id, offer_id, wine_id, old=current["price"], new=p.price)
    if (current["promotion"] or None) != (p.promotion or None):
        if p.promotion:
            log_event(con, today, "promotion_start", shop_id, offer_id, wine_id,
                      old=current["promotion"], new=p.promotion)
        else:
            log_event(con, today, "promotion_end", shop_id, offer_id, wine_id, old=current["promotion"])
    if current["in_stock_online"] is not None and stock is not None and stock != current["in_stock_online"]:
        log_event(con, today, "back_in_stock" if stock else "out_of_stock", shop_id, offer_id, wine_id)


def _your_fixes(con, shop_id: int, p: gall.GallProduct):
    """What you decided on the data quality page holds when the shop is read again: the bottle
    size of an offer, and one name for a producer the shops spell in different ways."""
    fix = con.execute("SELECT volume_cl FROM offer_fix WHERE shop_id=? AND shop_product_id=?",
                      (shop_id, p.product_id)).fetchone()
    if fix and fix["volume_cl"]:
        p.volume_cl = fix["volume_cl"]
    producer = p.specs.get("producer")
    if producer:
        same = con.execute("SELECT producer FROM producer_alias WHERE from_key=?", (norm(producer),)).fetchone()
        if same:
            p.specs["producer"] = same["producer"]


def save_product(con, shop_id: int, p: gall.GallProduct, today: str, matcher=None):
    p.volume_cl = fix_volume(p.volume_cl, p.title, p.url)  # a half bottle or magnum named in the title
    _your_fixes(con, shop_id, p)
    vintage = _int(p.specs.get("vintage"))
    vkey = str(vintage) if vintage else "nv"
    cuvee_id = _resolve_cuvee(con, p, shop_id, matcher)
    wine_id = _wine_for(con, p, cuvee_id, vintage)

    per75 = round(p.price * 75 / p.volume_cl, 2) if p.price and p.volume_cl else None
    deal = parse_deal(p.promotion, p.price)
    factor = deal.factor if deal else 1.0
    effective = effective_per_75cl(p.price, p.volume_cl, p.promotion, p.member_price)
    stock = None if p.in_stock_online is None else int(p.in_stock_online)
    values = dict(
        wine_id=wine_id, url=p.url, title=p.title, category=p.category, volume_cl=p.volume_cl,
        price=p.price, price_per_75cl=per75, was_price=p.was_price, member_price=p.member_price,
        promotion=p.promotion, deal_type=deal.kind if deal else None,
        deal_min_qty=deal.min_qty if deal else None, deal_factor=factor if deal else None,
        effective_price_per_75cl=effective, in_stock_online=stock, awards_count=p.awards_count,
        shop_rating=p.shop_rating, last_seen=today,
    )

    current = con.execute(
        "SELECT * FROM offer WHERE shop_id=? AND shop_product_id=? AND is_current=1 ORDER BY id DESC LIMIT 1",
        (shop_id, p.product_id),
    ).fetchone()
    if current and current["vintage_key"] != vkey and current["vintage_checked"] and vkey != "nv":
        # Same shop page, new vintage: keep the old offer and its prices, start a new one.
        con.execute("UPDATE offer SET is_current=0 WHERE id=?", (current["id"],))
        log_event(con, today, "vintage_change", shop_id, current["id"], wine_id,
                  old=current["vintage_key"], new=vkey, detail=p.title)
        current = None
    elif current and current["vintage_key"] != vkey and current["vintage_checked"] and vkey == "nv":
        vkey = current["vintage_key"]  # vintage not shown this time: keep what we knew
        wine_id = current["wine_id"]
        values["wine_id"] = wine_id

    if current is None:
        old = con.execute(
            "SELECT id FROM offer WHERE shop_id=? AND shop_product_id=? AND vintage_key=?",
            (shop_id, p.product_id, vkey),
        ).fetchone()
        if old:  # this vintage was offered before on this page: reactivate it
            con.execute("UPDATE offer SET is_current=0 WHERE shop_id=? AND shop_product_id=?",
                        (shop_id, p.product_id))
            con.execute("UPDATE offer SET is_current=1 WHERE id=?", (old["id"],))
            current = con.execute("SELECT * FROM offer WHERE id=?", (old["id"],)).fetchone()

    if current is None:
        cols = ["shop_id", "shop_product_id", "vintage_key", "vintage_checked", "is_current",
                "first_seen", *values]
        offer_id = con.execute(
            f"INSERT INTO offer ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
            (shop_id, p.product_id, vkey, 1, 1, today, *values.values()),
        ).lastrowid
        log_event(con, today, "new_offer", shop_id, offer_id, wine_id, new=p.price, detail=p.title)
    else:
        offer_id = current["id"]
        sets = ", ".join(f"{k}=?" for k in values)
        con.execute(
            f"UPDATE offer SET {sets}, vintage_key=?, vintage_checked=1, gone_since=NULL WHERE id=?",
            (*values.values(), vkey, offer_id),
        )
        if current["gone_since"]:
            log_event(con, today, "back", shop_id, offer_id, wine_id, old=current["gone_since"])
        # The first read with the phase 2 parser is a baseline: no change events.
        if current["vintage_checked"]:
            _change_events(con, today, shop_id, offer_id, wine_id, current, p, stock)

    con.execute(
        """INSERT INTO price_history (offer_id, seen_on, price, promotion, was_price, effective_price_per_75cl,
                                      in_stock)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(offer_id, seen_on) DO UPDATE SET price=excluded.price, promotion=excluded.promotion,
             was_price=excluded.was_price, effective_price_per_75cl=excluded.effective_price_per_75cl,
             in_stock=excluded.in_stock""",
        (offer_id, today, p.price, p.promotion, p.was_price, effective, stock),
    )
    _save_awards(con, p, wine_id, offer_id, shop_id, today)
    _wine_name(con, wine_id, cuvee_id)
    return offer_id


def _wine_name(con, wine_id: int, cuvee_id: int):
    """Estate and wine apart: the wine's own name, without the producer at its start."""
    for table, row_id, sql in (
            ("wine", wine_id, "SELECT w.name, COALESCE(w.producer, c.producer) FROM wine w "
                              "LEFT JOIN cuvee c ON c.id = w.cuvee_id WHERE w.id=?"),
            ("cuvee", cuvee_id, "SELECT name, producer FROM cuvee WHERE id=?")):
        r = con.execute(sql, (row_id,)).fetchone()
        if r:
            con.execute(f"UPDATE {table} SET wine_name=? WHERE id=? AND wine_name IS NOT ?",
                        (split_estate(r[0], r[1])[1], row_id, split_estate(r[0], r[1])[1]))


def mark_gone(con, shop_id: int, product_ids_in_sitemap: set[str], today: str) -> int:
    """Offers whose page is no longer in the shop's sitemap are gone."""
    rows = con.execute(
        "SELECT id, wine_id, shop_product_id FROM offer WHERE shop_id=? AND is_current=1 AND gone_since IS NULL",
        (shop_id,),
    ).fetchall()
    gone = [r for r in rows if r["shop_product_id"] not in product_ids_in_sitemap]
    for r in gone:
        con.execute("UPDATE offer SET gone_since=? WHERE id=?", (today, r["id"]))
        log_event(con, today, "gone", shop_id, r["id"], r["wine_id"])
    return len(gone)


def _open_run(con, job: str, resume: bool):
    """Return (run_id, skip_since_date, ok, failed). Reuses a recent unfinished run when resuming."""
    if resume:
        row = con.execute(
            """SELECT id, started, pages_ok, pages_failed FROM run_log
               WHERE job=? AND finished IS NULL AND started >= ?
               ORDER BY started DESC LIMIT 1""",
            (job, (dt.datetime.now() - dt.timedelta(days=8)).isoformat(timespec="seconds")),
        ).fetchone()
        if row:
            log.info("%s: resuming the run that started %s", job, row["started"])
            con.execute("UPDATE run_log SET process=?, wait_until=NULL WHERE id=?", (process_token(), row["id"]))
            return row["id"], row["started"][:10], row["pages_ok"] or 0, row["pages_failed"] or 0
    # Older unfinished runs will never be resumed: mark them.
    con.execute("""UPDATE run_log SET note='broke off; not carried on, a later run read the shop again', process=NULL
                   WHERE job=? AND finished IS NULL""", (job,))
    started = dt.datetime.now().isoformat(timespec="seconds")
    run_id = con.execute("INSERT INTO run_log (job, started, process) VALUES (?, ?, ?)",
                         (job, started, process_token())).lastrowid
    return run_id, None, 0, 0


def failure_reason(exc: Exception) -> str:
    """A short reason that is the same for every page failing the same way: no addresses."""
    response = getattr(exc, "response", None)
    if response is not None and getattr(response, "status_code", None):
        return f"HTTP {response.status_code} {getattr(response, 'reason_phrase', '') or ''}".strip()
    text = str(exc).split("\n")[0]
    text = re.sub(r"https?://\S+", "<page>", text)
    return f"{type(exc).__name__}: {text}"[:200] if text else type(exc).__name__


def fetch_page(shop, client, url: str, prefetched: dict | None = None, pid: str | None = None) -> str:
    """The page a shop's parse_product reads: what the shop gave in bulk at the start of the run
    (prefetched), the product page itself, or what the shop's own fetch_page fetches for it (a
    Shopify shop's small product JSON instead of the full page)."""
    if prefetched and pid in prefetched:
        return prefetched.pop(pid)
    own = getattr(shop, "fetch_page", None)
    return own(client, url) if own else client.get(url)


def prefetch(shop, client) -> dict:
    """Shops that can give many products per request (Shopify's /products.json) do so once at the
    start; a failure there only means every page is read one by one as before."""
    bulk = getattr(shop, "prefetch", None)
    if not bulk:
        return {}
    try:
        got = bulk(client)
        log.info("%s: %d products read in bulk", shop.SHOP_NAME, len(got))
        return got
    except ShopSaysWait:
        raise
    except Exception as exc:
        log.warning("%s: reading in bulk failed (%s); reading page by page", shop.SHOP_NAME, exc)
        return {}


def make_client(shop):
    """A polite client at the shop's own pace. (No shop needs a headless browser any more: Wijnvoordeel
    and Wijnbeurs are read through their product service.)"""
    return PoliteClient(shop.BASE, min_delay=pace_of.normal(shop))


def collect_shop(shop, limit: int | None = None, con=None, resume: bool = True, client=None) -> dict:
    """Read every wine page of one shop (a module from wijnkoop.shops) into the database."""
    con = con or db.connect()
    name = shop.SHOP_NAME
    con.execute("INSERT OR IGNORE INTO shop (name, website) VALUES (?, ?)", (name, shop.BASE))
    shop_id = con.execute("SELECT id FROM shop WHERE name=?", (name,)).fetchone()["id"]
    run_id, skip_since, ok, failed = _open_run(con, shop.KEY, resume and not limit)
    con.commit()
    try:
        return _read_pages(shop, con, shop_id, run_id, skip_since, ok, failed, limit, client)
    except BaseException:
        # Not running any more: the run stays unfinished, so the next one carries on from here.
        con.rollback()
        con.execute("UPDATE run_log SET process=NULL WHERE id=?", (run_id,))
        con.commit()
        raise


def _read_pages(shop, con, shop_id, run_id, skip_since, ok, failed, limit, client) -> dict:
    from .awards.importer import Matcher

    name = shop.SHOP_NAME

    own_client = client is None
    try:
        client = client or make_client(shop)
        pace_of.start(con, shop, client)
        con.commit()
        log.info("%s: waiting %.0f s between pages", name, client.delay)
        recorder = SitemapRecorder(client)  # notes each product's <lastmod> in the sitemaps
        products = shop.list_products(recorder)
        prefetched = prefetch(shop, client)
    except ShopSaysWait as exc:
        return _pause(shop, con, run_id, client, exc, ok, failed, "", own_client)
    log.info("%s: %d product pages to read", name, len(products))
    all_ids = {pid for _, pid in products}
    if limit:
        products = products[:limit]
    total = len(products)
    if skip_since:
        done = {r[0] for r in con.execute(
            "SELECT shop_product_id FROM offer WHERE shop_id=? AND last_seen >= ?", (shop_id, skip_since))}
        done |= {r[0] for r in con.execute(
            "SELECT shop_product_id FROM skipped_page WHERE shop_id=? AND seen_on >= ?", (shop_id, skip_since))}
        products = [(u, pid) for u, pid in products if pid not in done]
        log.info("%s: %d pages already read in this run, %d to go", name, total - len(products), len(products))
    today_date = dt.date.today()
    full = bool(limit) or bool(prefetched) or incremental.full_due(con, shop.KEY, today_date)
    carried = 0
    if not full:
        # Pages that did not change since they were read: not opened again (see incremental.py)
        same = incremental.unchanged(con, shop_id, products, recorder.lastmod)
        if same:
            carried = incremental.carry_forward(con, shop_id, same, today_date.isoformat())
            products = [(u, pid) for u, pid in products if pid not in same]
            log.info("%s: %d pages unchanged since they were read, %d to read", name, len(same), len(products))
    already = total - len(products)
    if skip_since:
        ok = already  # pages read before the interruption count as done
    else:
        ok += carried
    con.execute("UPDATE run_log SET note=? WHERE id=?", (f"{already} of {total} pages read", run_id))
    con.commit()

    # The same wine sold by another shop under a slightly different name is recognised.
    matcher = Matcher(con, other_than_shop=shop_id)
    is_wine = getattr(shop, "is_wine_product", None)
    today = dt.date.today().isoformat()
    paused = None
    read = no_price = failed_here = 0  # for noticing a shop that changed its site (health.py)
    for i, (url, pid) in enumerate(products, already + 1):
        try:
            product = shop.parse_product(url, fetch_page(shop, client, url, prefetched, pid))
            read += 1
            if product.price is None:
                no_price += 1
            if is_wine and not is_wine(product):
                # glasses, tastings, spirits: remember the page so a resumed run skips it
                con.execute("INSERT OR REPLACE INTO skipped_page (shop_id, shop_product_id, seen_on) VALUES (?,?,?)",
                            (shop_id, pid, today))
            else:
                save_product(con, shop_id, product, today, matcher)
            # Commit every page, so the database is never held while the next page is fetched
            # and another run can write in between.
            con.commit()
            ok += 1
        except NotAllowed:
            log.warning("robots.txt does not allow %s, skipped", url)
        except ShopSaysWait as exc:
            # Not the page's fault: it is read when the run carries on after the pause.
            con.rollback()
            paused = exc
            break
        except Exception as exc:  # keep going; one bad page must not stop the run
            con.rollback()  # nothing half-saved from this page
            failed += 1
            failed_here += 1
            log.warning("Failed %s: %s", url, exc)
            # Keep the reason, so the app can show why pages failed without the container's log.
            try:
                con.execute("INSERT OR REPLACE INTO failed_page (run_id, url, reason, seen_on) VALUES (?,?,?,?)",
                            (run_id, url, failure_reason(exc), today))
                con.commit()
            except sqlite3.Error:  # the database itself is the problem: the log has it
                con.rollback()
        if i % 25 == 0:
            con.execute("UPDATE run_log SET pages_ok=?, pages_failed=?, note=? WHERE id=?",
                        (ok, failed, f"{i} of {total} pages read", run_id))
            con.commit()
            log.info("%s: %d / %d pages done", name, i, total)
    health.check_run(con, shop, read, no_price, failed_here)
    if paused:
        return _pause(shop, con, run_id, client, paused, ok, failed, f"{i - 1} of {total} pages read; ", own_client)
    if own_client and hasattr(client, "close"):
        client.close()
    pace_of.after_run(con, shop, client)
    if not limit and total and failed < total / 2:
        n = mark_gone(con, shop_id, all_ids, today)
        log.info("%s: %d wines are no longer on the site", name, n)
    if full and not limit:
        incremental.note_full(con, shop.KEY, today_date)
    con.execute(
        "UPDATE run_log SET finished=?, pages_ok=?, pages_failed=?, note=?, process=NULL WHERE id=?",
        (dt.datetime.now().isoformat(timespec="seconds"), ok, failed, f"{total} of {total} pages read", run_id),
    )
    con.commit()
    log.info("%s: finished, %d ok, %d failed", name, ok, failed)
    _after_pages(con, name)
    return {"ok": ok, "failed": failed}


def _pause(shop, con, run_id, client, exc, ok, failed, read, own_client) -> dict:
    """The shop keeps answering "too many requests": stop asking. The run stays unfinished and
    carries on by itself after the pause, at a slower pace."""
    if own_client and hasattr(client, "close"):
        client.close()
    until = pace_of.pause(con, shop, client, exc)
    pace = pace_of.learned(con, shop.KEY)["delay"]
    note = (f"{read}paused: the shop answered HTTP {exc.status} (too many requests); carries on by itself "
            f"after {until:%a %d %b %H:%M}, one page every {pace:.0f} s")
    con.execute("UPDATE run_log SET pages_ok=?, pages_failed=?, note=?, process=NULL, wait_until=? WHERE id=?",
                (ok, failed, note, until.isoformat(timespec="seconds"), run_id))
    con.commit()
    log.warning("%s: %s", shop.SHOP_NAME, note)
    if ok:
        _after_pages(con, shop.SHOP_NAME)
    return {"ok": ok, "failed": failed, "paused": until}


def _after_pages(con, name):
    from .awards.upload import relink_uploads
    from .scores import mark_dirty

    n = relink_uploads(con)
    if n:
        log.info("%s: %d awards from your uploaded results files now linked to new wines", name, n)
    # The scores are worked out once when no shop is being read any more (the scheduler does it),
    # not after every shop.
    mark_dirty(con)


def collect_gall(limit: int | None = None, con=None, resume: bool = True, client=None) -> dict:
    return collect_shop(gall, limit=limit, con=con, resume=resume, client=client)
