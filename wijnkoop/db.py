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
    updated TEXT
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
    updated TEXT
);
CREATE TABLE IF NOT EXISTS run_log (
    id INTEGER PRIMARY KEY,
    job TEXT NOT NULL,
    started TEXT NOT NULL,
    finished TEXT,
    pages_ok INTEGER DEFAULT 0,
    pages_failed INTEGER DEFAULT 0,
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
    w.name AS wine,
    w.vintage,
    w.producer,
    w.colour,
    w.country,
    s.name AS shop,
    os.bottle_price_at_6 AS price_per_bottle_at_6,
    os.order_of_6 AS price_6_bottles_incl_delivery,
    o.price AS normal_price,
    o.promotion,
    CASE os.is_deal WHEN 1 THEN 'yes' ELSE 'no' END AS deal,
    os.deal_reason,
    ws.overall AS score,
    ws.n_awards AS awards,
    ws.best_award,
    os.value,
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
    w.name AS wine,
    w.vintage,
    w.colour,
    w.country,
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
    w.name AS wine,
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


def connect(path=None):
    path = path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode = WAL")
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
    con.executescript(VIEWS)
    con.execute("PRAGMA foreign_keys = ON")
    from .reference import seed

    seed(con)
    con.commit()
    return con
