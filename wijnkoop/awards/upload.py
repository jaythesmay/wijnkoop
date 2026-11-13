"""Results files that Loes saves herself (any competition, almost any format) as an official check.

A file can be a saved web page, CSV/TSV, Excel (.xlsx), PDF, JSON or plain text. It is turned
into rows of cells, then into award records:

1. Tables with a header row: columns are recognised by their names in English, Dutch, French,
   German or Italian (wine, producer, vintage, medal, points, country, colour...).
2. Tables without a header: each column's role is guessed from its contents (medal words,
   numbers from 80 to 100, years, country names, the longest text = the wine).
3. Text without columns (PDF text, card-style web pages): every line with a medal or score is
   an award; when that line holds no name, the line(s) just above it are the wine; a line that
   is only a medal ("GOLD") is a heading for the lines below it.

The awards are stored with source 'upload', linked to the wines in the shops, and compared with
what the shops print on their pages: confirmed, different medal, or not in the official list.
"""
import csv
import datetime as dt
import html as htmllib
import io
import json
import re
import sqlite3
import zipfile
from collections import Counter

from rapidfuzz import fuzz, process

from ..normalize import cuvee_key, norm, strip_accents
from ..reference import MEDALS, OTHER_COMPETITION, POINTS20_RE, competition_id, find_competition

MAX_BYTES = 40 * 1024 * 1024
AUTO, REVIEW = 88, 75

HEADER_WORDS = {
    "name": ["wine", "wijn", "name", "naam", "product", "cuvee", "label", "vin", "wein", "vino", "description",
             "omschrijving", "brand name", "wine name", "title", "titel"],
    "producer": ["producer", "producent", "winery", "wijnhuis", "wijnmaker", "domaine", "estate", "producteur",
                 "erzeuger", "weingut", "produttore", "company", "bedrijf", "exhibitor", "maker", "chateau",
                 "bodega", "cantina", "house"],
    "vintage": ["vintage", "jaargang", "oogst", "oogstjaar", "millesime", "jahrgang", "annata", "year", "jaar"],
    "medal": ["medal", "medaille", "award", "prijs", "result", "resultaat", "trophy", "auszeichnung", "medaglia",
              "distinction", "onderscheiding", "level"],
    "points": ["points", "score", "punten", "pts", "rating", "punkte", "punti", "note"],
    "country": ["country", "land", "pays", "paese", "origin", "herkomst"],
    "region": ["region", "regio", "streek", "appellation", "area", "gebied", "denomination", "subregion"],
    "colour": ["colour", "color", "kleur", "wine type", "type", "style", "category", "categorie", "couleur",
               "farbe", "colore"],
}
COUNTRIES = {
    "france", "frankrijk", "italy", "italie", "italia", "spain", "spanje", "espana", "portugal", "germany",
    "duitsland", "deutschland", "austria", "oostenrijk", "osterreich", "south africa", "zuid afrika", "chile",
    "chili", "argentina", "argentinie", "australia", "australie", "new zealand", "nieuw zeeland", "usa",
    "united states", "verenigde staten", "hungary", "hongarije", "greece", "griekenland", "england",
    "engeland", "united kingdom", "uk", "china", "israel", "lebanon", "libanon", "georgia", "georgie",
    "uruguay", "brazil", "brazilie", "canada", "slovenia", "slovenie", "croatia", "kroatie", "romania",
    "roemenie", "bulgaria", "bulgarije", "moldova", "switzerland", "zwitserland", "netherlands", "nederland",
    "belgium", "belgie", "japan", "mexico", "turkey", "turkije", "north macedonia", "serbia", "czech republic",
}
COLOURS = [(r"\bros[eé]\b|rosado|rosato|blush", "rosé"), (r"\b(red|rood|rode|rouge|rosso|tinto|rot)\b", "red"),
           (r"\b(white|wit|witte|blanc|bianco|blanco|weiss|weiß)\b", "white")]
TITLE_WORDS = re.compile(r"\b(results?|awards?|winners?|competition|concours|wettbewerb|wedstrijd|uitslag|"
                         r"medal(?:ist)?s|preisträger|palmar[eè]s|edition|editie)\b", re.I)
YEAR = re.compile(r"\b(19[5-9]\d|20[0-4]\d)\b")
NUMBER = re.compile(r"^\s*(\d{2,3}(?:[.,]\d{1,2})?)\s*(?:pts?|points?|punten|punti|punkte|/\s*100)?\s*$", re.I)
POINTS_IN_TEXT = re.compile(r"\b(\d{2,3}(?:[.,]\d)?)\s*(?:pts?\b|points?\b|punten\b|punti\b|punkte\b|/\s*100\b|p\b)",
                            re.I)


def _plain(text) -> str:
    return re.sub(r"\s+", " ", strip_accents(str(text or "")).lower()).strip()


# Medal words must stand alone here: "Argentino" holds "argent", "Golden Valley" holds "gold".
MEDAL_WORDS = [(re.compile(rf"\b(?:{pattern})\b"), name) for pattern, name in MEDALS]


def medal_in(text: str) -> str | None:
    t = _plain(text)
    for rx, name in MEDAL_WORDS:
        if rx.search(t):
            return name
    return None


def points_in(text: str, column: bool = False) -> float | None:
    t = str(text or "")
    m = POINTS20_RE.search(t)
    if m:
        return round(50 + 2.5 * float(m.group(1).replace(",", ".")), 1)
    m = NUMBER.match(t) if column else POINTS_IN_TEXT.search(t)
    if m:
        v = float(m.group(1).replace(",", "."))
        return v if 70 <= v <= 100 else None
    return None


def colour_in(text: str) -> str | None:
    t = _plain(text)
    for pattern, name in COLOURS:
        if re.search(pattern, t):
            return name
    return None


# ---------------------------------------------------------------- files -> rows of cells

def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def _text_rows(text: str) -> list[list[str]]:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return []
    sample = "\n".join(lines[:50])
    for delim in ("\t", ";", ",", "|"):
        counts = [ln.count(delim) for ln in lines[:50]]
        common = Counter(counts).most_common(1)[0]
        if common[0] >= 1 and common[1] >= max(2, len(counts) * 0.6):
            return [[c.strip() for c in row] for row in csv.reader(io.StringIO("\n".join(lines)), delimiter=delim)]
    try:
        dialect = csv.Sniffer().sniff(sample)
        rows = list(csv.reader(io.StringIO("\n".join(lines)), dialect))
        if sum(len(r) > 1 for r in rows) > len(rows) * 0.6:
            return [[c.strip() for c in r] for r in rows]
    except csv.Error:
        pass
    # Columns separated by runs of spaces (text copied from a PDF or a fixed-width export)
    split = [re.split(r"\s{2,}|\t", ln.strip()) for ln in lines]
    if sum(len(r) > 2 for r in split) > len(split) * 0.6:
        return split
    return [[ln.strip()] for ln in lines]


def _html_rows(text: str) -> list[list[str]]:
    from selectolax.lexbor import LexborHTMLParser

    tree = LexborHTMLParser(text)
    for s in tree.css("script, style, noscript, svg, nav, header, footer"):
        s.decompose()
    rows = []
    for tr in tree.css("table tr"):
        cells = [re.sub(r"\s+", " ", c.text()).strip() for c in tr.css("th, td")]
        if sum(1 for c in cells if c) >= 2:
            rows.append(cells)
    if len(rows) >= 3:
        return rows
    body = tree.body or tree.root
    text = body.text(separator="\n") if body else ""
    return [[re.sub(r"\s+", " ", htmllib.unescape(ln)).strip()] for ln in text.splitlines() if ln.strip()]


def _xlsx_rows(data: bytes) -> list[list[str]]:
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    rows = []
    for ws in wb.worksheets:
        for row in ws.iter_rows(values_only=True):
            cells = ["" if v is None else (str(int(v)) if isinstance(v, float) and v.is_integer() else str(v)).strip()
                     for v in row]
            if any(cells):
                rows.append(cells)
    return rows


def _pdf_rows(data: bytes) -> list[list[str]]:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    pages = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text(extraction_mode="layout") or "")
        except Exception:
            pages.append(page.extract_text() or "")
    text = "\n".join(pages)
    return _fixed_width_rows(text) or _text_rows(text)


def _fixed_width_rows(text: str) -> list[list[str]] | None:
    """Columns of a laid-out table: cut every line where the header's column names start."""
    lines = text.splitlines()
    for h, line in enumerate(lines[:60]):
        tokens = [(m.start(), m.group(0)) for m in re.finditer(r"\S+(?: \S+)*", line)]
        if len(tokens) < 2:
            continue
        roles = _header_roles([t for _, t in tokens])
        if "name" not in roles.values() or len(roles) < 2:
            continue
        cuts = [0] + [pos for pos, _ in tokens[1:]]
        rows = [[t for _, t in tokens]]
        for ln in lines[h + 1:]:
            if not ln.strip():
                continue
            cells, start = [], 0
            for cut in cuts[1:] + [None]:
                if cut is not None:
                    # move the cut to the nearest space so a word is never split
                    best = min((abs(k - cut), k) for k in range(max(start, cut - 4), min(len(ln), cut + 5) + 1)
                               if k >= len(ln) or ln[k] == " " or k == 0) if cut < len(ln) else (0, len(ln))
                    cut = best[1]
                cells.append(re.sub(r"\s+", " ", ln[start:cut]).strip())
                start = cut if cut is not None else start
            rows.append(cells)
        return rows
    return None


def _json_rows(text: str) -> list[list[str]]:
    data = json.loads(text)

    def find_list(d):
        if isinstance(d, list) and d and isinstance(d[0], dict):
            return d
        if isinstance(d, dict):
            for v in d.values():
                found = find_list(v)
                if found:
                    return found
        return None

    items = find_list(data) or []
    keys = list(dict.fromkeys(k for it in items for k in it if not isinstance(it[k], (dict, list))))
    return [keys] + [[str(it.get(k, "") or "") for k in keys] for it in items]


def read_file(filename: str, data: bytes) -> tuple[list[list[str]], str]:
    """Rows of cells, and what kind of file it was."""
    name = filename.lower()
    if data[:4] == b"%PDF" or name.endswith(".pdf"):
        return _pdf_rows(data), "PDF"
    if data[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            if any(n.startswith("xl/") for n in z.namelist()):
                return _xlsx_rows(data), "Excel"
        raise ValueError("This zip file is not an Excel workbook. Save it as .xlsx or CSV.")
    if name.endswith(".xls"):
        raise ValueError("Old Excel files (.xls) cannot be read. Save it as .xlsx or CSV.")
    text = _decode(data)
    head = text[:3000].lower()
    if name.endswith((".htm", ".html", ".mhtml")) or "<html" in head or "<table" in head or "<!doctype" in head:
        return _html_rows(text), "web page"
    if name.endswith(".json") or text.lstrip()[:1] in "[{":
        try:
            return _json_rows(text), "JSON"
        except ValueError:
            pass
    return _text_rows(text), ("CSV" if filename.lower().endswith((".csv", ".tsv")) else "text")


# ---------------------------------------------------------------- rows -> award records

def _header_roles(row: list[str]) -> dict:
    roles = {}
    for i, cell in enumerate(row):
        c = _plain(cell)
        if not c or len(c) > 40:
            continue
        best = None
        for role, words in HEADER_WORDS.items():
            for w in words:
                if c == w or re.search(rf"\b{re.escape(w)}\b", c):
                    # "wine name" beats "wine"; exact beats partial
                    score = (2 if c == w else 1, len(w))
                    if best is None or score > best[0]:
                        best = (score, role)
        if best and best[1] not in roles.values():
            roles[i] = best[1]
    return roles


def _content_roles(rows: list[list[str]]) -> dict:
    width = max(len(r) for r in rows)
    roles, stats = {}, []
    for i in range(width):
        col = [r[i] for r in rows if i < len(r) and r[i]]
        n = len(col) or 1
        stats.append({
            "medal": sum(1 for c in col if medal_in(c) and len(c) < 40) / n,
            "points": sum(1 for c in col if points_in(c, column=True)) / n,
            "vintage": sum(1 for c in col if re.fullmatch(r"(19|20)\d{2}|nv|n\.v\.", _plain(c))) / n,
            "country": sum(1 for c in col if _plain(c) in COUNTRIES) / n,
            "colour": sum(1 for c in col if colour_in(c) and len(c) < 25) / n,
            "length": sum(len(c) for c in col) / n,
            "filled": len(col),
        })
    for role in ("medal", "points", "vintage", "country", "colour"):
        i = max(range(width), key=lambda k: stats[k][role])
        if stats[i][role] >= 0.5 and i not in roles:
            roles[i] = role
    text_cols = sorted((i for i in range(width) if i not in roles and stats[i]["filled"] >= len(rows) * 0.5),
                       key=lambda k: -stats[k]["length"])
    if text_cols:
        roles[text_cols[0]] = "name"
    if len(text_cols) > 1 and stats[text_cols[1]]["length"] >= 4:
        roles[text_cols[1]] = "producer"
    return roles


def _record(name, producer=None, vintage=None, medal=None, points=None, country=None, colour=None,
            region=None, raw=""):
    name = re.sub(r"\s+", " ", str(name or "")).strip(" -–|,;:")
    if vintage is None:
        m = YEAR.search(name)
        if m:
            vintage = int(m.group(1))
    if vintage is not None:
        name = re.sub(rf"\s*\b{vintage}\b\s*", " ", name).strip(" -–|,;:")
    if not name or len(name) < 3 or not (medal or points):
        return None
    return {"name": name, "producer": (producer or "").strip() or None, "vintage": vintage, "medal": medal,
            "points": points, "country": (country or "").strip() or None, "colour": colour,
            "region": (region or "").strip() or None, "raw": raw[:300]}


def _table_records(rows, roles) -> list[dict]:
    out = []
    for r in rows:
        get = {role: (r[i] if i < len(r) else "") for i, role in roles.items()}
        if not any(get.values()):
            continue
        medal = medal_in(get.get("medal", "")) if "medal" in get else None
        points = points_in(get.get("points", ""), column=True) if "points" in get else None
        if "medal" in get and points is None:
            points = points_in(get["medal"])
        if not (medal or points):  # medal or score somewhere else in the row
            whole = " | ".join(r)
            medal = medal or medal_in(whole)
            points = points or points_in(whole)
        v = YEAR.search(get.get("vintage", "") or "")
        rec = _record(get.get("name"), get.get("producer"), int(v.group(1)) if v else None, medal, points,
                      get.get("country"), colour_in(get.get("colour", "")) or colour_in(get.get("name", "")),
                      get.get("region"), " | ".join(c for c in r if c))
        if rec:
            out.append(rec)
    return out


def _line_records(lines: list[str]) -> tuple[list[dict], bool]:
    """Awards from lines of text. Two layouts:
    cards   - name, producer, country, then "Gold" and/or "95 points" on their own lines;
    sections - a heading "GOLD" followed by one line per wine."""
    lines = [ln.strip() for ln in lines if ln.strip()]
    bare = [ln for ln in lines if (medal_in(ln) or points_in(ln) or points_in(ln, column=True))
            and len(re.findall(r"[^\W\d_]{2,}", _strip_award_words(ln))) < 2]
    sections = len(bare) < max(2, len(lines) * 0.1)
    if bare and not sections:
        # A heading layout ends with wines below the last heading; a card layout ends with a medal or score.
        last = max(i for i, ln in enumerate(lines) if ln in bare)
        tail = [ln for ln in lines[last + 1:] if not (medal_in(ln) or points_in(ln))]
        sections = any(YEAR.search(ln) and len(re.findall(r"[^\W\d_]{2,}", ln)) >= 2
                       and not re.search(r"©|copyright|privacy|cookie", ln, re.I) for ln in tail)
    out, buffer, section = [], [], None
    for text in lines:
        medal, points = medal_in(text), points_in(text) or (points_in(text, column=True) if out else None)
        if medal or points:
            rest = _strip_award_words(text)
            if len(re.findall(r"[^\W\d_]{2,}", rest)) >= 2:
                rec = _record(rest, medal=medal, points=points, colour=colour_in(text), raw=text)
                if rec:
                    out.append(rec)
                buffer = []
            elif sections and medal and not points and len(text) < 40:
                section, buffer = medal, []  # a heading such as "GOLD MEDALS"
            elif buffer:
                name_lines = [b for b in buffer if _plain(b) not in COUNTRIES]
                name = " ".join(name_lines[-3:])
                country = next((b for b in buffer if _plain(b) in COUNTRIES), None)
                rec = _record(name, medal=medal, points=points, country=country, colour=colour_in(name),
                              raw=" | ".join(buffer + [text]))
                if rec:
                    out.append(rec)
                buffer = []
            elif out and not buffer:
                # "95 points" or "Gold" on the next line of the same card
                last = out[-1]
                if points and not last["points"]:
                    last["points"] = points
                if medal and not last["medal"]:
                    last["medal"] = medal
            continue
        if sections and section and len(re.findall(r"[^\W\d_]{2,}", text)) >= 2 and len(text) < 160:
            rec = _record(text, medal=section, colour=colour_in(text), raw=f"{section} | {text}")
            if rec:
                out.append(rec)
            continue
        buffer.append(text)
        buffer = buffer[-4:]
    return out, sections


def _strip_award_words(text: str) -> str:
    t = text
    for pattern, _ in MEDALS:
        t = re.sub(rf"\b(?:{pattern})\b", " ", t, flags=re.I)
    t = POINTS_IN_TEXT.sub(" ", POINTS20_RE.sub(" ", t))
    t = re.sub(r"\b(medal|medaille|médaille|award|winner)\b", " ", t, flags=re.I)
    return re.sub(r"\s+", " ", t).strip(" -–|,;:()")


def records_from_rows(rows: list[list[str]]) -> tuple[list[dict], str]:
    """Award records, and a short explanation of how the file was understood."""
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        return [], "The file is empty."
    multi = [r for r in rows if sum(1 for c in r if c.strip()) >= 2]
    if len(multi) >= 2 and len(multi) >= len(rows) * 0.3:
        for h in range(min(10, len(multi))):
            roles = _header_roles(multi[h])
            if "name" in roles.values() and len(roles) >= 2:
                recs = _table_records(multi[h + 1:], roles)
                if recs:
                    names = ", ".join(f"{role} = '{multi[h][i]}'" for i, role in sorted(roles.items()))
                    return recs, f"Table with a header row: {names}."
        roles = _content_roles(multi) if len(multi) >= 3 else {}
        if "name" in roles.values():
            recs = _table_records(multi, roles)
            if recs:
                names = ", ".join(f"column {i + 1} = {role}" for i, role in sorted(roles.items()))
                return recs, f"Table without a header; columns guessed from their contents: {names}."
    lines = [" ".join(c for c in r if c) for r in rows]
    recs, sections = _line_records(lines)
    if sections:
        return recs, ("Text without columns, in sections: a heading such as 'GOLD' followed by one wine per line.")
    return recs, ("Text without columns, in blocks: a line with a medal or score is an award, "
                  "and the lines just above it name the wine.")


def detect(filename: str, rows: list[list[str]]) -> tuple[tuple | None, int | None]:
    """Competition and year from the file name and the first part of the text."""
    text = filename.replace("_", " ").replace("-", " ") + " " + " ".join(" ".join(r) for r in rows[:40])
    comp = find_competition(text)
    # The year comes from the file name or from a title line ("Mundus Vini 2025 results"),
    # never from a wine's vintage.
    titles = [" ".join(r) for r in rows[:40]
              if TITLE_WORDS.search(" ".join(r)) or find_competition(" ".join(r))]
    years = [int(y) for y in YEAR.findall(filename)] or [
        int(y) for y in YEAR.findall(" ".join(titles)) if int(y) >= 2015]
    return comp, (years[0] if years else None)


# ---------------------------------------------------------------- store, link, compare

def _match_index(con):
    rows = con.execute("""SELECT c.id, c.name, c.producer, c.colour FROM cuvee c WHERE c.is_wine = 1
                          AND c.id IN (SELECT w.cuvee_id FROM wine w JOIN offer o ON o.wine_id = w.id)""").fetchall()
    keys = {}
    for r in rows:
        full = cuvee_key(r["name"])
        if r["producer"] and norm(r["producer"]) not in norm(r["name"]):
            full = cuvee_key(f"{r['producer']} {r['name']}")
        keys[r["id"]] = (full, r["colour"])
    return keys


def _best(keys: dict, rec: dict) -> tuple[int | None, float]:
    name = rec["name"]
    if rec.get("producer") and norm(rec["producer"]) not in norm(name):
        name = f"{rec['producer']} {name}"
    q = cuvee_key(name)
    if not q:
        return None, 0
    choices = {cid: k for cid, (k, colour) in keys.items()
               if not (rec.get("colour") and colour and colour != rec["colour"])}
    hit = process.extractOne(q, choices, scorer=fuzz.token_sort_ratio, score_cutoff=REVIEW)
    if not hit:
        # a shorter shop name inside a longer official name ("... Reserva Especial Selección")
        hit = process.extractOne(q, choices, scorer=fuzz.token_set_ratio, score_cutoff=95)
        if hit and abs(len(hit[0].split()) - len(q.split())) > 3:
            hit = None
    return (hit[2], hit[1]) if hit else (None, 0)


def import_upload(con, filename: str, data: bytes, competition: str | None = None, year: int | None = None,
                  today: dt.date | None = None) -> dict:
    """Read one results file, store its awards, link them to shop wines and check the shops' claims."""
    if len(data) > MAX_BYTES:
        raise ValueError("The file is larger than 40 MB.")
    try:
        report = _import(con, filename, data, competition, year, today)
    except Exception:
        con.rollback()
        raise
    from ..scores import recompute

    try:
        recompute(con)
    except sqlite3.OperationalError:
        # A shop run holds the database for a long time: the awards are saved, and that run
        # computes the scores again when it finishes.
        con.rollback()
    return upload_report(con, report)


def _import(con, filename, data, competition, year, today) -> int:
    from .importer import Matcher

    today_s = (today or dt.date.today()).isoformat()
    rows, kind = read_file(filename, data)
    records, how = records_from_rows(rows)
    found_comp, found_year = detect(filename, rows)
    comp = None
    if competition:
        r = con.execute("SELECT short, name, kind, weight FROM competition WHERE short=? OR name=?",
                        (competition, competition)).fetchone()
        comp = tuple(r) if r else find_competition(competition) or (competition[:40], competition, "competition", 0.4)
    comp = comp or found_comp or OTHER_COMPETITION
    year = year or found_year
    comp_id = competition_id(con, comp)
    upload_id = con.execute(
        "INSERT INTO award_upload (filename, kind, competition_id, year, uploaded_on, how, rows_read) "
        "VALUES (?,?,?,?,?,?,?)", (filename, kind, comp_id, year, today_s, how, len(rows))).lastrowid
    source_url = f"upload:{upload_id}:{filename}"
    matcher, keys = Matcher(con), _match_index(con)
    stored = linked = review = 0
    for rec in records:
        raw = " ".join(str(x) for x in (rec.get("producer"), rec["name"], rec.get("vintage"), "-", comp[0], year,
                                         rec.get("medal"), f"{rec['points']:g} pts" if rec.get("points") else None)
                       if x)
        cur = con.execute(
            """INSERT OR IGNORE INTO award (competition_id, year, producer_raw, name_raw, vintage, colour, country,
                                            medal, points, source, source_url, raw_text, found_on)
               VALUES (?,?,?,?,?,?,?,?,?,'upload',?,?,?)""",
            (comp_id, year, rec.get("producer"), rec["name"], rec.get("vintage"), rec.get("colour"),
             rec.get("country"), rec.get("medal"), rec.get("points"), source_url, raw, today_s))
        if not cur.rowcount:
            continue
        stored += 1
        how_linked = _link(con, cur.lastrowid, rec, matcher, keys)
        linked += how_linked == "auto"
        review += how_linked == "review"
    con.execute("UPDATE award_upload SET awards=?, linked=?, to_review=? WHERE id=?",
                (stored, linked, review, upload_id))
    _keep_file(upload_id, filename, data)
    con.commit()
    return upload_id


def _link(con, award_id: int, rec: dict, matcher, keys: dict, new_only: int = 0) -> str | None:
    """Link one official award to a shop wine. Returns 'auto', 'review' (probable) or None.
    new_only: only cuvees with a higher id count (the ones added since the last check)."""
    cid, score = (None, 0)
    if rec.get("producer"):
        cid, score = matcher.best({"producer": rec["producer"], "name": rec["name"], "colour": rec.get("colour")})
        if cid is not None and cid <= new_only:
            cid, score = None, 0
    if score < REVIEW:
        cid, score = _best(keys, rec)
    if not cid or score < REVIEW:
        return None
    wine_id = matcher.wine_for(cid, rec.get("vintage"))
    confirmed = 1 if score >= AUTO else None
    con.execute("INSERT OR IGNORE INTO award_match (award_id, wine_id, match_score, confirmed) "
                "VALUES (?,?,?,?)", (award_id, wine_id, round(score / 100, 3), confirmed))
    return "auto" if confirmed else "review"


# ---------------------------------------------------------------- kept files, later links

def stored_file(upload_id: int, filename: str):
    """Where the uploaded file itself is kept (in the data folder, next to the database)."""
    from .. import config

    safe = re.sub(r"[^\w.\- ]+", "_", filename).strip() or "upload"
    return config.DATA_DIR / "uploads" / f"{upload_id}-{safe}"


def _keep_file(upload_id: int, filename: str, data: bytes):
    path = stored_file(upload_id, filename)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    except OSError:  # the awards are stored anyway; the copy of the file is a convenience
        pass


RELINK_SETTING = "upload_relink_cuvee_id"


def relink_uploads(con) -> int:
    """Official results never change, but WijnKoop keeps finding new wines (new shops, new
    vintages). Check every uploaded award that is not linked yet against the wines added since
    the last check. Returns the number of awards newly linked."""
    from .importer import Matcher

    row = con.execute("SELECT value FROM setting WHERE key=?", (RELINK_SETTING,)).fetchone()
    since = int(row[0]) if row and str(row[0]).isdigit() else 0
    top = con.execute("SELECT COALESCE(MAX(id), 0) FROM cuvee").fetchone()[0]
    if top <= since:
        return 0
    open_awards = con.execute(
        """SELECT a.id, a.name_raw, a.producer_raw, a.colour, a.vintage, a.source_url FROM award a
           WHERE a.source = 'upload' AND NOT EXISTS (SELECT 1 FROM award_match m WHERE m.award_id = a.id)"""
    ).fetchall()
    found = 0
    if open_awards:
        keys = {cid: k for cid, k in _match_index(con).items() if cid > since}
        matcher = Matcher(con)
        touched = set()
        for a in open_awards:
            rec = {"name": a["name_raw"], "producer": a["producer_raw"], "colour": a["colour"],
                   "vintage": a["vintage"]}
            if _link(con, a["id"], rec, matcher, keys, new_only=since):
                found += 1
                touched.add(a["source_url"].split(":")[1])
        for upload_id in touched:
            _count_links(con, int(upload_id))
    con.execute("INSERT OR REPLACE INTO setting (key, value) VALUES (?, ?)", (RELINK_SETTING, str(top)))
    con.commit()
    return found


def _count_links(con, upload_id: int):
    like = f"upload:{upload_id}:%"
    linked, review = con.execute(
        """SELECT COALESCE(SUM(m.confirmed = 1), 0), COALESCE(SUM(m.confirmed IS NULL), 0)
           FROM award a JOIN award_match m ON m.award_id = a.id
           WHERE a.source = 'upload' AND a.source_url LIKE ?""", (like,)).fetchone()
    con.execute("UPDATE award_upload SET linked=?, to_review=? WHERE id=?", (linked, review, upload_id))


def upload_report(con, upload_id: int) -> dict:
    up = con.execute("""SELECT u.*, c.short, c.name AS competition FROM award_upload u
                        JOIN competition c ON c.id = u.competition_id WHERE u.id=?""", (upload_id,)).fetchone()
    if not up:
        return {}
    up = dict(up)
    like = f"upload:{upload_id}:%"
    sample = [dict(r) for r in con.execute(
        "SELECT producer_raw, name_raw, vintage, medal, points, colour, country FROM award "
        "WHERE source='upload' AND source_url LIKE ? ORDER BY id LIMIT 15", (like,))]
    matched = [dict(r) for r in con.execute(
        """SELECT a.id AS award_id, a.name_raw, a.vintage AS award_vintage, a.medal, a.points, m.wine_id,
                  w.name || COALESCE(' ' || w.vintage, '') AS wine, m.match_score, m.confirmed,
                  (SELECT GROUP_CONCAT(DISTINCT COALESCE(sa.medal, '') || CASE WHEN sa.points IS NULL THEN '' ELSE ' ' || printf('%g', sa.points) END)
                     FROM award_match sm JOIN award sa ON sa.id = sm.award_id
                    WHERE sm.wine_id = m.wine_id AND sa.source = 'shop page' AND sa.competition_id = a.competition_id
                      AND (sa.year IS NULL OR a.year IS NULL OR sa.year = a.year)) AS shop_says,
                  EXISTS (SELECT 1 FROM offer o WHERE o.wine_id = m.wine_id AND o.is_current = 1) AS on_sale
           FROM award a JOIN award_match m ON m.award_id = a.id JOIN wine w ON w.id = m.wine_id
           WHERE a.source = 'upload' AND a.source_url LIKE ?
           ORDER BY on_sale DESC, a.points DESC, a.medal""", (like,))]
    for r in matched:
        official = " ".join(x for x in (r["medal"] or "", f"{r['points']:g}" if r["points"] else "") if x)
        if not r["shop_says"]:
            r["check"] = "not on the shop page"
        elif any(_same(official, s) for s in r["shop_says"].split(",")):
            r["check"] = "confirmed"
        else:
            r["check"] = "different"
    # Shop claims for this competition and year whose wine is not in the uploaded list
    missing = []
    if up["awards"]:
        missing = [dict(r) for r in con.execute(
            """SELECT DISTINCT w.id AS wine_id, w.name || COALESCE(' ' || w.vintage, '') AS wine, sa.medal, sa.points,
                      sa.raw_text
               FROM award sa JOIN award_match sm ON sm.award_id = sa.id JOIN wine w ON w.id = sm.wine_id
               WHERE sa.source = 'shop page' AND sa.competition_id = ? AND (? IS NULL OR sa.year = ?)
                 AND NOT EXISTS (SELECT 1 FROM award_match um JOIN award ua ON ua.id = um.award_id
                                 WHERE um.wine_id = w.id AND ua.source = 'upload' AND ua.source_url LIKE ?)
               ORDER BY w.name LIMIT 200""", (up["competition_id"], up["year"], up["year"], like))]
    counts = Counter(r["check"] for r in matched)
    up["kept"] = stored_file(upload_id, up["filename"]).exists()
    return {"upload": up, "sample": sample, "matched": matched, "missing": missing, "counts": dict(counts)}


def _same(official: str, shop: str) -> bool:
    o, s = _plain(official), _plain(shop)
    if not s:
        return False
    om, sm = medal_in(o), medal_in(s)
    if om and sm:
        return om == sm
    on, sn = re.findall(r"\d+(?:\.\d)?", o), re.findall(r"\d+(?:\.\d)?", s)
    return bool(on and sn and abs(float(on[0]) - float(sn[0])) < 0.6)


def uploads(con) -> list[dict]:
    rows = [dict(r) for r in con.execute(
        """SELECT u.id, u.filename, u.kind, u.year, u.uploaded_on, u.awards, u.linked, u.to_review, c.short
           FROM award_upload u JOIN competition c ON c.id = u.competition_id ORDER BY u.id DESC""")]
    for r in rows:
        r["kept"] = stored_file(r["id"], r["filename"]).exists()
    return rows


def delete_upload(con, upload_id: int) -> int:
    like = f"upload:{upload_id}:%"
    ids = [r[0] for r in con.execute("SELECT id FROM award WHERE source='upload' AND source_url LIKE ?", (like,))]
    con.executemany("DELETE FROM award_match WHERE award_id=?", [(i,) for i in ids])
    con.executemany("DELETE FROM award WHERE id=?", [(i,) for i in ids])
    up = con.execute("SELECT filename FROM award_upload WHERE id=?", (upload_id,)).fetchone()
    con.execute("DELETE FROM award_upload WHERE id=?", (upload_id,))
    con.commit()
    if up:
        stored_file(upload_id, up["filename"]).unlink(missing_ok=True)
    from ..scores import recompute

    recompute(con)
    return len(ids)
