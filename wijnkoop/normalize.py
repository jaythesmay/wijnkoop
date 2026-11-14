import re
import unicodedata

ABBREVIATIONS = {
    "ch": "chateau",
    "dom": "domaine",
    "st": "saint",
    "ste": "sainte",
}


def strip_accents(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)
    )


def norm(text: str | None) -> str:
    if not text:
        return ""
    text = strip_accents(text).lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    words = [ABBREVIATIONS.get(w, w) for w in text.split()]
    return " ".join(words)


def wine_key(name: str, vintage: int | None, volume_cl: float | None = None) -> str:
    """Key that identifies a wine + vintage across one shop's catalogue."""
    return f"{cuvee_key(name)}|{vintage or 'nv'}"


def cuvee_key(name: str) -> str:
    """Key for a wine regardless of vintage: the normalised name without years."""
    return re.sub(r"\s+", " ", re.sub(r"\b(19|20)\d{2}\b", " ", norm(name))).strip()


# Words that may stand between or before the producer's own words at the start of a name
# ("Herdade do Esporao", "Chateau de la Riviere") without being the wine's name.
_JOIN = set("de del della dei di da do dos das du d la le les l y e et und van von the and of "
            "chateau domaine bodega bodegas herdade quinta tenuta castello cantina cantine weingut "
            "maison cave caves azienda agricola vina finca hacienda clos mas".split())


def split_estate(name: str | None, producer: str | None) -> tuple[str | None, str]:
    """(estate, wine) from a full name and its producer: "Herdade do Esporao Reserva Tinto" with
    producer "Esporao" gives ("Esporao", "Reserva Tinto"). The producer's words are taken off the
    start of the name only; when the name does not start with them, the name stays whole."""
    name = (name or "").strip()
    if not producer:
        return None, name
    own = set(norm(producer).split())
    needed = own - _JOIN  # all of the producer's own words must be there ("Cape Dream" is not "Cape Mentelle")
    words = name.split()
    cut, used, seen = 0, False, set()
    for i, w in enumerate(words):
        n = norm(w)
        if not n:
            continue
        parts = n.split()
        if all(p in own for p in parts):
            used = used or any(p not in _JOIN for p in parts)
            seen.update(parts)
            cut = i + 1
        elif all(p in _JOIN for p in parts) and not used:
            continue  # "Herdade do" before "Esporao"
        else:
            break
    rest = " ".join(words[cut:]).strip(" -–,:") if used and needed <= seen else name
    return producer, rest or name
