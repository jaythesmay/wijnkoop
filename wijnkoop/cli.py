"""Command line: python -m wijnkoop <command>"""
import argparse
import datetime as dt
import logging
import sys
import time

from . import config, db, scores
from .awards.importer import awards_due, match_awards, run_award_import
from .collect import collect_gall
from .mail import mail_due, run_mail, save_settings, send_overview
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
    """Forever: the weekly shop run first, award checks in between, then wait 10 minutes."""
    log = logging.getLogger("wijnkoop")
    announced = None
    while True:
        now = dt.datetime.now()
        con = db.connect()
        try:
            if run_is_due(con, now):
                collect_gall(con=con)
                match_awards(con)
                scores.recompute(con)
                continue
            if mail_due(con, now):
                try:
                    run_mail(con, now)
                except Exception:
                    # Wrong password or no internet: note it and try again in an hour.
                    log.exception("Weekly e-mail failed")
                    con.execute("UPDATE run_log SET note='failed, see log' WHERE job='mail' AND finished IS NULL")
                    con.commit()
                    time.sleep(3600)
                continue
            if awards_due(con, now):
                run_award_import(con)
                continue
        except Exception:
            log.exception("Scheduled job failed, trying again in 1 hour")
            time.sleep(3600)
            continue
        finally:
            con.close()
        nxt = _slot(now, 1)
        if announced != nxt:
            log.info("Next Gall run: %s", nxt.strftime("%A %d %B %Y %H:%M"))
            announced = nxt
        time.sleep(600)


def cmd_mail_setup(args):
    import getpass

    print("Weekly e-mail via Gmail. You need a Gmail app password (16 letters),")
    print("made at https://myaccount.google.com/apppasswords")
    username = input("Gmail address to send from: ").strip()
    to = input("Send the overview to (Enter = same address): ").strip() or username
    app_password = getpass.getpass("App password (not shown while typing): ").strip()
    app_url = input("Address of the app (Enter = http://192.168.2.19:8001): ").strip() or "http://192.168.2.19:8001"
    save_settings(username, app_password, to, app_url)
    print(f"Saved in {config.MAIL_SETTINGS}. Sending a test e-mail now...")
    con = db.connect()
    try:
        print(send_overview(con))
    except Exception as exc:
        print(f"Sending failed: {exc}")
        print("Check the app password and run mail-setup again.")
    con.close()


def cmd_mail(args):
    con = db.connect()
    print(send_overview(con))
    con.close()


def cmd_awards(args):
    con = db.connect()
    if args.match_only:
        print(match_awards(con, only_unmatched=False))
        scores.recompute(con)
    else:
        print(run_award_import(con, sources=args.source or ("iwc", "cmb")))
    con.close()


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
    p = sub.add_parser("awards", help="read IWC and Concours Mondial results now and link them to wines")
    p.add_argument("--source", action="append", choices=["iwc", "cmb"])
    p.add_argument("--match-only", action="store_true", help="only redo the linking to wines")
    p.set_defaults(func=cmd_awards)
    sub.add_parser("mail-setup", help="set up the weekly e-mail and send a test").set_defaults(func=cmd_mail_setup)
    sub.add_parser("mail", help="send the weekly overview e-mail now").set_defaults(func=cmd_mail)
    sub.add_parser("scheduler", help="run the weekly schedule forever").set_defaults(func=cmd_scheduler)
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
