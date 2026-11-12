"""Store shop products in the database."""
import datetime as dt
import logging

from . import db
from .fetch import NotAllowed, PoliteClient
from .deals import parse_deal
from .normalize import cuvee_key, norm, wine_key
from .reference import competition_id, parse_award_text
from .shops import gall

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
             is_wine=excluded.is_wine""",
        (key, p.title, producer, norm(producer) or None, p.colour, p.specs.get("country"),
         p.specs.get("region"), p.specs.get("grapes"), p.category, int(p.is_wine)),
    )
    return con.execute("SELECT id FROM cuvee WHERE norm_key=?", (key,)).fetchone()["id"]


def _upsert_wine(con, p: gall.GallProduct, cuvee_id: int, vintage: int | None) -> int:
    key = wine_key(p.title, vintage)
    con.execute(
        """INSERT INTO wine (norm_key, cuvee_id, name, producer, vintage, colour, country, region, grapes,
                             style, alcohol)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(norm_key) DO UPDATE SET
             cuvee_id=excluded.cuvee_id,
             producer=COALESCE(excluded.producer, producer),
             colour=COALESCE(excluded.colour, colour),
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
    for source, result in p.expert_awards:
        parsed = parse_award_text(source, result)
        if not parsed:
            continue
        comp_id = competition_id(con, parsed["competition"])
        cur = con.execute(
            """INSERT OR IGNORE INTO award (competition_id, year, name_raw, producer_raw, medal, points,
                                            source, source_url, raw_text, found_on)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (comp_id, parsed["year"], p.title, p.specs.get("producer"), parsed["medal"], parsed["points"],
             "shop page", p.url, parsed["raw_text"], today),
        )
        if cur.rowcount:
            award_id = cur.lastrowid
            log_event(con, today, "new_award", shop_id, offer_id, wine_id, new=parsed["raw_text"])
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
            (award_id, wine_id),
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


def save_product(con, shop_id: int, p: gall.GallProduct, today: str):
    vintage = _int(p.specs.get("vintage"))
    vkey = str(vintage) if vintage else "nv"
    cuvee_id = _upsert_cuvee(con, p)
    wine_id = _upsert_wine(con, p, cuvee_id, vintage)

    per75 = round(p.price * 75 / p.volume_cl, 2) if p.price and p.volume_cl else None
    deal = parse_deal(p.promotion, p.price)
    factor = deal.factor if deal else 1.0
    effective = round(per75 * factor, 2) if per75 else None
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
    return offer_id


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
    all_wine_urls = list(wine_urls)
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
    if not limit and total and failed < total / 2:
        sitemap_ids = {gall.parse_url(u)["product_id"] for u in all_wine_urls}
        n = mark_gone(con, shop_id, sitemap_ids, today)
        log.info("Gall: %d wines are no longer on the site", n)
    con.execute(
        "UPDATE run_log SET finished=?, pages_ok=?, pages_failed=?, note=? WHERE id=?",
        (dt.datetime.now().isoformat(timespec="seconds"), ok, failed, f"{total} of {total} pages read", run_id),
    )
    con.commit()
    log.info("Gall: finished, %d ok, %d failed", ok, failed)
    from .scores import recompute

    recompute(con)
    return {"ok": ok, "failed": failed}
