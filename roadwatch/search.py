"""Search the evidence of every case. Both stores use this matcher, so a hit means the same thing on SQLite and
MongoDB: every word of the query appears as a whole word on the page (or in the document title), ignoring case
and accents."""

import re
import unicodedata


def fold(text):
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)).casefold()


def words(query):
    return list(dict.fromkeys(w for w in re.findall(r"\w+", fold(query)) if len(w) >= 2))[:8]


def _snippet(text, position):
    start = max(0, position - 90)
    excerpt = " ".join(text[start:position + 170].split())
    return ("… " if start else "") + excerpt + (" …" if position + 170 < len(text) else "")


def evidence_hits(project_id, project_name, document, wanted, limit):
    """Hits for one evidence document: one per matching page, at most `limit`."""
    patterns = [re.compile(rf"(?<!\w){re.escape(word)}(?!\w)") for word in wanted]
    title = fold(document.get("title") or "")
    in_title = [bool(p.search(title)) for p in patterns]
    hits = []
    for page in document.get("pages") or []:
        text = page.get("text") or ""
        folded = fold(text)
        found = [p.search(folded) for p in patterns]
        if not any(found) or not all(match or titled for match, titled in zip(found, in_title)):
            continue
        hits.append({"project_id": project_id, "project_name": project_name, "evidence_id": document.get("id"),
                     "title": document.get("title") or "Untitled document", "page": page.get("number"),
                     "snippet": _snippet(text, min(m.start() for m in found if m))})
        if len(hits) >= limit:
            break
    return hits
