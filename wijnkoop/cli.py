"""Command line: python -m wijnkoop <command>"""
import argparse
import datetime as dt
import logging
import sys
import time

from . import backup, config, db, jobs, personal, quality, scores, vivino
from .awards.importer import awards_due, match_awards, run_award_import
from .collect import collect_shop, fetch_page, make_client
from .mail import mail_due, run_mail, save_settings, send_overview
from .shops import SHOPS, gall


def cmd_init(args):
    con = db.connect()
    result = scores.recompute(con)
    con.close()
    print(f"Database ready at {config.DB_PATH} ({result['wines_scored']} wines with award scores)")


def cmd_scores(args):
    con = db.connect()
    print(scores.recompute(con))
    con.close()


def cmd_quality(args):
    """Go through the database for data quality problems (and look for award wines in the shops)."""
    from . import quality

    con = db.connect()
    if quality.running(con):
        print("The data quality checks are running already.")
        return
    print(quality.run_checks(con, look_for_awards=not args.quick))
    con.close()


def cmd_collect(args):
    con = db.connect()
    if args.job:  # a run started with the button in the app, or the weekly run: starts one process per shop
        jobs.run_job(con, args.job)
        return
    if args.one:  # one shop of such a run, in its own process
        collect_shop(SHOPS[args.one], con=con)
        con.close()
        return
    keys = [k for part in (args.shop or []) for k in part.split(",") if k]
    unknown = [k for k in keys if k not in SHOPS]
    if unknown:
        raise SystemExit(f"Unknown shop: {', '.join(unknown)}. Shops: {', '.join(SHOPS)}")
    status = {s["key"]: s for s in jobs.shop_status(con)["shops"]}
    if args.new:
        keys = [k for k, s in status.items() if not s["last"]]
    elif args.due:
        keys = [k for k, s in status.items() if s["state"] == "useful"]
    keys = keys or ([] if args.new or args.due else list(SHOPS))
    for key in keys:
        if status[key]["state"] in ("running", "queued"):
            print(f"{status[key]['name']}: already being read, skipped")
            continue
        jobs.wait_for_slot(con, key, say=print)
        collect_shop(SHOPS[key], limit=args.limit, con=con)
    if not keys:
        print("Nothing to read: every shop was read recently.")
    elif scores.recompute_if_due(con, busy=bool(jobs.running_shops(con))):
        print("Scores worked out again.")
    con.close()


def cmd_check(args):
    """Fetch a few wine pages of one shop, print what was understood, save the raw HTML."""
    shop = SHOPS[args.shop]
    client = make_client(shop)
    wine_urls = [u for u, _ in shop.list_products(client)][: args.count]
    config.SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    for url in wine_urls:
        html = fetch_page(shop, client, url)
        p = shop.parse_product(url, html)
        (config.SAMPLES_DIR / f"{shop.KEY}-{p.product_id}.html").write_text(html, encoding="utf-8")
        print("-" * 60)
        print(f"{p.title}\n  url: {url}\n  price: {p.price}  volume: {p.volume_cl} cl  colour: {p.colour}")
        print(f"  promotion: {p.promotion}  was: {p.was_price}  Premium price: {p.member_price}")
        print(f"  online in stock: {p.in_stock_online}  awards: {p.awards_count}  category: {p.category}"
              f"  wine: {'yes' if p.is_wine else 'no'}")
        for award in p.expert_awards:
            print(f"  expert: {' - '.join(str(x) for x in award)}")
        for k, v in sorted(p.specs.items()):
            print(f"  {k}: {v}")
        if p.awards_count and shop is gall:
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


def run_is_due(con, now: dt.datetime, job: str = "gall") -> bool:
    """Due when no run of this shop has finished since the latest scheduled moment.

    This also catches up on a run that was missed because the NAS was off,
    and carries on a run that broke off (a restart or an update), also one started from the app.
    """
    if jobs.paused_run(con, job, now):
        return False  # the shop asked us to wait; it carries on after the pause
    if jobs.resumable_run(con, job, now):
        return True
    row = con.execute(
        "SELECT 1 FROM run_log WHERE job=? AND finished IS NOT NULL AND started >= ? LIMIT 1",
        (job, _slot(now).isoformat(timespec="seconds")),
    ).fetchone()
    if row:
        return False
    unfinished = con.execute(
        "SELECT 1 FROM run_log WHERE job=? AND finished IS NULL LIMIT 1", (job,)
    ).fetchone()
    # A brand-new install waits for its first scheduled moment, unless a run was started and broke off.
    # A shop added later starts right away once any shop has been read before.
    any_finished = con.execute(
        "SELECT 1 FROM run_log WHERE job IN ({}) AND finished IS NOT NULL LIMIT 1".format(
            ",".join("?" * len(SHOPS))), list(SHOPS)
    ).fetchone()
    return bool(unfinished or any_finished)


def cmd_scheduler(args):
    """Forever: the weekly shop run first, award checks in between, then wait 10 minutes."""
    log = logging.getLogger("wijnkoop")
    announced = None
    while True:
        now = dt.datetime.now()
        con = db.connect()
        try:
            if jobs.resume_dead_jobs(con):  # runs from the app that a restart broke off
                time.sleep(30)  # let them claim their shops first
                continue
            # The shops that are due are read as one weekly run in its own process, as many at the
            # same time as "Shops read at once" allows. Shops being read or waiting in a run are left out.
            busy = set(jobs.running_shops(con)) | jobs.shops_in_live_jobs(con)
            due = [key for key in SHOPS
                   if run_is_due(con, now, key) and key not in busy and not jobs.failed_lately(con, key)]
            if due:
                job_id = jobs.start_weekly(con, due)
                log.info("Weekly run %s: %s", job_id, ", ".join(due))
            if jobs.weekly_running(con):  # the other jobs wait until the shops are read
                time.sleep(30)
                continue
            # Once, after the shops are read (or at most an hour after new prices came in).
            if scores.recompute_if_due(con, busy=bool(jobs.running_shops(con))):
                personal.run_alerts(con)  # watched wines at or below your target price
                continue
            if backup.run_if_due(con, now):
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
            if vivino.weekly_due(con, now):
                vivino.run_weekly(con)
                scores.mark_dirty(con)
                continue
            if quality.weekly_due(con, now) and not quality.running(con):
                quality.run_checks(con)
                continue
        except Exception:
            log.exception("Scheduled job failed, trying again in 1 hour")
            time.sleep(3600)
            continue
        finally:
            con.close()
        nxt = _slot(now, 1)
        if announced != nxt:
            log.info("Next shop run: %s", nxt.strftime("%A %d %B %Y %H:%M"))
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


def cmd_vivino_setup(args):
    import getpass

    from . import vivino

    if args.forget:
        for path in (vivino.SETTINGS, vivino.SETTINGS.with_name("brave.ini")):
            if path.exists():
                path.unlink()
        con = db.connect()
        con.execute("DELETE FROM vivino_rating")
        con.commit()
        print("Search key and all stored Vivino ratings deleted.")
        return
    print("Vivino ratings via the Tavily search API: free for 1,000 searches a month, no credit card.")
    print("Make a free account at https://app.tavily.com and copy your API key (it starts with tvly-).")
    print(f"WijnKoop uses at most {vivino.MONTHLY_LIMIT} searches a month.")
    key = getpass.getpass("Tavily API key (not shown while typing): ").strip()
    if not key:
        print("No key given; nothing saved.")
        return
    vivino.save_key(key)
    old = vivino.SETTINGS.with_name("brave.ini")
    if old.exists():
        old.unlink()  # the Brave key is not used any more
    print(f"Saved in {vivino.SETTINGS}.")
    print("A test with the first 3 wines of the weekly look-ups (3 of your free searches):")
    args.count = 3
    cmd_vivino_test(args)


def cmd_vivino_run(args):
    """This week's Vivino look-ups now (the button on Read shops now), not waiting for the scheduler."""
    from . import vivino

    con = db.connect()
    if vivino.running(con):
        print("The Vivino look-up is running already.")
        return
    print(vivino.run_weekly(con))
    scores.mark_dirty(con)
    con.close()


def cmd_vivino_test(args):
    from . import vivino

    key = vivino.load_key()
    if not key:
        print("No Tavily key yet. Run: python -m wijnkoop vivino-setup")
        return
    con = db.connect()
    ids = vivino.wines_to_look_up(con, args.count)  # the same order as the weekly look-ups
    found = 0
    for wine_id in ids:
        try:
            r = vivino.look_up(con, key, wine_id)
        except Exception as exc:  # show the reason (wrong key, limit) and stop
            print(f"Stopped: {exc}")
            break
        price = f", €{r['price']:.2f}" if r.get("price") else ""
        if r["rating"]:
            found += 1
            print(f"  {r['wine']}: {r['rating']:.1f} stars ({r['ratings_count'] or '?'} ratings, {r['how']}{price})"
                  f"  <- {r['vivino_title']}")
        elif price:
            print(f"  {r['wine']}: no rating{price}  <- {r['vivino_title']}")
        else:
            print(f"  {r['wine']}: no rating ({r['how']}, best match {r['match_score']:.0f})")
            if r.get("snippet"):
                print(f"      the search brought back {r['snippet']}")
        time.sleep(1.1)
    print(f"{found} of {len(ids)} wines got a Vivino rating. "
          f"Searches this month: {vivino.searches_this_month(con)} of {vivino.MONTHLY_LIMIT}.")
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


def cmd_import_results(args):
    from pathlib import Path

    from .awards import upload

    path = Path(args.file)
    con = db.connect()
    r = upload.import_upload(con, path.name, path.read_bytes(), args.competition, args.year)
    u = r["upload"]
    print(f"{u['competition']} {u['year'] or '(year unknown)'} from {u['kind']}: {u['how']}")
    print(f"{u['awards']} awards, {u['linked']} linked to shop wines, {u['to_review']} probable matches")
    for a in r["sample"][:5]:
        print("  ", " ".join(str(x) for x in (a["producer_raw"], a["name_raw"], a["vintage"], a["medal"],
                                              a["points"] and f"{a['points']:g} pts") if x))
    for check, n in sorted(r["counts"].items()):
        print(f"  {check}: {n}")
    if r["missing"]:
        print(f"  shop claims not in this file: {len(r['missing'])}")
    print(f"Details: /upload/{u['id']} in WijnKoop")


def _log_to_file():
    """Also keep the log in /data/logs/wijnkoop.log (5 files of 1 MB at most), so it survives an
    update of the container."""
    from logging.handlers import RotatingFileHandler

    try:
        config.LOG_DIR.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(config.LOG_DIR / "wijnkoop.log", maxBytes=1_000_000, backupCount=4,
                                      encoding="utf-8")
    except OSError:
        return
    handler.setFormatter(logging.Formatter("%(asctime)s %(process)d %(levelname)s %(message)s"))
    logging.getLogger("wijnkoop").addHandler(handler)


def main(argv=None):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )
    # Not a line for every page fetched: WijnKoop logs what matters itself (and every 429 once).
    logging.getLogger("httpx").setLevel(logging.WARNING)
    _log_to_file()
    parser = argparse.ArgumentParser(prog="wijnkoop")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="create the database").set_defaults(func=cmd_init)
    p = sub.add_parser("collect", help="read all wines of every shop (or one shop) now")
    p.add_argument("--shop", action="append",
                   help="only these shops, separated by commas: --shop vindict,vinocom (" + ", ".join(SHOPS) + ")")
    p.add_argument("--new", action="store_true", help="only the shops never read before")
    p.add_argument("--due", action="store_true", help="only the shops not read in the last two days")
    p.add_argument("--limit", type=int, default=None, help="only the first N wines (for testing)")
    p.add_argument("--job", type=int, help=argparse.SUPPRESS)
    p.add_argument("--one", choices=list(SHOPS), help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_collect)
    p = sub.add_parser("check", help="test a few pages and show what was read")
    p.add_argument("--count", type=int, default=3)
    p.add_argument("--shop", default="gall", choices=list(SHOPS))
    p.set_defaults(func=cmd_check)
    sub.add_parser("scores", help="recompute scores and deals").set_defaults(func=cmd_scores)
    p = sub.add_parser("quality", help="check the data for duplicates, wrong links and other problems")
    p.add_argument("--quick", action="store_true", help="skip looking for uploaded award wines in the shops")
    p.set_defaults(func=cmd_quality)
    p = sub.add_parser("awards", help="read IWC and Concours Mondial results now and link them to wines")
    p.add_argument("--source", action="append", choices=["iwc", "cmb"])
    p.add_argument("--match-only", action="store_true", help="only redo the linking to wines")
    p.set_defaults(func=cmd_awards)
    p = sub.add_parser("import-results", help="read an official results file (any format) and check the awards")
    p.add_argument("file")
    p.add_argument("--competition", help="short name, e.g. DWWA, IWSC, Mundus Vini (default: from the file)")
    p.add_argument("--year", type=int)
    p.set_defaults(func=cmd_import_results)
    sub.add_parser("mail-setup", help="set up the weekly e-mail and send a test").set_defaults(func=cmd_mail_setup)
    p = sub.add_parser("vivino-setup", help="save a Tavily API key for Vivino ratings (free: 1,000 searches a month)")
    p.add_argument("--forget", action="store_true", help="delete the key and every stored Vivino rating")
    p.set_defaults(func=cmd_vivino_setup)
    p = sub.add_parser("vivino-run", help="look up this week's Vivino ratings now")
    p.set_defaults(func=cmd_vivino_run)
    p = sub.add_parser("vivino-test", help="look up Vivino ratings of the first wines of the weekly look-ups now, and show what the search found")
    p.add_argument("--count", type=int, default=10)
    p.set_defaults(func=cmd_vivino_test)
    sub.add_parser("mail", help="send the weekly overview e-mail now").set_defaults(func=cmd_mail)
    sub.add_parser("scheduler", help="run the weekly schedule forever").set_defaults(func=cmd_scheduler)
    args = parser.parse_args(argv)
    if args.command in ("collect", "scheduler", "awards"):
        jobs.be_nice()
    args.func(args)


if __name__ == "__main__":
    main()
