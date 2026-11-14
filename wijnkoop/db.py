"""Database: schema, upgrades of older databases, and views.

Levels:
  cuvee  - a wine regardless of vintage (producer + name), e.g. "Taittinger Brut Prestige"
  wine   - one vintage of a cuvee, e.g. "Veuve Clicquot La Grande Dame Brut 2018"
  offer  - one wine (vintage) at one shop page. When a shop page switches to a new
           vintage, the old offer is kept (is_current = 0) and a new one starts.
"""
import sqlite3

from .config import DB_PATH

SCHEMA_VERSION = 2

TABLES = """
CREATE TABLE IF NOT EXISTS shop (
    id INTEGER PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    website TEXT,
    delivery_cost REAL,
    free_delivery_from REAL,
    min_order REAL,
    delivery_note TEXT
);
CREATE TABLE IF NOT EXISTS cuvee (
    id INTEGER PRIMARY KEY,
    norm_key TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    producer TEXT,
    producer_key TEXT,
    colour TEXT,
    country TEXT,
    region TEXT,
    grapes TEXT,
    category TEXT,
    is_wine INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS wine (
    id INTEGER PRIMARY KEY,
    norm_key TEXT UNIQUE NOT NULL,
    cuvee_id INTEGER REFERENCES cuvee(id),
    name TEXT NOT NULL,
    producer TEXT,
    vintage INTEGER,
    colour TEXT,
    country TEXT,
    region TEXT,
    grapes TEXT,
    style TEXT,
    alcohol REAL
);
CREATE TABLE IF NOT EXISTS offer (
    id INTEGER PRIMARY KEY,
    shop_id INTEGER NOT NULL REFERENCES shop(id),
    wine_id INTEGER NOT NULL REFERENCES wine(id),
    shop_product_id TEXT NOT NULL,
    vintage_key TEXT NOT NULL DEFAULT 'nv',
    vintage_checked INTEGER DEFAULT 1,
    is_current INTEGER DEFAULT 1,
    url TEXT NOT NULL,
    title TEXT,
    category TEXT,
    volume_cl REAL,
    price REAL,
    price_per_75cl REAL,
    was_price REAL,
    member_price REAL,
    promotion TEXT,
    deal_type TEXT,
    deal_min_qty INTEGER,
    deal_factor REAL,
    deal_mixable INTEGER,
    effective_price_per_75cl REAL,
    in_stock_online INTEGER,
    awards_count INTEGER,
    shop_rating REAL,
    first_seen TEXT,
    last_seen TEXT,
    gone_since TEXT,
    UNIQUE (shop_id, shop_product_id, vintage_key)
);
CREATE TABLE IF NOT EXISTS price_history (
    id INTEGER PRIMARY KEY,
    offer_id INTEGER NOT NULL REFERENCES offer(id),
    seen_on TEXT NOT NULL,
    price REAL,
    promotion TEXT,
    was_price REAL,
    effective_price_per_75cl REAL,
    in_stock INTEGER,
    UNIQUE (offer_id, seen_on)
);
CREATE TABLE IF NOT EXISTS event (
    id INTEGER PRIMARY KEY,
    happened_on TEXT NOT NULL,
    kind TEXT NOT NULL,
    shop_id INTEGER REFERENCES shop(id),
    offer_id INTEGER REFERENCES offer(id),
    wine_id INTEGER REFERENCES wine(id),
    old_value TEXT,
    new_value TEXT,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS ix_event_day ON event(happened_on);
CREATE TABLE IF NOT EXISTS competition (
    id INTEGER PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    short TEXT,
    kind TEXT DEFAULT 'competition',
    website TEXT,
    weight REAL DEFAULT 0.4
);
CREATE TABLE IF NOT EXISTS award (
    id INTEGER PRIMARY KEY,
    competition_id INTEGER NOT NULL REFERENCES competition(id),
    year INTEGER,
    producer_raw TEXT,
    name_raw TEXT,
    vintage INTEGER,
    colour TEXT,
    country TEXT,
    medal TEXT,
    points REAL,
    source TEXT,
    source_url TEXT,
    raw_text TEXT,
    found_on TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_award ON award (
    COALESCE(source, ''), COALESCE(source_url, ''), competition_id, COALESCE(year, 0),
    COALESCE(medal, ''), COALESCE(points, 0), COALESCE(name_raw, '')
);
CREATE TABLE IF NOT EXISTS award_match (
    award_id INTEGER NOT NULL REFERENCES award(id),
    wine_id INTEGER NOT NULL REFERENCES wine(id),
    match_score REAL,
    confirmed INTEGER,
    PRIMARY KEY (award_id, wine_id)
);
CREATE TABLE IF NOT EXISTS manual_score (
    id INTEGER PRIMARY KEY,
    wine_id INTEGER NOT NULL REFERENCES wine(id),
    source TEXT NOT NULL,
    value REAL NOT NULL,
    noted_on TEXT
);
CREATE TABLE IF NOT EXISTS wine_score (
    wine_id INTEGER PRIMARY KEY REFERENCES wine(id),
    n_awards INTEGER,
    evidence REAL,
    overall REAL,
    best_award TEXT,
    cuvee_vintages INTEGER,
    cuvee_avg REAL,
    producer_wines INTEGER,
    producer_avg REAL,
    updated TEXT,
    gold INTEGER DEFAULT 0,
    wine TEXT
);
CREATE TABLE IF NOT EXISTS offer_score (
    offer_id INTEGER PRIMARY KEY REFERENCES offer(id),
    bottle_price_at_6 REAL,
    order_of_6 REAL,
    delivery_at_6 REAL,
    reference_price REAL,
    is_deal INTEGER,
    deal_reason TEXT,
    value REAL,
    fair_price REAL,
    price_quality REAL,
    verdict TEXT,
    updated TEXT,
    wine TEXT,
    price_as_93 REAL,
    market_rank REAL,
    best_for_price INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS skipped_page (
    shop_id INTEGER NOT NULL REFERENCES shop(id),
    shop_product_id TEXT NOT NULL,
    seen_on TEXT,
    PRIMARY KEY (shop_id, shop_product_id)
);
CREATE TABLE IF NOT EXISTS failed_page (
    run_id INTEGER NOT NULL REFERENCES run_log(id),
    url TEXT NOT NULL,
    reason TEXT,
    seen_on TEXT,
    PRIMARY KEY (run_id, url)
);
CREATE TABLE IF NOT EXISTS cuvee_alias (
    norm_key TEXT PRIMARY KEY,
    cuvee_id INTEGER NOT NULL REFERENCES cuvee(id),
    name TEXT,
    shop_id INTEGER REFERENCES shop(id),
    match_score REAL
);
CREATE TABLE IF NOT EXISTS same_wine_check (
    cuvee_id INTEGER NOT NULL REFERENCES cuvee(id),
    other_cuvee_id INTEGER NOT NULL REFERENCES cuvee(id),
    match_score REAL,
    PRIMARY KEY (cuvee_id, other_cuvee_id)
);
-- Results files Loes uploads herself as an official check (awards with source 'upload')
CREATE TABLE IF NOT EXISTS award_upload (
    id INTEGER PRIMARY KEY,
    filename TEXT NOT NULL,
    kind TEXT,
    competition_id INTEGER REFERENCES competition(id),
    year INTEGER,
    uploaded_on TEXT,
    how TEXT,
    rows_read INTEGER,
    awards INTEGER,
    linked INTEGER,
    to_review INTEGER
);
CREATE TABLE IF NOT EXISTS setting (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS watch (
    wine_id INTEGER PRIMARY KEY REFERENCES wine(id),
    added TEXT
);
CREATE TABLE IF NOT EXISTS order_item (
    offer_id INTEGER PRIMARY KEY REFERENCES offer(id),
    qty INTEGER NOT NULL DEFAULT 1,
    added TEXT
);
CREATE TABLE IF NOT EXISTS run_log (
    id INTEGER PRIMARY KEY,
    job TEXT NOT NULL,
    started TEXT NOT NULL,
    finished TEXT,
    pages_ok INTEGER DEFAULT 0,
    pages_failed INTEGER DEFAULT 0,
    note TEXT,
    process TEXT
);
CREATE TABLE IF NOT EXISTS app_job (
    id INTEGER PRIMARY KEY,
    shops TEXT NOT NULL,
    requested TEXT NOT NULL,
    process TEXT,
    finished TEXT,
    note TEXT
);
"""

VIEWS = """
DROP VIEW IF EXISTS wine_list;
DROP VIEW IF EXISTS best_buys;
DROP VIEW IF EXISTS award_list;
DROP VIEW IF EXISTS weekly_events;

CREATE VIEW wine_list AS
SELECT
    w.name AS wine,
    w.producer,
    w.wine_name,
    w.vintage,
    w.colour,
    w.country,
    w.region,
    w.grapes,
    s.name AS shop,
    o.price,
    o.price_per_75cl,
    o.was_price,
    o.promotion,
    o.effective_price_per_75cl,
    o.awards_count,
    CASE o.in_stock_online WHEN 1 THEN 'yes' WHEN 0 THEN 'no' END AS online_in_stock,
    CASE WHEN o.gone_since IS NULL THEN 'no' ELSE 'yes' END AS gone,
    o.last_seen,
    o.url
FROM offer o
JOIN wine w ON w.id = o.wine_id
JOIN shop s ON s.id = o.shop_id
LEFT JOIN cuvee c ON c.id = w.cuvee_id
WHERE o.is_current = 1 AND COALESCE(c.is_wine, 1) = 1;

CREATE VIEW best_buys AS
SELECT
    o.id AS offer_id,
    w.id AS wine_id,
    w.name AS wine,
    w.vintage,
    w.producer,
    w.wine_name,
    w.colour,
    w.country,
    s.name AS shop,
    os.bottle_price_at_6 AS price_per_bottle_at_6,  -- per 75 cl, so every bottle size compares fairly
    ROUND(os.bottle_price_at_6 * COALESCE(o.volume_cl, 75) / 75.0, 2) AS price_per_bottle_as_sold,
    os.order_of_6 AS price_6_bottles_incl_delivery,
    COALESCE(o.price_per_75cl, o.price) AS normal_price,
    o.promotion,
    CASE os.is_deal WHEN 1 THEN 'yes' ELSE 'no' END AS deal,
    os.deal_reason,
    ws.overall AS score,
    ws.n_awards AS awards,
    ws.best_award,
    os.value,
    os.price_as_93,
    CAST(ROUND(100 * os.market_rank) AS INTEGER) AS cheaper_than_pct,
    os.verdict,
    CASE os.best_for_price WHEN 1 THEN 'yes' ELSE 'no' END AS nothing_better_for_less,
    os.fair_price,
    os.price_quality,
    CASE ws.gold WHEN 1 THEN 'yes' ELSE 'no' END AS gold_or_better,
    w.grapes,
    w.style,
    c.category,
    o.volume_cl,
    o.member_price,
    ws.cuvee_vintages AS other_vintages_awarded,
    ws.cuvee_avg AS other_vintages_avg,
    ws.producer_wines AS producer_wines_awarded,
    ws.producer_avg,
    CASE o.in_stock_online WHEN 1 THEN 'yes' WHEN 0 THEN 'no' END AS online_in_stock,
    o.url
FROM offer o
JOIN offer_score os ON os.offer_id = o.id
JOIN wine w ON w.id = o.wine_id
JOIN shop s ON s.id = o.shop_id
LEFT JOIN cuvee c ON c.id = w.cuvee_id
LEFT JOIN wine_score ws ON ws.wine_id = w.id
WHERE o.is_current = 1 AND o.gone_since IS NULL AND COALESCE(c.is_wine, 1) = 1;

CREATE VIEW award_list AS
SELECT
    c.short AS award,
    c.name AS competition,
    c.kind,
    a.year,
    a.medal,
    a.points,
    w.id AS wine_id,
    w.name AS wine,
    w.producer,
    w.wine_name,
    w.vintage,
    w.colour,
    w.country,
    a.producer_raw AS file_producer,
    a.name_raw AS file_wine,
    (SELECT MIN(os.bottle_price_at_6) FROM offer o JOIN offer_score os ON os.offer_id = o.id
      WHERE o.wine_id = w.id AND o.is_current = 1 AND o.gone_since IS NULL) AS price_per_bottle_at_6,
    a.source,
    a.raw_text
FROM award a
JOIN competition c ON c.id = a.competition_id
JOIN award_match m ON m.award_id = a.id
JOIN wine w ON w.id = m.wine_id;

CREATE VIEW weekly_events AS
SELECT
    e.happened_on,
    e.kind,
    e.wine_id,
    w.name AS wine,
    w.producer,
    w.wine_name,
    w.vintage,
    s.name AS shop,
    e.old_value,
    e.new_value,
    e.detail,
    ws.overall AS score,
    o.url
FROM event e
LEFT JOIN wine w ON w.id = e.wine_id
LEFT JOIN shop s ON s.id = e.shop_id
LEFT JOIN offer o ON o.id = e.offer_id
LEFT JOIN wine_score ws ON ws.wine_id = e.wine_id;
"""

# Columns added since phase 1, per table: (column, definition)
ADDED_COLUMNS = {
    "shop": [("delivery_cost", "REAL"), ("free_delivery_from", "REAL"), ("min_order", "REAL"),
             ("delivery_note", "TEXT")],
    "wine": [("cuvee_id", "INTEGER REFERENCES cuvee(id)")],
    "price_history": [("was_price", "REAL"), ("effective_price_per_75cl", "REAL"), ("in_stock", "INTEGER")],
    "competition": [("short", "TEXT"), ("kind", "TEXT DEFAULT 'competition'")],
    "award": [("source", "TEXT"), ("raw_text", "TEXT"), ("found_on", "TEXT")],
}

OFFER_COLUMNS = [
    "id", "shop_id", "wine_id", "shop_product_id", "url", "title", "volume_cl", "price",
    "price_per_75cl", "promotion", "in_stock_online", "awards_count", "first_seen", "last_seen",
]


def _columns(con, table):
    return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}


def _exec_all(con, sql):
    """Run several statements inside the current transaction (executescript would commit)."""
    for stmt in sql.split(";\n"):
        if stmt.strip():
            con.execute(stmt)


def _upgrade_from_phase1(con):
    """Phase 1 databases: add columns, rebuild offer (new unique key), fill cuvees."""
    for table, cols in ADDED_COLUMNS.items():
        have = _columns(con, table)
        for name, definition in cols:
            if name not in have:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
    if "vintage_key" not in _columns(con, "offer"):
        # SQLite's recipe: build the new table, copy, drop the old one, rename.
        new_offer = TABLES[TABLES.index("CREATE TABLE IF NOT EXISTS offer ("):]
        new_offer = new_offer[: new_offer.index(");") + 2].replace(
            "IF NOT EXISTS offer (", "offer_new (")
        con.execute(new_offer)
        cols = ", ".join(OFFER_COLUMNS)
        src = ", ".join("o." + c for c in OFFER_COLUMNS)
        # Phase 1 could not read vintages: mark them as not checked yet, so the
        # first phase 2 run fills them in instead of logging a vintage change.
        con.execute(
            f"INSERT INTO offer_new ({cols}, vintage_key, vintage_checked, is_current) "
            f"SELECT {src}, COALESCE(CAST(w.vintage AS TEXT), 'nv'), 0, 1 "
            f"FROM offer o JOIN wine w ON w.id = o.wine_id"
        )
        con.execute("DROP TABLE offer")
        con.execute("ALTER TABLE offer_new RENAME TO offer")
    _exec_all(con, TABLES)
    _fill_cuvees(con)
    _fill_effective_prices(con)


def _fill_effective_prices(con):
    """Phase 1 read promotions from the whole page, including the "others also bought"
    tiles, so they cannot be trusted. Keep its prices, but count no deal until the
    first phase 2 run has read the promotion badge itself."""
    con.execute("UPDATE offer SET effective_price_per_75cl = price_per_75cl "
                "WHERE effective_price_per_75cl IS NULL")


def _fill_cuvees(con):
    from .normalize import cuvee_key, norm

    rows = con.execute("SELECT id, name, producer, colour, country, region, grapes FROM wine "
                       "WHERE cuvee_id IS NULL").fetchall()
    for r in rows:
        key = cuvee_key(r["name"])
        con.execute(
            """INSERT INTO cuvee (norm_key, name, producer, producer_key, colour, country, region, grapes)
               VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(norm_key) DO NOTHING""",
            (key, r["name"], r["producer"], norm(r["producer"]), r["colour"], r["country"], r["region"],
             r["grapes"]),
        )
        cid = con.execute("SELECT id FROM cuvee WHERE norm_key=?", (key,)).fetchone()[0]
        con.execute("UPDATE wine SET cuvee_id=? WHERE id=?", (cid, r["id"]))


def _backup(con, path, version):
    """Copy the database before upgrading it, e.g. wijnkoop-backup-v0.db next to it."""
    target = path.with_name(f"{path.stem}-backup-v{version}.db")
    if target.exists():
        return
    dest = sqlite3.connect(target)
    with dest:
        con.backup(dest)
    dest.close()


# Columns added after phase 2 went live; added on start-up when missing.
LATER_COLUMNS = {
    "run_log": [("process", "TEXT"), ("wait_until", "TEXT")],
    # The wine's own name, without the estate (which is in producer): "Reserva Tinto" of Esporao
    "cuvee": [("wine_name", "TEXT"),
              ("manual_is_wine", "INTEGER")],  # set on the data quality page: wins over the shop
    "wine": [("wine_name", "TEXT"),
             ("manual_colour", "TEXT")],  # set on the data quality page: wins over the shop
    "award_upload": [("log", "TEXT")],
    "offer": [("image_url", "TEXT")],  # the bottle photo at the shop, for comparing on the data quality page
    "wine_score": [("gold", "INTEGER DEFAULT 0"), ("wine", "TEXT")],
    "offer_score": [("fair_price", "REAL"), ("price_quality", "REAL"), ("verdict", "TEXT"), ("wine", "TEXT"),
                    ("price_as_93", "REAL"), ("market_rank", "REAL"), ("best_for_price", "INTEGER DEFAULT 0")],
}


def fill_wine_names(con, only_missing: bool = True) -> int:
    """Estate and wine apart: wine_name is the name without the producer at its start."""
    from .normalize import split_estate

    n = 0
    for table, sql in (
            ("cuvee", "SELECT id, name, producer FROM cuvee"),
            ("wine", "SELECT w.id, w.name, COALESCE(w.producer, c.producer) AS producer FROM wine w "
                     "LEFT JOIN cuvee c ON c.id = w.cuvee_id")):
        where = " WHERE wine_name IS NULL" if only_missing else ""
        ids = {r[0] for r in con.execute(f"SELECT id FROM {table}{where}")}
        if not ids:
            continue
        rows = [(split_estate(r[1], r[2])[1], r[0]) for r in con.execute(sql) if r[0] in ids]
        con.executemany(f"UPDATE {table} SET wine_name=? WHERE id=?", rows)
        n += len(rows)
    con.commit()
    return n


def _add_later_columns(con):
    for table, cols in LATER_COLUMNS.items():
        have = _columns(con, table)
        for name, definition in cols:
            if name not in have:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
    con.commit()


# Indexes for what the pages, the scores and the runs look up most (added on start-up when missing).
INDEXES = """
CREATE INDEX IF NOT EXISTS ix_offer_wine ON offer(wine_id);
CREATE INDEX IF NOT EXISTS ix_offer_shop_current ON offer(shop_id, is_current);
CREATE INDEX IF NOT EXISTS ix_award_match_wine ON award_match(wine_id);
CREATE INDEX IF NOT EXISTS ix_event_wine ON event(wine_id);
CREATE INDEX IF NOT EXISTS ix_wine_cuvee ON wine(cuvee_id);
CREATE INDEX IF NOT EXISTS ix_run_log_job ON run_log(job, finished);
CREATE INDEX IF NOT EXISTS ix_price_history_seen ON price_history(seen_on);
"""


PRICE_FIELDS = ("price", "promotion", "was_price", "effective_price_per_75cl", "in_stock")


def record_price(con, offer_id: int, day: str, price, promotion, was_price, effective, in_stock) -> bool:
    """Note the offer's price of this day in price_history, but only when something changed since
    the line before: an unchanged price is carried by offer.last_seen (scores.expand gives the
    weekly series back). Returns whether a line was written."""
    values = (price, promotion, was_price, effective, in_stock)
    prev = con.execute(f"SELECT seen_on, {', '.join(PRICE_FIELDS)} FROM price_history "
                       "WHERE offer_id=? AND seen_on <= ? ORDER BY seen_on DESC LIMIT 1", (offer_id, day)).fetchone()
    if prev and prev[0] < day and tuple(prev[1:]) == values:
        return False
    con.execute(
        """INSERT INTO price_history (offer_id, seen_on, price, promotion, was_price, effective_price_per_75cl,
                                      in_stock)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(offer_id, seen_on) DO UPDATE SET price=excluded.price, promotion=excluded.promotion,
             was_price=excluded.was_price, effective_price_per_75cl=excluded.effective_price_per_75cl,
             in_stock=excluded.in_stock""", (offer_id, day, *values))
    return True


def compact_price_history(con) -> int:
    """Once: remove the weekly lines that repeat the line before (same price, promotion, stock).
    Databases from before November 2026 kept a line every week; this makes them many times smaller."""
    if con.execute("SELECT 1 FROM setting WHERE key='price_history_compacted'").fetchone():
        return 0
    same = " AND ".join(f"{f} IS prev_{f}" for f in PRICE_FIELDS)
    lags = ", ".join(f"LAG({f}) OVER w AS prev_{f}" for f in PRICE_FIELDS)
    n = con.execute(f"""DELETE FROM price_history WHERE id IN (
                          SELECT id FROM (SELECT id, {', '.join(PRICE_FIELDS)}, LAG(id) OVER w AS prev_id, {lags}
                                          FROM price_history WINDOW w AS (PARTITION BY offer_id ORDER BY seen_on))
                          WHERE prev_id IS NOT NULL AND {same})""").rowcount
    con.execute("INSERT OR REPLACE INTO setting (key, value) VALUES ('price_history_compacted', date('now'))")
    con.commit()
    if n > 10000:
        con.execute("VACUUM")  # give the space back to the NAS (once)
    return n


def tune(con) -> None:
    """Settings per connection. In WAL mode synchronous=NORMAL is safe (a power cut can lose the
    last moments, never corrupt the file) and saves a disk flush for every wine a run saves, so the
    NAS disks are not kept busy all night."""
    con.execute("PRAGMA synchronous = NORMAL")


def optimize(con) -> None:
    """Keep SQLite's query planner up to date; cheap, run after the scores are worked out."""
    try:
        con.execute("PRAGMA optimize")
    except sqlite3.Error:
        pass


BUSY_TIMEOUT = 300  # seconds: a shop run's scores or an upload can hold the database a while


def connect(path=None):
    path = path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    # Two jobs may write at the same time (a run per shop, the web pages): wait for the other one
    # instead of failing with "database is locked".
    con = sqlite3.connect(path, timeout=BUSY_TIMEOUT)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode = WAL")
    tune(con)
    if _up_to_date(con):
        # The schema, views and reference data are what this code expects: nothing to write. (Before,
        # every connection rewrote them, which needs the write lock and could wait minutes for a run.)
        con.execute("PRAGMA foreign_keys = ON")
        return con
    migrate(con, path)
    return con


_STAMP = None


def schema_stamp() -> str:
    """A fingerprint of the code that shapes the database (tables, views, reference data)."""
    global _STAMP
    if _STAMP is None:
        import hashlib
        from pathlib import Path

        here = Path(__file__).parent
        h = hashlib.sha1()
        for name in ("db.py", "reference.py", "vivino.py", "pace.py", "quality.py", "normalize.py", "personal.py"):
            f = here / name
            if f.exists():
                h.update(f.read_bytes())
        _STAMP = h.hexdigest()[:16]
    return _STAMP


def _up_to_date(con) -> bool:
    try:
        row = con.execute("SELECT value FROM setting WHERE key='schema_stamp'").fetchone()
    except sqlite3.Error:
        return False
    return bool(row) and row[0] == schema_stamp()


def migrate(con, path=None):
    """Create or upgrade the tables, views and reference data. Runs once after an update (the
    container start does it with "wijnkoop init"); every later connection sees the stamp and skips it."""
    path = path or DB_PATH
    version = con.execute("PRAGMA user_version").fetchone()[0]
    if version < SCHEMA_VERSION:
        has_offer = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='offer'").fetchone()
        if has_offer:
            _backup(con, path, version)
        con.execute("PRAGMA foreign_keys = OFF")
        con.executescript("DROP VIEW IF EXISTS wine_list;")
        con.execute("BEGIN")
        if has_offer:
            _upgrade_from_phase1(con)
        else:
            _exec_all(con, TABLES)
        con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        con.commit()
    con.executescript(TABLES)
    from .vivino import ensure_tables

    ensure_tables(con)
    from .pace import ensure_table as ensure_pace

    ensure_pace(con)
    from .quality import ensure_tables as ensure_quality

    ensure_quality(con)
    from .personal import ensure_tables as ensure_personal

    ensure_personal(con)
    _add_later_columns(con)
    fill_wine_names(con)
    con.executescript(INDEXES)
    con.executescript(VIEWS)
    con.execute("PRAGMA foreign_keys = ON")
    from .reference import seed

    seed(con)
    compact_price_history(con)
    con.execute("INSERT OR REPLACE INTO setting (key, value) VALUES ('schema_stamp', ?)", (schema_stamp(),))
    con.commit()
    return con
