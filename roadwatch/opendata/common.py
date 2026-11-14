import hashlib
import json
import os
import re
import time
from pathlib import Path

import httpx

USER_AGENT = "Mozilla/5.0 (compatible; Roadwatch/0.3; public records research)"
CACHE_TTL = 24 * 3600

STREET_TYPES = {
    "road": "rd", "rd": "rd", "street": "st", "st": "st", "avenue": "ave", "ave": "ave", "av": "ave",
    "drive": "dr", "dr": "dr", "boulevard": "blvd", "blvd": "blvd", "crescent": "cres", "cres": "cres",
    "court": "crt", "crt": "crt", "ct": "crt", "lane": "lane", "ln": "lane", "way": "way", "trail": "trl",
    "trl": "trl", "parkway": "pkwy", "pkwy": "pkwy", "place": "pl", "pl": "pl", "circle": "cir", "cir": "cir",
    "terrace": "ter", "ter": "ter", "gardens": "gdns", "gdns": "gdns", "square": "sq", "sq": "sq",
    "highway": "hwy", "hwy": "hwy", "expressway": "expy", "expy": "expy",
}
DIRECTIONS = {"east", "west", "north", "south", "e", "w", "n", "s", "est", "ouest", "nord", "sud", "o"}
FRENCH_TYPES = {"rue": "rue", "avenue": "ave", "av": "ave", "boulevard": "blvd", "boul": "blvd", "chemin": "ch",
                "ch": "ch", "montee": "montee", "rang": "rang", "place": "pl", "cote": "cote", "allee": "allee",
                "promenade": "prom", "autoroute": "hwy", "route": "route", "croissant": "cres", "terrasse": "ter",
                "impasse": "impasse", "carre": "sq", "square": "sq"}
TYPE_PATTERN = "|".join(sorted(STREET_TYPES, key=len, reverse=True))
ACCENTS = str.maketrans("àâäéèêëîïôöùûüçÀÂÄÉÈÊËÎÏÔÖÙÛÜÇ", "aaaeeeeiioouuucAAAEEEEIIOOUUUC")


def fold(text):
    """Lower-case, accent-free, hyphens as spaces: 'Rue Saint-Denis' -> 'rue saint denis'."""
    return str(text or "").translate(ACCENTS).casefold().replace("-", " ").replace("’", "'")


def street_key(name):
    """'Davenport Road' / 'DAVENPORT RD' / 'Davenport Rd W' -> ('davenport', 'rd');
    'Rue Saint-Denis' / 'SAINT-DENIS' -> ('saint denis', 'rue'); 'Boul. Saint-Laurent O' -> ('saint laurent', 'blvd')."""
    words = re.findall(r"[a-z0-9']+", fold(name))
    while len(words) > 1 and words[-1] in DIRECTIONS:
        words.pop()
    kind = None
    if len(words) > 1 and words[0] in FRENCH_TYPES:
        kind = FRENCH_TYPES[words.pop(0)]
    elif len(words) > 1 and words[-1] in STREET_TYPES:
        kind = STREET_TYPES[words.pop()]
    return " ".join(words), kind


def mentions_street(text, street):
    """True when `text` names the street: case/accent-insensitive, word-bounded, type abbreviations equal.
    French types precede the name ('rue Saint-Denis'); English types follow ('Davenport Rd')."""
    base, kind = street_key(street)
    if not base or not text:
        return False
    folded = fold(text)
    french = "|".join(sorted(FRENCH_TYPES, key=len, reverse=True))
    pattern = (r"(?:\b(" + french + r")\.?\s+(?:de\s+|du\s+|des\s+|d'|de\s+la\s+|de\s+l')?)?\b" + re.escape(base)
               + r"(?:\s+(" + TYPE_PATTERN + r"))?\b")
    for match in re.finditer(pattern, folded):
        found = FRENCH_TYPES.get(match.group(1)) if match.group(1) else (STREET_TYPES.get(match.group(2)) if match.group(2) else None)
        if kind is None or found is None or found == kind or {kind, found} <= {"rue", "st"}:
            return True
    return False




COMPANY_SUFFIXES = {"LIMITED": "LTD", "LIMITEE": "LTEE", "INCORPORATED": "INC", "INCORPOREE": "INC", "CORPORATION": "CORP",
                    "COMPANY": "CO", "COMPAGNIE": "CIE", "ENR": "ENR"}


def canonical_company(name):
    """Upper-case, accent- and punctuation-free company name with suffixes normalised, so 'Acme Ltd.',
    'ACME LTD', 'Acme Limited' and '9368-6616 Québec inc.' / '9368 6616 QUEBEC INC' each count as one company."""
    words = re.sub(r"\s+", " ", re.sub(r"[.,'\"()]", "", fold(name))).strip().upper().split(" ")
    return " ".join(COMPANY_SUFFIXES.get(w, w) for w in words if w)


LEGAL_SUFFIXES = {"LTD", "LTEE", "INC", "CORP", "CO", "CIE", "ENR", "LLP", "LLC", "LP", "ULC", "SENC", "SENCRL", "PLC", "GP"}


def company_base(name):
    """Canonical company name without its trailing legal suffixes: 'GC Strategies Inc.' -> 'GC STRATEGIES'."""
    words = canonical_company(name).split(" ")
    while len(words) > 1 and words[-1] in LEGAL_SUFFIXES:
        words.pop()
    return " ".join(words)


def company_matcher(names, min_prefix=6):
    """A predicate over raw company names for one supplier and its aliases. Matches the canonical form exactly, or
    the suffix-free base as a word prefix ('GC STRATEGIES' matches 'GC STRATEGIES INC' and 'GC STRATEGIES CONSULTING'),
    so legal-form variants are found; short bases are matched exactly to avoid catching unrelated firms."""
    wanted = {canonical_company(n) for n in names if n and str(n).strip()}
    bases = {company_base(n) for n in names if n and len(company_base(n)) >= min_prefix}

    def matches(raw):
        candidate = canonical_company(raw)
        return bool(candidate) and (candidate in wanted or any(candidate == b or candidate.startswith(b + " ") for b in bases))
    return matches


def matching_street(text, streets):
    """The first of `streets` named in `text`, or None."""
    return next((s for s in streets if mentions_street(text, s)), None)


def cache_dir():
    base = os.environ.get("ROADWATCH_CACHE", Path(os.environ.get(
        "ROADWATCH_DB", Path(__file__).parent.parent.parent / "data" / "roadwatch.sqlite3")).parent / "cache")
    path = Path(base)
    path.mkdir(parents=True, exist_ok=True)
    return path


def fetch_cached(name, url, *, params=None, ttl=CACHE_TTL, max_bytes=60_000_000):
    """Download `url` to the cache once per `ttl` seconds and return the bytes."""
    path = cache_dir() / name
    if path.exists() and time.time() - path.stat().st_mtime < ttl:
        return path.read_bytes()
    with httpx.Client(timeout=120, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as client:
        response = client.get(url, params=params)
    response.raise_for_status()
    if len(response.content) > max_bytes:
        raise ValueError(f"{name} is larger than {max_bytes // 1_000_000} MB")
    path.write_bytes(response.content)
    return response.content


def post_json_cached(name, url, payload, *, ttl=CACHE_TTL):
    """POST `payload` as JSON (e.g. a datastore SQL query), cached like fetch_cached."""
    key = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]
    path = cache_dir() / f"{name}-{key}.json"
    if path.exists() and time.time() - path.stat().st_mtime < ttl:
        return json.loads(path.read_text())
    with httpx.Client(timeout=120, headers={"User-Agent": USER_AGENT}) as client:
        response = client.post(url, json=payload)
    response.raise_for_status()
    data = response.json()
    path.write_text(json.dumps(data))
    return data


def render_records(records, fields):
    """Fixed 'Label: value' lines per record so AI and checks can quote them verbatim."""
    blocks = []
    for index, record in enumerate(records, 1):
        lines = [f"Record {index}"]
        for label, key in fields:
            value = record.get(key)
            if value not in (None, "", "None"):
                lines.append(f"{label}: {str(value).strip()}")
        blocks.append("\n".join(lines))
    return blocks


def paginate(blocks, per_page=25):
    pages = [blocks[i:i + per_page] for i in range(0, len(blocks), per_page)] or [[]]
    return [{"number": n, "text": "\n\n".join(chunk)} for n, chunk in enumerate(pages, 1)]


def dataset_result(*, key, title, source_url, description, records, fields, since=None):
    blocks = render_records(records, fields)
    note = f"{len(records)} record(s)." + (f" Filtered to {since} onward." if since else "")
    header = f"{title}\nSource: {source_url}\n{description}\n{note}"
    pages = paginate(blocks)
    pages[0]["text"] = header + ("\n\n" + pages[0]["text"] if pages[0]["text"] else "")
    return {"key": key, "title": title, "source_url": source_url, "description": description,
            "records": records, "pages": pages}


def load_json(raw):
    return json.loads(raw.decode("utf-8-sig"))


TEXT_FIELDS = ("description", "location", "extent", "intersection", "title", "particulars", "detail", "road")


def flag_limits(records, limits):
    """Set record['within_limits'] from the text fields: True if a from/to cross street of the record's street is
    named, False if the street has limits but none is named, None if no limits were given. Text heuristic only."""
    for r in records:
        bounds = limits.get(r.get("street") or "")
        if not bounds:
            r["within_limits"] = None
            continue
        text = " ".join(str(r.get(f) or "") for f in TEXT_FIELDS)
        r["within_limits"] = any(mentions_street(text, b) for b in bounds if b)
    return records
