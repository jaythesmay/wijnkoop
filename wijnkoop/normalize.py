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
