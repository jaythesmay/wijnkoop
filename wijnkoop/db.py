import sqlite3
from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS shop (
    id INTEGER PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    website TEXT
);
CREATE TABLE IF NOT EXISTS wine (
    id INTEGER PRIMARY KEY,
    norm_key TEXT UNIQUE NOT NULL,
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
    url TEXT NOT NULL,
    title TEXT,
    volume_cl REAL,
    price REAL,
    price_per_75cl REAL,
    promotion TEXT,
    in_stock_online INTEGER,
    awards_count INTEGER,
    first_seen TEXT,
    last_seen TEXT,
    UNIQUE (shop_id, shop_product_id)
);
CREATE TABLE IF NOT EXISTS price_history (
    id INTEGER PRIMARY KEY,
    offer_id INTEGER NOT NULL REFERENCES offer(id),
    seen_on TEXT NOT NULL,
    price REAL,
    promotion TEXT,
    UNIQUE (offer_id, seen_on)
);
CREATE TABLE IF NOT EXISTS competition (
    id INTEGER PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    website TEXT,
    weight REAL DEFAULT 1.0
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
    source_url TEXT
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
CREATE TABLE IF NOT EXISTS run_log (
    id INTEGER PRIMARY KEY,
    job TEXT NOT NULL,
    started TEXT NOT NULL,
    finished TEXT,
    pages_ok INTEGER DEFAULT 0,
    pages_failed INTEGER DEFAULT 0,
    note TEXT
);

CREATE VIEW IF NOT EXISTS wine_list AS
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
    o.promotion,
    o.awards_count,
    CASE o.in_stock_online WHEN 1 THEN 'yes' WHEN 0 THEN 'no' END AS online_in_stock,
    o.last_seen,
    o.url
FROM offer o
JOIN wine w ON w.id = o.wine_id
JOIN shop s ON s.id = o.shop_id;
"""


def connect(path=None):
    path = path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    con.executescript(SCHEMA)
    return con
