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
    return f"{norm(name)}|{vintage or 'nv'}"
