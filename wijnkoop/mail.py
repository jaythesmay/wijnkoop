"""Weekly overview by e-mail (Gmail with an app password).

Settings are read from /data/mail.ini on the NAS:

    [mail]
    smtp_host = smtp.gmail.com
    smtp_port = 465
    username = you@gmail.com
    app_password = abcd efgh ijkl mnop
    to = you+wijnkoop@gmail.com
    app_url = http://192.168.2.19:8001
"""
import configparser
import datetime as dt
import html
import logging
import os
import smtplib
from email.message import EmailMessage

from . import config

log = logging.getLogger("wijnkoop")

SECTIONS = [
    ("New awards for wines in the shop", "kind = 'new_award'"),
    ("Deals on award-winning wines", "kind IN ('promotion_start', 'price_drop', 'new_offer', 'back') "
                                     "AND score IS NOT NULL AND deal = 'yes'"),
    ("Price drops on award-winning wines", "kind = 'price_drop' AND score IS NOT NULL AND deal = 'no'"),
    ("New in the shop, with a score", "kind IN ('new_offer', 'back') AND score IS NOT NULL"),
    ("New vintage on the shop page", "kind = 'vintage_change' AND score IS NOT NULL"),
    ("Gone from the shop", "kind = 'gone' AND score IS NOT NULL"),
]
PER_SECTION = 15


def load_settings(path=None) -> dict | None:
    path = path or config.MAIL_SETTINGS
    if not path.exists():
        return None
    cp = configparser.ConfigParser()
    cp.read(path, encoding="utf-8")
    if "mail" not in cp:
        return None
    s = dict(cp["mail"])
    s.setdefault("smtp_host", "smtp.gmail.com")
    s.setdefault("smtp_port", "465")
    return s if s.get("username") and s.get("app_password") and s.get("to") else None


def save_settings(username: str, app_password: str, to: str, app_url: str = "", path=None):
    path = path or config.MAIL_SETTINGS
    cp = configparser.ConfigParser()
    cp["mail"] = {"smtp_host": "smtp.gmail.com", "smtp_port": "465", "username": username,
                  "app_password": app_password, "to": to, "app_url": app_url}
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        cp.write(f)
    os.chmod(path, 0o600)


def overview(con, today: dt.date) -> list[tuple[str, list]]:
    since = (today - dt.timedelta(days=7)).isoformat()
    out = []
    shown = set()  # each wine only once, in the first section it fits
    for title, where in SECTIONS:
        rows = con.execute(
            f"""SELECT * FROM (
                  SELECT e.kind, e.wine, e.vintage, e.shop, e.old_value, e.new_value, e.score, e.url,
                         b.price_per_bottle_at_6, b.promotion, b.value, COALESCE(b.deal, 'no') AS deal,
                         b.best_award, e.happened_on
                  FROM weekly_events e LEFT JOIN best_buys b ON b.url = e.url
                  WHERE e.happened_on > ?)
                WHERE {where}
                GROUP BY url
                ORDER BY score DESC, value DESC
                """, (since,)).fetchall()
        rows = [r for r in rows if r["url"] not in shown][:PER_SECTION]
        shown.update(r["url"] for r in rows)
        if rows:
            out.append((title, rows))
    return out


def _money(x):
    return f"€ {x:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".") if x is not None else ""


def render(sections, today: dt.date, app_url: str = "") -> tuple[str, str]:
    """Return (plain text, html)."""
    head = f"WijnKoop weekly overview, {today.strftime('%-d %B %Y')}"
    text = [head, ""]
    parts = [f"<h2 style='font-family:sans-serif'>{html.escape(head)}</h2>"]
    if not sections:
        text.append("Nothing new among the wines with a score this week.")
        parts.append("<p style='font-family:sans-serif'>Nothing new among the wines with a score this week.</p>")
    for title, rows in sections:
        text += [title, "-" * len(title)]
        parts.append(f"<h3 style='font-family:sans-serif;margin-bottom:4px'>{html.escape(title)}</h3>"
                     "<table style='font-family:sans-serif;font-size:14px;border-collapse:collapse'>")
        for r in rows:
            name = f"{r['wine']} {r['vintage'] or ''}".strip()
            detail = []
            if r["kind"] == "new_award" and r["new_value"]:
                detail.append(r["new_value"])
            elif r["best_award"]:
                detail.append(r["best_award"])
            if r["kind"] == "price_drop":
                detail.append(f"was {_money(float(r['old_value']))}")
            if r["promotion"]:
                detail.append(r["promotion"])
            price = _money(r["price_per_bottle_at_6"])
            score = f"{r['score']:g}" if r["score"] else ""
            text.append(f"* {name} ({r['shop']}) score {score}, {price} per bottle at 6. "
                        f"{'; '.join(detail)}\n  {r['url']}")
            parts.append(
                "<tr>"
                f"<td style='padding:3px 8px'><a href='{html.escape(r['url'] or '')}'>{html.escape(name)}</a>"
                f"<br><small>{html.escape(r['shop'] or '')} · {html.escape('; '.join(detail))}</small></td>"
                f"<td style='padding:3px 8px;text-align:right'><b>{score}</b></td>"
                f"<td style='padding:3px 8px;text-align:right;white-space:nowrap'>{price}</td></tr>")
        parts.append("</table>")
        text.append("")
    if app_url:
        text.append(f"Everything, including changes without a score: {app_url}/wijnkoop/weekly_overview")
        parts.append(f"<p style='font-family:sans-serif'><a href='{html.escape(app_url)}/wijnkoop/weekly_overview'>"
                     "Open the full weekly overview in WijnKoop</a> (only works at home)</p>")
    parts.append("<p style='font-family:sans-serif;color:#777;font-size:12px'>Score = combined award score; "
                 "price per bottle when you buy 6, promotion included.</p>")
    return "\n".join(text), "\n".join(parts)


def send_overview(con, today: dt.date | None = None, settings: dict | None = None, smtp_class=None) -> str:
    today = today or dt.date.today()
    settings = settings or load_settings()
    if not settings:
        return "no mail settings yet (run: python -m wijnkoop mail-setup)"
    text, html_body = render(overview(con, today), today, settings.get("app_url", "").rstrip("/"))
    msg = EmailMessage()
    msg["Subject"] = f"WijnKoop: week of {today.strftime('%-d %B')}"
    msg["From"] = settings["username"]
    msg["To"] = settings["to"]
    msg.set_content(text)
    msg.add_alternative(html_body, subtype="html")
    smtp_class = smtp_class or smtplib.SMTP_SSL
    with smtp_class(settings["smtp_host"], int(settings["smtp_port"]), timeout=60) as smtp:
        smtp.login(settings["username"], settings["app_password"].replace(" ", ""))
        smtp.send_message(msg)
    log.info("Weekly overview sent")
    return "sent"


def mail_slot(now: dt.datetime) -> dt.datetime:
    days = (now.weekday() - config.MAIL_WEEKDAY) % 7
    slot = (now - dt.timedelta(days=days)).replace(hour=config.MAIL_HOUR, minute=0, second=0, microsecond=0)
    if slot > now:
        slot -= dt.timedelta(days=7)
    return slot


def mail_due(con, now: dt.datetime) -> bool:
    """Due once per week from Wednesday 08:00; sent later if the NAS was off then."""
    if not config.MAIL_SETTINGS.exists():
        return False
    row = con.execute(
        "SELECT 1 FROM run_log WHERE job='mail' AND finished IS NOT NULL AND started >= ? LIMIT 1",
        (mail_slot(now).isoformat(timespec="seconds"),)).fetchone()
    return row is None


def run_mail(con, now: dt.datetime | None = None) -> str:
    now = now or dt.datetime.now()
    run_id = con.execute("INSERT INTO run_log (job, started) VALUES ('mail', ?)",
                         (now.isoformat(timespec="seconds"),)).lastrowid
    con.commit()
    result = send_overview(con, now.date())
    con.execute("UPDATE run_log SET finished=?, pages_ok=1, note=? WHERE id=?",
                (dt.datetime.now().isoformat(timespec="seconds"), result, run_id))
    con.commit()
    return result
