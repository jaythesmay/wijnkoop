"""Command line: python -m wijnkoop <command>"""
import argparse
import datetime as dt
import logging
import sys
import time

from . import config, db, jobs, scores, vivino
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


def cmd_collect(args):
    con = db.connect()
    if args.job:  # a run started with the button in the app
        jobs.run_job(con, args.job, collect_shop)
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
            busy = jobs.running_shops(con)  # a run started from the app
            due = [key for key in SHOPS
                   if run_is_due(con, now, key) and key not in busy and jobs.slot_free(con, key)]
            if due:
                collect_shop(SHOPS[due[0]], con=con)
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
            if vivino.weekly_due(con, now):
                vivino.run_weekly(con)
                scores.recompute(con)
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
        if vivino.SETTINGS.exists():
            vivino.SETTINGS.unlink()
        con = db.connect()
        con.execute("DELETE FROM vivino_rating")
        con.commit()
        print("Brave key and all stored Vivino ratings deleted.")
        return
    print("Vivino ratings via the Brave Search API. Make a key at https://api-dashboard.search.brave.com")
    print(f"WijnKoop uses at most {vivino.MONTHLY_LIMIT} searches a month.")
    key = getpass.getpass("Brave API key (not shown while typing): ").strip()
    if not key:
        print("No key given; nothing saved.")
        return
    vivino.save_key(key)
    print(f"Saved in {vivino.SETTINGS}. Test it with: python -m wijnkoop vivino-test")


def cmd_vivino_test(args):
    from . import vivino

    key = vivino.load_key()
    if not key:
        print("No Brave key yet. Run: python -m wijnkoop vivino-setup")
        return
    con = db.connect()
    ids = vivino.wines_to_test(con, args.count)
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


def main(argv=None):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )
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
    p.set_defaults(func=cmd_collect)
    p = sub.add_parser("check", help="test a few pages and show what was read")
    p.add_argument("--count", type=int, default=3)
    p.add_argument("--shop", default="gall", choices=list(SHOPS))
    p.set_defaults(func=cmd_check)
    sub.add_parser("scores", help="recompute scores and deals").set_defaults(func=cmd_scores)
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
    p = sub.add_parser("vivino-setup", help="save a Brave Search API key for Vivino ratings")
    p.add_argument("--forget", action="store_true", help="delete the key and every stored Vivino rating")
    p.set_defaults(func=cmd_vivino_setup)
    p = sub.add_parser("vivino-test", help="look up Vivino ratings of the best-scored wines (test)")
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
