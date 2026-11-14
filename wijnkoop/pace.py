"""How fast WijnKoop reads each shop, and what it learned from shops that asked it to slow down.

Every shop has its own pace (seconds between two pages):
- at least MIN_DELAY (5 s);
- longer when the shop's robots.txt sets a crawl-delay, or the shop module sets PACE;
- longer when the shop answered "429 Too Many Requests" (or 503) before: that slower pace is
  remembered per shop, and eased back by a quarter after every run without such an answer.

When a shop answers 429 the run waits (as long as the shop's Retry-After says, at least a minute),
goes on at a slower pace, and after three refusals for the same page stops for a few hours instead
of asking again and again. The run stays unfinished, so the scheduler carries it on by itself after
the pause, at the slower pace.
"""
import datetime as dt
import email.utils
import logging

from . import config

log = logging.getLogger("wijnkoop")

SLOWEST = 120.0  # seconds between pages, at most
EASE = 0.75  # after a run without a refusal, the learned pace moves this far back towards normal
CALM_PAGES = 200  # within a run: this many pages without a refusal and the pace eases a little
PAUSE_HOURS = [2, 6, 24]  # the 1st, 2nd and 3rd pause in a row (and every one after)
LONGEST_PAUSE = 48 * 3600  # never longer, whatever Retry-After says
WAIT_IN_RUN = 15 * 60  # a Retry-After longer than this pauses the run right away

SCHEMA = """
CREATE TABLE IF NOT EXISTS shop_pace (
    shop TEXT PRIMARY KEY,      -- the shop's key, as in wijnkoop.shops.SHOPS
    delay REAL,                 -- learned seconds between pages
    refusals INTEGER DEFAULT 0, -- 429/503 answers in total since the pace was last normal
    pauses INTEGER DEFAULT 0,   -- pauses in a row (0 after a run that finished)
    last_refusal TEXT,          -- when the shop last asked us to slow down
    last_status INTEGER,        -- 429 or 503
    robots_delay REAL           -- the crawl-delay in the shop's robots.txt at the last run
);
"""


class ShopSaysWait(Exception):
    """The shop keeps answering 429/503: stop reading it for a while."""

    def __init__(self, status: int, retry_after: float | None = None):
        self.status = status
        self.retry_after = retry_after
        super().__init__(f"HTTP {status}: the shop asks us to wait"
                         + (f" {int(retry_after)} s" if retry_after else ""))


def retry_after(value: str | None, now: dt.datetime | None = None) -> float | None:
    """Seconds from a Retry-After header: a number of seconds or a date."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if when is None:
        return None
    now = now or dt.datetime.now(dt.timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    return max(0.0, (when - now).total_seconds())


def ensure_table(con):
    con.executescript(SCHEMA)


def learned(con, key: str) -> dict | None:
    try:
        r = con.execute("SELECT * FROM shop_pace WHERE shop=?", (key,)).fetchone()
    except Exception:
        return None
    return dict(r) if r else None


def normal(shop, robots_delay: float = 0) -> float:
    """The shop's pace without anything learned: the minimum, its PACE, or robots.txt's crawl-delay."""
    return max(config.MIN_DELAY, float(getattr(shop, "PACE", 0) or 0), float(robots_delay or 0))


def start(con, shop, client) -> None:
    """Set the client's pace for a new or resumed run: the learned pace if it is slower."""
    if not hasattr(client, "delay"):
        return
    client.delay = max(client.delay, normal(shop))
    client.normal_delay = client.delay
    con.execute("INSERT INTO shop_pace (shop, robots_delay) VALUES (?, ?) "
                "ON CONFLICT(shop) DO UPDATE SET robots_delay=excluded.robots_delay",
                (shop.KEY, getattr(client, "robots_delay", 0) or 0))
    mem = learned(con, shop.KEY)
    if mem and mem["delay"] and mem["delay"] > client.delay:
        client.delay = min(SLOWEST, mem["delay"])
        log.info("%s: one page every %.0f s (slower than normal, %.0f s, since it asked us to slow down)",
                 shop.SHOP_NAME, client.delay, client.normal_delay)


def _save(con, key, delay, refusals, pauses, status=None, when=None):
    con.execute("""INSERT INTO shop_pace (shop, delay, refusals, pauses, last_refusal, last_status)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(shop) DO UPDATE SET delay=excluded.delay, refusals=excluded.refusals,
                       pauses=excluded.pauses,
                       last_refusal=COALESCE(excluded.last_refusal, shop_pace.last_refusal),
                       last_status=COALESCE(excluded.last_status, shop_pace.last_status)""",
                (key, round(delay, 1), refusals, pauses, when, status))


def after_run(con, shop, client) -> None:
    """A run finished: remember a slower pace if the shop refused pages, otherwise ease back."""
    if not hasattr(client, "delay"):
        return
    base = getattr(client, "normal_delay", normal(shop))
    mem = learned(con, shop.KEY)
    refused = getattr(client, "refusals", 0)
    if refused:
        _save(con, shop.KEY, max(client.delay, base), (mem["refusals"] if mem else 0) + refused, 0,
              getattr(client, "last_status", None), dt.datetime.now().isoformat(timespec="seconds"))
    elif mem and mem["delay"]:
        eased = max(base, mem["delay"] * EASE)
        if eased <= base * 1.05:  # back to normal
            con.execute("UPDATE shop_pace SET delay=NULL, refusals=0, pauses=0 WHERE shop=?", (shop.KEY,))
        else:
            _save(con, shop.KEY, eased, mem["refusals"] or 0, 0)
    elif mem and mem["pauses"]:
        con.execute("UPDATE shop_pace SET pauses=0 WHERE shop=?", (shop.KEY,))


def pause(con, shop, client, exc: ShopSaysWait, now: dt.datetime | None = None) -> dt.datetime:
    """The shop keeps refusing: remember a slower pace and say until when the run waits."""
    now = now or dt.datetime.now()
    mem = learned(con, shop.KEY)
    pauses = (mem["pauses"] if mem else 0) + 1
    seconds = PAUSE_HOURS[min(pauses, len(PAUSE_HOURS)) - 1] * 3600
    if exc.retry_after:
        seconds = max(seconds, exc.retry_after)
    seconds = min(seconds, LONGEST_PAUSE)
    delay = getattr(client, "delay", None) or normal(shop)
    delay = min(SLOWEST, max(delay, (mem["delay"] if mem else 0) or 0) * 1.5)
    _save(con, shop.KEY, delay, (mem["refusals"] if mem else 0) + max(1, getattr(client, "refusals", 0)),
          pauses, exc.status, now.isoformat(timespec="seconds"))
    return now + dt.timedelta(seconds=seconds)


def describe(con, shop) -> dict:
    """The pace for the page Read shops now: seconds between pages and why."""
    mem = learned(con, shop.KEY)
    robots_delay = (mem or {}).get("robots_delay") or 0
    base = normal(shop, robots_delay)
    why = []
    if robots_delay and robots_delay >= base:
        why.append("the shop's robots.txt asks for this")
    elif not getattr(shop, "PACE", 0) and base <= config.MIN_DELAY:
        why.append("WijnKoop's minimum; the shop's robots.txt asks for no more")
    elif getattr(shop, "PACE", 0) and float(shop.PACE) >= base:
        why.append(getattr(shop, "PACE_WHY", "set for this shop"))
    else:
        why.append("WijnKoop's minimum")
    out = {"normal": base, "now": base, "why": why[0], "learned": None}
    if mem and mem["delay"] and mem["delay"] > base:
        out["now"] = min(SLOWEST, mem["delay"])
        out["learned"] = (f"slower since the shop answered HTTP {mem['last_status'] or 429} "
                          f"on {(mem['last_refusal'] or '')[:16].replace('T', ' ')}; eases back after every run "
                          "without that answer")
    return out
