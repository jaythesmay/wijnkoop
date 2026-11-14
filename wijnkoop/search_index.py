"""A search index for the search box, so a search does not go through every wine.

search_text (an FTS5 table with trigrams) holds one normalised line per offer on sale: wine,
producer, vintage, grapes. It is rebuilt after the scores are worked out. A search first asks it
for the 300 offers that share the most three-letter pieces with what was typed (so a typo still
finds the wine), and only those are compared letter by letter. Without FTS5 (an old SQLite)
nothing is indexed and the search looks at every wine, as before.
"""
import sqlite3

from .normalize import norm

CANDIDATES = 300


def ensure(con) -> bool:
    try:
        con.execute("CREATE VIRTUAL TABLE IF NOT EXISTS search_text USING fts5(text, tokenize='trigram')")
        return True
    except sqlite3.Error:
        return False


def rebuild(con) -> int:
    if not ensure(con):
        return 0
    rows = con.execute("SELECT offer_id, wine, producer, vintage, grapes FROM best_buys").fetchall()
    con.execute("DELETE FROM search_text")
    con.executemany("INSERT INTO search_text (rowid, text) VALUES (?, ?)",
                    [(r[0], norm(f"{r[1]} {r[2] or ''} {r[3] or ''} {r[4] or ''}")) for r in rows])
    con.commit()
    return len(rows)


def candidates(con, q: str) -> list[int] | None:
    """Offer ids worth comparing, best first; None when there is no index (search everything)."""
    pieces = {w[i:i + 3] for w in norm(q).split() if len(w) >= 3 for i in range(len(w) - 2)}
    if not pieces:
        return None
    match = " OR ".join(f'"{p}"' for p in sorted(pieces))
    try:
        if not con.execute("SELECT 1 FROM search_text LIMIT 1").fetchone():
            return None
        return [r[0] for r in con.execute(
            "SELECT rowid FROM search_text WHERE search_text MATCH ? ORDER BY rank LIMIT ?", (match, CANDIDATES))]
    except sqlite3.Error:
        return None
