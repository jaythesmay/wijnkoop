"""Command line: python -m wijnkoop <command>"""
import argparse
import datetime as dt
import logging
import sys
import time

from . import config, db, scores
from .collect import collect_gall
from .fetch import PoliteClient
from .shops import gall


def cmd_init(args):
    con = db.connect()
    result = scores.recompute(con)
    con.close()
    print(f"Database ready at {config.DB_PATH} ({result['wines_scored']} wines with award scores)")


def cmd_scores(args):
    con = db.connect()
    print(scores.recompute(con))
    con.close()


def cmd_collect(args):
    collect_gall(limit=args.limit)


def cmd_check(args):
    """Fetch a few Gall wine pages, print what was understood, save the raw HTML."""
    client = PoliteClient(gall.BASE)
    urls = []
    for sm in gall.child_sitemaps(client.get(gall.SITEMAP_INDEX)):
        urls += gall.product_urls_from_sitemap(client.get(sm))
        break
    wine_urls = [u for u in urls if (gall.parse_url(u) or {}).get("colour")][: args.count]
    config.SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    for url in wine_urls:
        html = client.get(url)
        p = gall.parse_product(url, html)
        (config.SAMPLES_DIR / f"gall-{p.product_id}.html").write_text(html, encoding="utf-8")
        print("-" * 60)
        print(f"{p.title}\n  url: {url}\n  price: {p.price}  volume: {p.volume_cl} cl  colour: {p.colour}")
        print(f"  promotion: {p.promotion}  was: {p.was_price}  Premium price: {p.member_price}")
        print(f"  online in stock: {p.in_stock_online}  awards: {p.awards_count}  category: {p.category}"
              f"  wine: {'yes' if p.is_wine else 'no'}")
        for source, result in p.expert_awards:
            print(f"  expert: {source} - {result}")
        for k, v in sorted(p.specs.items()):
            print(f"  {k}: {v}")
        if p.awards_count:
            # Gall loads the full award list separately. Save one reply to learn its format.
            awards_url = (gall.BASE + "/on/demandware.store/Sites-gall-nl-Site/nl_NL/Product-Awards"
                          f"?pid={p.product_id}&async=true")
            try:
                reply = client.get(awards_url)
                (config.SAMPLES_DIR / f"gall-{p.product_id}-awards.txt").write_text(reply, encoding="utf-8")
                print(f"  award list saved ({len(reply)} characters)")
            except Exception as exc:
                print(f"  award list not available: {exc}")
    print("-" * 60)
    print(f"Raw pages saved in {config.SAMPLES_DIR}")


def _slot(now: dt.datetime, offset_weeks: int = 0) -> dt.datetime:
    """The most recent scheduled moment at or before now (plus offset_weeks)."""
    days = (now.weekday() - config.RUN_WEEKDAY) % 7
    slot = (now - dt.timedelta(days=days)).replace(hour=config.RUN_HOUR, minute=0, second=0, microsecond=0)
    if slot > now:
        slot -= dt.timedelta(days=7)
    return slot + dt.timedelta(weeks=offset_weeks)


def run_is_due(con, now: dt.datetime) -> bool:
    """Due when no Gall run has finished since the latest scheduled moment.

    This also catches up on a run that was missed because the NAS was off,
    and resumes a run that was interrupted.
    """
    row = con.execute(
        "SELECT 1 FROM run_log WHERE job='gall' AND finished IS NOT NULL AND started >= ? LIMIT 1",
        (_slot(now).isoformat(timespec="seconds"),),
    ).fetchone()
    if row:
        return False
    unfinished = con.execute(
        "SELECT 1 FROM run_log WHERE job='gall' AND finished IS NULL LIMIT 1"
    ).fetchone()
    any_finished = con.execute(
        "SELECT 1 FROM run_log WHERE job='gall' AND finished IS NOT NULL LIMIT 1"
    ).fetchone()
    # A brand-new install waits for its first scheduled moment, unless a run was started and broke off.
    return bool(unfinished or any_finished)


def cmd_scheduler(args):
    log = logging.getLogger("wijnkoop")
    while True:
        now = dt.datetime.now()
        con = db.connect()
        due = run_is_due(con, now)
        con.close()
        if due:
            try:
                collect_gall()
                continue
            except Exception:
                log.exception("Gall run failed, trying again in 1 hour")
                time.sleep(3600)
                continue
        nxt = _slot(now, 1)
        log.info("Next Gall run: %s", nxt.strftime("%A %d %B %Y %H:%M"))
        while dt.datetime.now() < nxt:
            time.sleep(min(600, max(1, (nxt - dt.datetime.now()).total_seconds())))
        # Fresh scheduled run: start over instead of resuming.
        try:
            collect_gall(resume=False)
        except Exception:
            log.exception("Gall run failed, trying again in 1 hour")
            time.sleep(3600)


def main(argv=None):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )
    parser = argparse.ArgumentParser(prog="wijnkoop")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="create the database").set_defaults(func=cmd_init)
    p = sub.add_parser("collect", help="read all Gall wines now")
    p.add_argument("--limit", type=int, default=None, help="only the first N wines (for testing)")
    p.set_defaults(func=cmd_collect)
    p = sub.add_parser("check", help="test a few pages and show what was read")
    p.add_argument("--count", type=int, default=3)
    p.set_defaults(func=cmd_check)
    sub.add_parser("scores", help="recompute scores and deals").set_defaults(func=cmd_scores)
    sub.add_parser("scheduler", help="run the weekly schedule forever").set_defaults(func=cmd_scheduler)
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
