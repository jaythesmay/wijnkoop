"""Command line: python -m wijnkoop <command>"""
import argparse
import datetime as dt
import logging
import sys
import time

from . import config, db
from .collect import collect_gall
from .fetch import PoliteClient
from .shops import gall


def cmd_init(args):
    db.connect().close()
    print(f"Database ready at {config.DB_PATH}")


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
        print(f"  promotion: {p.promotion}  online in stock: {p.in_stock_online}  awards: {p.awards_count}")
        for k, v in sorted(p.specs.items()):
            print(f"  {k}: {v}")
    print("-" * 60)
    print(f"Raw pages saved in {config.SAMPLES_DIR}")


def _next_run(now: dt.datetime) -> dt.datetime:
    days = (config.RUN_WEEKDAY - now.weekday()) % 7
    run = (now + dt.timedelta(days=days)).replace(hour=config.RUN_HOUR, minute=0, second=0, microsecond=0)
    if run <= now:
        run += dt.timedelta(days=7)
    return run


def cmd_scheduler(args):
    log = logging.getLogger("wijnkoop")
    while True:
        run = _next_run(dt.datetime.now())
        log.info("Next Gall run: %s", run.strftime("%A %d %B %Y %H:%M"))
        while dt.datetime.now() < run:
            time.sleep(min(600, max(1, (run - dt.datetime.now()).total_seconds())))
        try:
            collect_gall()
        except Exception:
            log.exception("Gall run failed")


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
    sub.add_parser("scheduler", help="run the weekly schedule forever").set_defaults(func=cmd_scheduler)
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
