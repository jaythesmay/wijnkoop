"""Vivino ratings via the Tavily search API.

Vivino's terms forbid reading vivino.com with automated means, so WijnKoop never opens a
Vivino page. Instead it asks a search engine (Tavily, with Loes's own free key: 1,000 searches a
month, no credit card) for the wine on vivino.com and reads the rating and price from the text of
the search result. Only prices in euros are kept. (Until Nov 2026 this used the Brave Search API,
which dropped its free plan; results in Brave's shape are still understood.)

- The key lives in /data/tavily.ini (written by "python -m wijnkoop vivino-setup"), never in code.
- At most MONTHLY_LIMIT searches a month, so the free 1,000 a month are never exceeded.
- Every week the scheduler looks up at most WEEKLY wines (within the monthly limit): wines on the
  watchlist or in the order first, then wines on sale without any award, then the best scored.
  A rating is looked up again after RECHECK_DAYS.
- The Vivino rating (1-5 stars from the public) counts in the score as points = 50 + 10 x stars
  (4.0 is 90). How much it weighs depends on how many people rated it (Loes's rule):
  many ratings for this vintage: as much as a big critic (FULL); few for this vintage but many
  for the wine's other vintages or for the wine as a whole: a bit less (LESS); few everywhere:
  very lightly (LIGHT); under MIN_RATINGS: not at all. Never as "gold or better".
- The price is shown as a reference, not as an offer: it cannot be ordered through WijnKoop.
- "vivino-setup --forget" deletes the key and every stored Vivino rating.
"""
import configparser
import datetime as dt
import os
import re

import httpx
from rapidfuzz import fuzz

from . import config
from .normalize import norm

SETTINGS = config.DATA_DIR / "tavily.ini"
ENDPOINT = "https://api.tavily.com/search"
MONTHLY_LIMIT = int(os.environ.get("WIJNKOOP_VIVINO_MONTHLY_LIMIT", "900"))
MIN_MATCH = 80
WEEKLY = int(os.environ.get("WIJNKOOP_VIVINO_WEEKLY", "200"))
RECHECK_DAYS = 180
NO_RESULT_DAYS = 30  # a wine the search found nothing for is tried again after a month
# The price band looked up first: wines of EUR 9-15 a bottle (per 75 cl), Loes's sweet spot
FOCUS = (float(os.environ.get("WIJNKOOP_VIVINO_FROM", "9")), float(os.environ.get("WIJNKOOP_VIVINO_TO", "15")))
# Raised when the way of searching changes: look-ups that found nothing are then tried again.
LOOKUP_VERSION = 2
MANY = 250          # ratings: "many"
FULL, LESS, LIGHT = 1.0, 0.6, 0.15
MIN_RATINGS = 10

SCHEMA = """
CREATE TABLE IF NOT EXISTS vivino_rating (
    wine_id INTEGER PRIMARY KEY REFERENCES wine(id),
    wine TEXT,                 -- name + vintage, to check by eye
    rating REAL,               -- 1-5 stars, NULL when the result showed none
    ratings_count INTEGER,
    vivino_title TEXT,
    vivino_url TEXT,
    match_score REAL,
    how TEXT,                  -- 'structured', 'snippet' or 'no result'
    snippet TEXT,
    checked_on TEXT,
    price REAL,                -- euros per bottle as the search result showed it, NULL when none
    rating_vintage INTEGER     -- the vintage the rating is for; NULL: the wine as a whole (all vintages)
);
CREATE TABLE IF NOT EXISTS vivino_usage (month TEXT PRIMARY KEY, searches INTEGER NOT NULL DEFAULT 0);
"""


def ensure_tables(con):
    con.executescript(SCHEMA)
    cols = {r[1] for r in con.execute("PRAGMA table_info(vivino_rating)")}
    if "price" not in cols:  # databases from before prices were read
        con.execute("ALTER TABLE vivino_rating ADD COLUMN price REAL")
    if "rating_vintage" not in cols:
        con.execute("ALTER TABLE vivino_rating ADD COLUMN rating_vintage INTEGER")


def save_key(key: str, path=None):
    path = path or SETTINGS
    cp = configparser.ConfigParser()
    cp["tavily"] = {"api_key": key.strip()}
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        cp.write(f)
    os.chmod(path, 0o600)


def load_key(path=None) -> str | None:
    path = path or SETTINGS
    if not path.exists():
        return None
    cp = configparser.ConfigParser()
    cp.read(path)
    return cp.get("tavily", "api_key", fallback=None) or None


def searches_this_month(con, today: dt.date | None = None) -> int:
    month = (today or dt.date.today()).strftime("%Y-%m")
    row = con.execute("SELECT searches FROM vivino_usage WHERE month=?", (month,)).fetchone()
    return row[0] if row else 0


def _count_search(con, today: dt.date | None = None):
    month = (today or dt.date.today()).strftime("%Y-%m")
    con.execute("""INSERT INTO vivino_usage (month, searches) VALUES (?, 1)
                   ON CONFLICT(month) DO UPDATE SET searches = searches + 1""", (month,))


def search(key: str, query: str, client=None) -> dict:
    """One basic Tavily search (1 credit), vivino.com preferred. Only the result text Tavily gives;
    never the page itself (no raw content)."""
    client = client or httpx.Client(timeout=20)
    # vivino.com first, but other pages too: shops and reviews often quote the Vivino rating
    # ("4,1 op Vivino"), and Vivino's own pages are not always in the search index.
    r = client.post(ENDPOINT, json={"query": query, "search_depth": "basic", "max_results": 8,
                                    "include_domains": ["vivino.com"], "include_domains_mode": "prefer",
                                    "include_raw_content": False},
                    headers={"Authorization": f"Bearer {key}", "Accept": "application/json",
                             "User-Agent": config.USER_AGENT})
    r.raise_for_status()
    return r.json()


SNIPPET_RATING = [
    re.compile(r"(?:rating|beoordeling|rated|gemiddelde)[^0-9]{0,30}([1-5][.,]\d)\b", re.I),
    re.compile(r"\b([1-5][.,]\d)\s*(?:/\s*5|out of 5|van de 5|stars|sterren|★)", re.I),
    # The page text as a search result gives it: "4.2 3,456 ratings", "4,1 (512 beoordelingen)"
    re.compile(r"(?<![\d.,])([1-5][.,]\d)(?![\d.,]*\d)(?:\s+|\s*[(·|-]\s*)\d[\d.,]*\s*(?:ratings|beoordelingen|reviews)\b",
               re.I),
]
SNIPPET_COUNT = re.compile(r"([\d.,]+)\s*(?:ratings|beoordelingen|reviews)", re.I)


def _rating_from_text(text: str) -> tuple[float | None, int | None]:
    rating = None
    for rx in SNIPPET_RATING:
        m = rx.search(text)
        if m:
            rating = float(m.group(1).replace(",", "."))
            break
    count = None
    m = SNIPPET_COUNT.search(text)
    if m:
        digits = re.sub(r"[.,]", "", m.group(1))
        count = int(digits) if digits.isdigit() else None
    return rating, count


# Another page quoting the Vivino rating: "Vivino: 4,1", "Vivino rating 4.2 (1.234 beoordelingen)",
# "4.1 op Vivino", "4,0/5 on Vivino"
NOT_A_RATING = r"(?!\d|\s*(?:%|cl\b|l\b|ltr|liter|€|eur|euro))"
MENTION = [
    re.compile(r"vivino[^0-9<>]{0,40}?(?<![\d.,])([1-5][.,]\d)" + NOT_A_RATING, re.I),
    re.compile(r"(?<![\d.,])([1-5][.,]\d)" + NOT_A_RATING + r"\s*(?:/\s*5\s*)?(?:★\s*|stars?\s*|sterren\s*)?"
               r"(?:on|op|bij|auf|su|sur)\s+vivino", re.I),
]


def _mention(text: str) -> tuple[float | None, int | None]:
    """The Vivino rating another page quotes, and the number of ratings when it says so."""
    for rx in MENTION:
        m = rx.search(text)
        if m:
            rating = float(m.group(1).replace(",", "."))
            near = SNIPPET_COUNT.search(text[max(0, m.start() - 40):m.end() + 60])
            count = None
            if near:
                digits = re.sub(r"[.,]", "", near.group(1))
                count = int(digits) if digits.isdigit() else None
            return rating, count
    return None, None


def in_page(page: str | None) -> tuple[float, int | None] | None:
    """The Vivino rating a shop quotes in its own product description ("Vivino 4,1"), from the
    page a shop run reads anyway: no search needed. Only the wine's own description (the shop's
    product JSON, or the page's description meta and JSON-LD), not the rest of the page, where
    other wines may be shown with theirs."""
    import html
    import json

    if not page or "vivino" not in page.lower():
        return None
    texts = []
    if page.lstrip()[:1] == "{":
        try:
            d = json.loads(page)
            texts.append(str(d.get("description") or d.get("body_html") or ""))
        except ValueError:
            return None
    else:
        texts += re.findall(r"""<meta[^>]+(?:name|property)=["'](?:description|og:description)["'][^>]*content=["']([^"']*)""",
                            page, re.I)
        for block in re.findall(r"""<script[^>]+application/ld\+json[^>]*>(.*?)</script>""", page, re.I | re.S):
            texts += re.findall(r'"description"\s*:\s*"((?:[^"\\]|\\.)*)"', block)
    for t in texts:
        rating, count = _mention(re.sub(r"<[^>]+>", " ", html.unescape(t)))
        if rating:
            return rating, count
    return None


def save_from_shop(con, wine_id: int, shop: str, url: str, title: str, found: tuple, today: str):
    """Keep a rating a shop quotes, unless a search found one with its number of ratings."""
    ensure_tables(con)
    rating, count = found
    old = con.execute("SELECT rating, ratings_count, how FROM vivino_rating WHERE wine_id=?", (wine_id,)).fetchone()
    if old and old["rating"] and old["ratings_count"] and not (old["how"] or "").startswith(("quoted", "on the page")):
        return
    con.execute("""INSERT OR REPLACE INTO vivino_rating (wine_id, wine, rating, ratings_count, vivino_title, vivino_url,
                                                          match_score, how, checked_on)
                   VALUES (?, ?, ?, ?, ?, ?, 100, ?, ?)""",
                (wine_id, title, rating, count, title, url, f"on the page of {shop}", today))


EURO = r"(\d{1,4}(?:[.,]\d{2})?)"
SNIPPET_PRICE = [
    re.compile(r"(?:€|EUR)\s?" + EURO + r"(?![\d%])", re.I),
    re.compile(r"(?<![\d.,])" + EURO + r"\s?(?:€|EUR\b)", re.I),
]
PRICE_WORDS = re.compile(r"price|prijs|prix|preis|kopen|buy|from|vanaf", re.I)


def _euros(value) -> float | None:
    try:
        v = float(str(value).replace(",", "."))
    except ValueError:
        return None
    return round(v, 2) if 2 <= v <= 5000 else None


def _price_from_text(text: str) -> float | None:
    """The first euro price in the text; one next to a word such as "price" wins."""
    found = []
    for rx in SNIPPET_PRICE:
        for m in rx.finditer(text):
            v = _euros(m.group(1))
            if v:
                near = PRICE_WORDS.search(text[max(0, m.start() - 30):m.start()])
                found.append((0 if near else 1, m.start(), v))
    return min(found)[2] if found else None


def _price_from_structured(r: dict) -> float | None:
    offers = (r.get("product") or {}).get("offers") or r.get("offers") or []
    if isinstance(offers, dict):
        offers = [offers]
    for o in offers:
        if isinstance(o, dict) and (o.get("priceCurrency") or "EUR").upper() == "EUR":
            v = _euros(o.get("price") or o.get("lowPrice") or "")
            if v:
                return v
    return None


def candidates(result: dict) -> list[dict]:
    """Vivino wine pages in a search result (Tavily's, or Brave's from before), with any rating it carries."""
    out = []
    for r in (result.get("results") or []) + ((result.get("web") or {}).get("results") or []):
        url = r.get("url") or ""
        text = " ".join([r.get("content") or r.get("description") or ""] + list(r.get("extra_snippets") or []))
        text = re.sub(r"<[^>]+>", "", text)
        on_vivino = "vivino.com" in url
        if on_vivino and not re.search(r"/w/\d|/wines/\d", url):
            continue  # a search or list page, not one wine
        if not on_vivino:  # another page: only when it quotes the Vivino rating
            rating, count = _mention(text)
            if not rating:
                continue
            site = re.sub(r"^www\.", "", url.split("/")[2]) if url.count("/") >= 2 else url
            title = re.sub(r"\s*[|\-–]\s*[^|\-–]*$", "", r.get("title") or "").strip() or (r.get("title") or "")
            out.append({"title": title, "url": url, "rating": rating, "ratings_count": count,
                        "how": f"quoted on {site}", "snippet": text[:300], "price": None, "vintage": None})
            continue
        title = re.sub(r"\s*[|-]\s*Vivino.*$", "", r.get("title") or "", flags=re.I).strip()
        rating, count, how = None, None, None
        structured = r.get("rating") or (r.get("product") or {}).get("rating") or {}
        if isinstance(structured, dict) and structured.get("ratingValue"):
            best = float(structured.get("bestRating") or 5)
            rating = round(float(structured["ratingValue"]) * 5 / best, 2)
            count = structured.get("reviewCount") or structured.get("ratingCount")
            how = "structured"
        else:
            rating, count = _rating_from_text(text)
            how = "snippet" if rating else None
        price = _price_from_structured(r) or _price_from_text(text)
        # A page of one vintage says so in its address (?year=2020) or its title
        year = re.search(r"[?&]year=((?:19|20)\d{2})\b", url) or re.search(r"\b((?:19|20)\d{2})\b", title)
        out.append({"title": title, "url": url.split("?")[0], "rating": rating,
                    "ratings_count": int(count) if count else None, "how": how, "snippet": text[:300],
                    "price": price, "vintage": int(year.group(1)) if year else None})
    return out


def summary(result: dict) -> str:
    """What a search brought back, in one line: to see why a wine got no rating."""
    rows = (result.get("results") or []) + ((result.get("web") or {}).get("results") or [])
    if not rows:
        return "the search found nothing"
    parts = [f"{(r.get('title') or '?')[:60]} ({(r.get('url') or '').split('/')[2] if (r.get('url') or '').count('/') >= 2 else '?'})"
             for r in rows[:4]]
    return f"{len(rows)} result{'s' if len(rows) != 1 else ''}: " + "; ".join(parts)


def best_match(name: str, cands: list[dict]) -> tuple[dict | None, float]:
    """The result that is this wine: the best-matching name; among names that match about as well,
    one with a rating, and a Vivino page before a page quoting Vivino."""
    scored = []
    for c in cands:
        title = re.sub(r"\b(19|20)\d{2}\b", "", c["title"])
        s = max(fuzz.token_set_ratio(norm(name), norm(title)), fuzz.token_sort_ratio(norm(name), norm(title)))
        scored.append((s, c))
    if not scored:
        return None, 0.0
    top = max(s for s, _ in scored)
    if top < MIN_MATCH:
        return None, top
    close = [(s, c) for s, c in scored if s >= max(MIN_MATCH, top - 5)]
    s, c = max(close, key=lambda x: (x[1]["rating"] is not None, not x[1]["how"] or not x[1]["how"].startswith("quoted"), x[0]))
    return c, s


SIZE_WORDS = re.compile(
    r"\b\d+\s*[x×]\s*\d+([.,]\d+)?\s*(cl|ml|l)\b|\b\d+([.,]\d+)?\s*(cl|ml|ltr|liter|litre|l)\b"
    r"|\b(dubbele\s+)?magnum\b|\b(halve|half)\s+(fles|flesje|bottle)\b|\b(19|20)\d{2}\b", re.I)


def search_name(name: str) -> str:
    """The wine's name as Vivino knows it: without the vintage or the bottle size, which make
    Vivino's own search find nothing ("Taylor's Tawny 2019 37,5cl")."""
    return re.sub(r"\s+", " ", SIZE_WORDS.sub(" ", name or "")).strip(" -,")


def query_for(name: str, vintage=None) -> str:
    # The vintage is left out: a Vivino wine page covers every vintage.
    return f"{search_name(name)} vivino rating"


def look_up(con, key: str, wine_id: int, client=None, today: dt.date | None = None) -> dict:
    """Search one wine, store what came back, and return it."""
    ensure_tables(con)
    if searches_this_month(con, today) >= MONTHLY_LIMIT:
        raise RuntimeError(f"monthly limit of {MONTHLY_LIMIT} searches reached")
    w = con.execute("SELECT name, vintage FROM wine WHERE id=?", (wine_id,)).fetchone()
    name, vintage = w[0], w[1]
    result = search(key, query_for(name, vintage), client)
    _count_search(con, today)
    cands = candidates(result)
    match, score = best_match(name, cands)
    seen = summary(result)
    row = {"wine_id": wine_id, "wine": f"{name} {vintage or ''}".strip(), "rating": None, "ratings_count": None,
           "vivino_title": None, "vivino_url": None, "match_score": round(score, 1), "how": "no result",
           "snippet": seen if not match else None, "checked_on": (today or dt.date.today()).isoformat(), "price": None,
           "rating_vintage": None}
    if not match and cands:
        row["how"] = "no match"  # Vivino results, but none of them this wine
    elif not match and seen != "the search found nothing":
        row["how"] = "no Vivino page"  # results, but no Vivino page and no page quoting Vivino
    if match:
        row.update(rating=match["rating"], ratings_count=match["ratings_count"], vivino_title=match["title"],
                   vivino_url=match["url"], how=match["how"] or "no rating in result", snippet=match["snippet"],
                   price=match["price"], rating_vintage=match.get("vintage"))
    con.execute(f"INSERT OR REPLACE INTO vivino_rating ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                tuple(row.values()))
    con.commit()
    return row


def points(rating: float | None) -> float | None:
    """1-5 stars to the 100-point scale: 3.5 -> 85, 4.0 -> 90, 4.5 -> 95."""
    return round(50 + 10 * rating, 1) if rating else None


def counted(con, wine_id: int | None = None) -> list[dict]:
    """The Vivino ratings that count in the score, with their weight and why."""
    ensure_tables(con)
    rows = con.execute(
        """SELECT v.wine_id, v.rating, v.ratings_count, v.rating_vintage, w.vintage, w.cuvee_id,
                  (SELECT MAX(o.ratings_count) FROM vivino_rating o JOIN wine ow ON ow.id = o.wine_id
                    WHERE ow.cuvee_id = w.cuvee_id AND o.wine_id != v.wine_id AND o.rating IS NOT NULL) AS others
           FROM vivino_rating v JOIN wine w ON w.id = v.wine_id
           WHERE v.rating BETWEEN 1 AND 5 AND (? IS NULL OR v.wine_id = ?)""", (wine_id, wine_id)).fetchall()
    out = []
    for wid, rating, n, rating_vintage, vintage, _, others in rows:
        n, others = n or 0, others or 0
        this_vintage = bool(rating_vintage) and rating_vintage == vintage
        if this_vintage and n >= MANY:
            weight, why = FULL, f"{n:,} ratings for this vintage"
        elif n >= MANY or others >= MANY:
            weight, why = LESS, ("few ratings for this vintage, many for other vintages" if this_vintage
                                 else f"{max(n, others):,} ratings, not for this vintage alone")
        elif max(n, others) >= MIN_RATINGS:
            weight, why = LIGHT, f"only {max(n, others):,} ratings"
        else:
            continue
        out.append({"wine_id": wid, "points": points(rating), "weight": weight, "why": why,
                    "label": f"Vivino {rating:.1f} ({n:,} ratings)"})
    return out


def _reset_after_change(con):
    """After a change in the way of searching (LOOKUP_VERSION), the wines it found nothing for are
    tried again: they may well be found now."""
    row = con.execute("SELECT value FROM setting WHERE key='vivino_lookup_version'").fetchone()
    if row and str(row[0]).isdigit() and int(row[0]) >= LOOKUP_VERSION:
        return
    con.execute("DELETE FROM vivino_rating WHERE rating IS NULL AND price IS NULL")
    con.execute("INSERT OR REPLACE INTO setting (key, value) VALUES ('vivino_lookup_version', ?)", (str(LOOKUP_VERSION),))
    con.commit()


def _due_sql(today: dt.date | None):
    """Wines on sale not looked up yet, or looked up long enough ago: half a year after a rating,
    a month after a search that found nothing."""
    today = today or dt.date.today()
    since = (today - dt.timedelta(days=RECHECK_DAYS)).isoformat()
    since_none = (today - dt.timedelta(days=NO_RESULT_DAYS)).isoformat()
    return ("""w.id IN (SELECT wine_id FROM offer WHERE is_current = 1 AND gone_since IS NULL)
               AND w.id NOT IN (SELECT wine_id FROM vivino_rating
                                WHERE checked_on >= ? OR (checked_on >= ? AND (rating IS NOT NULL OR price IS NOT NULL)))""",
            [since_none, since])


def wines_to_look_up(con, count: int, today: dt.date | None = None) -> list[int]:
    """Wines on sale to look up, most useful first:
    1. on the watchlist or in the order;
    2. EUR 9-15 a bottle (FOCUS) with awards, best award score first: is the public as fond of them?
    3. EUR 9-15 without awards: Vivino is the only opinion there, and finds the ones the public likes;
    4. other wines with awards, best scored first; 5. the rest."""
    ensure_tables(con)
    _reset_after_change(con)
    where, args = _due_sql(today)
    return [r[0] for r in con.execute(
        f"""SELECT w.id,
                   (SELECT MIN(COALESCE(o.price_per_75cl, o.price)) FROM offer o
                     WHERE o.wine_id = w.id AND o.is_current = 1 AND o.gone_since IS NULL AND o.price > 0) AS price
            FROM wine w LEFT JOIN wine_score ws ON ws.wine_id = w.id
            WHERE {where}
            ORDER BY (w.id IN (SELECT wine_id FROM watch)
                      OR w.id IN (SELECT o.wine_id FROM order_item i JOIN offer o ON o.id = i.offer_id)) DESC,
                     CASE WHEN price BETWEEN ? AND ? THEN (CASE WHEN COALESCE(ws.n_awards, 0) > 0 THEN 0 ELSE 1 END)
                          WHEN COALESCE(ws.n_awards, 0) > 0 THEN 2 ELSE 3 END,
                     COALESCE(ws.overall, 0) DESC, price, w.id
            LIMIT ?""", (*args, *FOCUS, count))]


def waiting(con, today: dt.date | None = None) -> int:
    """How many wines on sale are still to look up (see _due_sql)."""
    ensure_tables(con)
    where, args = _due_sql(today)
    return con.execute(f"SELECT COUNT(*) FROM wine w WHERE {where}", args).fetchone()[0]


def cross_reference(con, low: float | None = None, high: float | None = None, limit: int = 100) -> dict:
    """Wines on sale with a Vivino rating, in a price band (default FOCUS), next to their award score:
    the ones the public likes that have no awards, and the ones both like (or disagree on)."""
    ensure_tables(con)
    low, high = low if low is not None else FOCUS[0], high if high is not None else FOCUS[1]
    rows = [dict(r) for r in con.execute(
        """SELECT w.id AS wine_id, w.name, w.vintage, w.colour, v.rating, v.ratings_count, v.how, v.vivino_url,
                  v.vivino_title, ws.overall, COALESCE(ws.n_awards, 0) AS n_awards, ws.best_award,
                  (SELECT MIN(COALESCE(o.price_per_75cl, o.price)) FROM offer o
                    WHERE o.wine_id = w.id AND o.is_current = 1 AND o.gone_since IS NULL AND o.price > 0) AS price
           FROM vivino_rating v JOIN wine w ON w.id = v.wine_id LEFT JOIN wine_score ws ON ws.wine_id = w.id
           WHERE v.rating IS NOT NULL""")]
    rows = [r for r in rows if r["price"] and low <= r["price"] <= high]
    for r in rows:
        r["points"] = points(r["rating"])
        r["gap"] = round(r["points"] - r["overall"], 1) if r["overall"] and r["n_awards"] else None
    no_awards = sorted((r for r in rows if not r["n_awards"]), key=lambda r: (-r["rating"], -(r["ratings_count"] or 0)))
    both = sorted((r for r in rows if r["n_awards"]), key=lambda r: (-(r["overall"] or 0), -r["rating"]))
    counts = con.execute("""SELECT COUNT(*), COUNT(rating), SUM(rating IS NULL AND how = 'no result'),
                                   SUM(rating IS NULL AND how = 'no Vivino page'), SUM(rating IS NULL AND how = 'no match'),
                                   SUM(rating IS NULL AND how NOT IN ('no result', 'no Vivino page', 'no match'))
                            FROM vivino_rating""").fetchone()
    misses = [dict(r) for r in con.execute(
        """SELECT v.wine_id, v.wine, v.how, v.snippet, v.match_score, v.checked_on FROM vivino_rating v
           WHERE v.rating IS NULL ORDER BY v.checked_on DESC, v.wine_id DESC LIMIT 15""")]
    return {"low": low, "high": high, "no_awards": no_awards[:limit], "both": both[:limit],
            "looked_up": counts[0], "rated": counts[1] or 0,
            "why_not": {"the search found nothing": counts[2] or 0, "no Vivino page in the results": counts[3] or 0,
                        "Vivino pages, but not this wine": counts[4] or 0, "a page without a rating": counts[5] or 0},
            "misses": misses}


def running(con) -> dict | None:
    """The look-up that is going on now, if any."""
    from .process import is_alive

    r = con.execute("SELECT * FROM run_log WHERE job='vivino' AND finished IS NULL AND process IS NOT NULL "
                    "ORDER BY id DESC LIMIT 1").fetchone()
    return dict(r) if r and is_alive(r["process"]) else None


def status(con, now: dt.datetime | None = None) -> dict:
    """For the page Read shops now: is it set up, what it did last, what's next, the searches left."""
    ensure_tables(con)
    now = now or dt.datetime.now()
    today = now.date()
    used = searches_this_month(con, today)
    last = con.execute("SELECT * FROM run_log WHERE job='vivino' AND finished IS NOT NULL "
                       "ORDER BY id DESC LIMIT 1").fetchone()
    last = dict(last) if last else None
    has_key = bool(load_key())
    now_running = running(con)
    nxt = None
    if has_key and not now_running:
        nxt = "as soon as the shop runs are done" if weekly_due(con, now) else \
            (dt.datetime.fromisoformat(last["started"]) + dt.timedelta(days=7)).strftime("%A %d %B")
    room = max(0, min(WEEKLY, MONTHLY_LIMIT - used))
    counts = con.execute("SELECT COUNT(*), COUNT(rating) FROM vivino_rating").fetchone()
    return {"has_key": has_key, "running": now_running, "last": last, "next": nxt, "used": used,
            "limit": MONTHLY_LIMIT, "weekly": WEEKLY, "room": room, "checked": counts[0], "rated": counts[1],
            "waiting": waiting(con, today), "month": today.strftime("%B"), "focus": FOCUS}


def start_in_background() -> None:
    """A look-up as its own process, so the page stays quick."""
    import subprocess
    import sys
    import threading

    proc = subprocess.Popen([sys.executable, "-m", "wijnkoop", "vivino-run"], start_new_session=True,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
    threading.Thread(target=proc.wait, daemon=True).start()


def weekly_due(con, now: dt.datetime) -> bool:
    if not load_key() or running(con):
        return False
    row = con.execute("SELECT MAX(started) FROM run_log WHERE job='vivino' AND finished IS NOT NULL").fetchone()
    return not row[0] or (now - dt.datetime.fromisoformat(row[0])).days >= 7


def run_weekly(con, key: str | None = None, client=None, today: dt.date | None = None) -> dict:
    """Look up this week's wines. Stops at the monthly limit or when Tavily refuses the key."""
    import logging

    log = logging.getLogger("wijnkoop")
    key = key or load_key()
    ensure_tables(con)
    from .process import process_token

    now = dt.datetime.now().isoformat(timespec="seconds")
    room = max(0, min(WEEKLY, MONTHLY_LIMIT - searches_this_month(con, today)))
    wines = wines_to_look_up(con, room, today) if key else []
    run_id = con.execute("INSERT INTO run_log (job, started, process, note) VALUES ('vivino', ?, ?, ?)",
                         (now, process_token(), f"looking up {len(wines)} wines")).lastrowid
    con.commit()
    found = failed = done = 0
    note = None if key else "no Tavily key"
    if key and not room:
        note = f"the {MONTHLY_LIMIT} searches of this month are used"
    client = client or httpx.Client(timeout=20)
    for wine_id in wines:
        con.execute("UPDATE run_log SET pages_ok=?, pages_failed=? WHERE id=?", (done, failed, run_id))
        try:
            row = look_up(con, key, wine_id, client, today)
        except httpx.HTTPStatusError as e:
            failed += 1
            if e.response.status_code in (401, 403, 429, 432, 433):  # wrong key, or the free searches are used up
                note = f"Tavily answered {e.response.status_code}; stopped"
                break
            continue
        except (httpx.HTTPError, RuntimeError) as e:
            failed += 1
            log.warning("Vivino look-up of wine %s failed: %s", wine_id, e)
            continue
        done += 1
        found += row["rating"] is not None
    con.execute("UPDATE run_log SET finished=?, pages_ok=?, pages_failed=?, note=?, process=NULL WHERE id=?",
                (dt.datetime.now().isoformat(timespec="seconds"), done, failed,
                 note or f"{found} of {done} wines got a Vivino rating", run_id))
    con.commit()
    return {"looked_up": done, "rated": found, "failed": failed}


def search_link(name: str, vintage=None) -> str:
    from urllib.parse import quote_plus
    return "https://www.vivino.com/search/wines?q=" + quote_plus(search_name(name))
