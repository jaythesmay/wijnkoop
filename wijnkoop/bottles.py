"""Bottle sizes from a product name or address, so a half bottle or a magnum is compared fairly.

Every price is compared per 75 cl. Shops do not always say the size in a separate field (some
readers then assume 75 cl), but the name or the address usually does: "37,5cl", "375 ml",
"0,375 l", "halve fles", "Magnum".
"""
import re

NAMED = [  # checked in this order: "dubbele magnum" before "magnum"
    (r"\b(dubbele|double)[\s-]+magnum\b|\bjeroboam\b", 300.0),
    (r"\brehoboam\b", 450.0),
    (r"\b(methusalem|methuselah)\b", 600.0),  # not "imperial": Moët Impérial is a normal bottle
    (r"\bmagnum\b", 150.0),
    (r"\b(halve|half)[\s-]+(fles|flesje|bottle)\b|\bdemi[\s-]+bouteille\b|\bmezza\b|\bhalbe\s+flasche\b", 37.5),
]
SIZE = re.compile(
    r"(?<![\dx×])(?<!\dx )(\d{1,4}(?:[.,]\d{1,3})?)\s*-?\s*(cl|ml|ltr|liter|litre|l)\b", re.I)
PLAUSIBLE = (18.0, 600.0)  # a piccolo up to an imperial, in cl


def _cl(value: str, unit: str) -> float | None:
    v = float(value.replace(",", "."))
    unit = unit.lower()
    cl = v / 10 if unit == "ml" else v * 100 if unit in ("l", "ltr", "liter", "litre") else v
    return round(cl, 1) if PLAUSIBLE[0] <= cl <= PLAUSIBLE[1] else None


def volume_from_text(*texts: str | None) -> float | None:
    """The bottle size in cl named in a product name or address, or None when none is named.
    Packs ("6x75cl") are not a bottle size."""
    for text in texts:
        if not text:
            continue
        t = text.lower()
        if "://" in t or "/" in t:  # an address: "-37-5cl", "375ml", "0-375l"
            t = re.sub(r"(?<!\d)(\d{1,2})-(\d{1,3})(?=\s*-?(?:cl|ml|ltr|liter|litre|l)\b)", r"\1.\2", t.replace("_", "-"))
            t = re.sub(r"[-/]", " ", t)
        for m in SIZE.finditer(t):
            before = t[max(0, m.start() - 3):m.start()]
            if re.search(r"\d\s*[x×]\s*$", before):
                continue
            cl = _cl(m.group(1), m.group(2))
            if cl:
                return cl
        for pattern, cl in NAMED:
            if re.search(pattern, t):
                return cl
    return None


def fix_volume(volume_cl: float | None, *texts: str | None) -> float | None:
    """The shop's size, unless it is missing or the usual 75 cl (which some readers assume) while
    the name or address names another size."""
    named = volume_from_text(*texts)
    if named and (volume_cl is None or volume_cl == 75.0):
        return named
    return volume_cl


def bottle_size(cl) -> str:
    """'half bottle of 37.5 cl', 'magnum of 1.5 l', or '' for a normal 75 cl bottle."""
    if not cl or abs(cl - 75) < 0.5:
        return ""
    names = {37.5: "half bottle", 150.0: "magnum", 300.0: "double magnum", 50.0: "bottle"}
    size = f"{cl / 100:g} l" if cl >= 100 else f"{cl:g} cl"
    return f"{names.get(round(cl, 1), 'bottle')} of {size}"
