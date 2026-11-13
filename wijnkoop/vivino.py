"""Vivino ratings via the Brave Search API, as a test.

Vivino's terms forbid reading vivino.com with automated means, so WijnKoop never opens a
Vivino page. Instead it asks a search engine (Brave Search API, with Loes's own key) for the
wine on vivino.com and reads the rating from the search result: Brave's structured rating
when it has one, otherwise a rating in the snippet text.

- The key lives in /data/brave.ini (written by "python -m wijnkoop vivino-setup"), never in code.
- At most MONTHLY_LIMIT searches a month, so the free monthly credit is never exceeded.
- The Vivino rating (1-5 stars from the public) is kept apart from the award score: it is
  shown next to it, not mixed in.
- Brave's terms say search results may not be kept after the API agreement ends: delete the
  vivino_rating table (or run "vivino-setup --forget") when the key is cancelled.
"""
import configparser
import datetime as dt
import os
import re

import httpx
from rapidfuzz import fuzz

from . import config
from .normalize import norm

SETTINGS = config.DATA_DIR / "brave.ini"
ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
MONTHLY_LIMIT = int(os.environ.get("WIJNKOOP_VIVINO_MONTHLY_LIMIT", "900"))
MIN_MATCH = 80

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
    checked_on TEXT
);
CREATE TABLE IF NOT EXISTS vivino_usage (month TEXT PRIMARY KEY, searches INTEGER NOT NULL DEFAULT 0);
"""


def ensure_tables(con):
    con.executescript(SCHEMA)


def save_key(key: str, path=None):
    path = path or SETTINGS
    cp = configparser.ConfigParser()
    cp["brave"] = {"api_key": key.strip()}
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
    return cp.get("brave", "api_key", fallback=None) or None


def searches_this_month(con, today: dt.date | None = None) -> int:
    month = (today or dt.date.today()).strftime("%Y-%m")
    row = con.execute("SELECT searches FROM vivino_usage WHERE month=?", (month,)).fetchone()
    return row[0] if row else 0


def _count_search(con, today: dt.date | None = None):
    month = (today or dt.date.today()).strftime("%Y-%m")
    con.execute("""INSERT INTO vivino_usage (month, searches) VALUES (?, 1)
                   ON CONFLICT(month) DO UPDATE SET searches = searches + 1""", (month,))


def search(key: str, query: str, client=None) -> dict:
    client = client or httpx.Client(timeout=20)
    r = client.get(ENDPOINT, params={"q": query, "count": 5, "extra_snippets": "true"},
                   headers={"X-Subscription-Token": key, "Accept": "application/json",
                            "User-Agent": config.USER_AGENT})
    r.raise_for_status()
    return r.json()


SNIPPET_RATING = [
    re.compile(r"(?:rating|beoordeling|rated|gemiddelde)[^0-9]{0,30}([1-5][.,]\d)\b", re.I),
    re.compile(r"\b([1-5][.,]\d)\s*(?:/\s*5|out of 5|van de 5|stars|sterren|★)", re.I),
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


def candidates(result: dict) -> list[dict]:
    """Vivino wine pages in a Brave result, with any rating the result carries."""
    out = []
    for r in (result.get("web") or {}).get("results", []):
        url = r.get("url") or ""
        if "vivino.com" not in url or "/w/" not in url:
            continue
        title = re.sub(r"\s*[|-]\s*Vivino.*$", "", r.get("title") or "", flags=re.I).strip()
        text = " ".join([r.get("description") or ""] + list(r.get("extra_snippets") or []))
        text = re.sub(r"<[^>]+>", "", text)
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
        out.append({"title": title, "url": url.split("?")[0], "rating": rating,
                    "ratings_count": int(count) if count else None, "how": how, "snippet": text[:300]})
    return out


def best_match(name: str, cands: list[dict]) -> tuple[dict | None, float]:
    best, score = None, 0.0
    for c in cands:
        title = re.sub(r"\b(19|20)\d{2}\b", "", c["title"])
        s = max(fuzz.token_set_ratio(norm(name), norm(title)), fuzz.token_sort_ratio(norm(name), norm(title)))
        if s > score:
            best, score = c, s
    return (best, score) if score >= MIN_MATCH else (None, score)


def query_for(name: str, vintage) -> str:
    return f"site:vivino.com {name} {vintage or ''}".strip()


def look_up(con, key: str, wine_id: int, client=None, today: dt.date | None = None) -> dict:
    """Search one wine, store what came back, and return it."""
    ensure_tables(con)
    if searches_this_month(con, today) >= MONTHLY_LIMIT:
        raise RuntimeError(f"monthly limit of {MONTHLY_LIMIT} searches reached")
    w = con.execute("SELECT name, vintage FROM wine WHERE id=?", (wine_id,)).fetchone()
    name, vintage = w[0], w[1]
    result = search(key, query_for(name, vintage), client)
    _count_search(con, today)
    match, score = best_match(name, candidates(result))
    row = {"wine_id": wine_id, "wine": f"{name} {vintage or ''}".strip(), "rating": None, "ratings_count": None,
           "vivino_title": None, "vivino_url": None, "match_score": round(score, 1), "how": "no result",
           "snippet": None, "checked_on": (today or dt.date.today()).isoformat()}
    if match:
        row.update(rating=match["rating"], ratings_count=match["ratings_count"], vivino_title=match["title"],
                   vivino_url=match["url"], how=match["how"] or "no rating in result", snippet=match["snippet"])
    con.execute(f"INSERT OR REPLACE INTO vivino_rating ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                tuple(row.values()))
    con.commit()
    return row


def wines_to_test(con, count: int) -> list[int]:
    """The best-scored wines on sale that were not looked up yet."""
    ensure_tables(con)
    return [r[0] for r in con.execute(
        """SELECT b.wine_id FROM best_buys b
           WHERE b.score IS NOT NULL AND b.wine_id NOT IN (SELECT wine_id FROM vivino_rating)
           GROUP BY b.wine_id ORDER BY MAX(b.score) DESC LIMIT ?""", (count,))]


def search_link(name: str, vintage=None) -> str:
    from urllib.parse import quote_plus
    return "https://www.vivino.com/search/wines?q=" + quote_plus(f"{name} {vintage or ''}".strip())
