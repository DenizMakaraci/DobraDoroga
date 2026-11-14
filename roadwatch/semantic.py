"""Search evidence by meaning with MongoDB Atlas Vector Search.

Needs the MongoDB store and an embedding model (ROADWATCH_EMBED_MODEL on any OpenAI-compatible /embeddings
endpoint). Each evidence page is cut into pieces; every piece is embedded once and stored in the `chunks`
collection, and a query is embedded the same way so Atlas can return the nearest pieces. An evidence part never
changes after it is written (see store_mongo), so it is embedded once and its chunks go when the part goes.
Where Atlas Vector Search is missing (a self-hosted server) or its index is still building, the nearest pieces
are found by comparing the query with every stored vector instead.
"""

import hashlib
import logging
import os
import threading
import time

from openai import OpenAI, RateLimitError
from pymongo import ASCENDING
from datetime import datetime, timezone

from pymongo.errors import BulkWriteError, OperationFailure
from pymongo.operations import SearchIndexModel

from . import store_mongo

PIECE_CHARACTERS = 1500
DEFAULT_BASE_URL = "https://api.openai.com/v1"

VECTOR_DAYS = 90
QUOTA_PAUSE = 1800

_state = {"thread": None, "error": None, "started": 0.0, "paused_until": 0.0}
_lock = threading.Lock()
_queryable = set()


def settings():
    """The embedding model in use, or None when search by meaning is not configured."""
    model = os.environ.get("ROADWATCH_EMBED_MODEL", "").strip()
    api_key = os.environ.get("ROADWATCH_EMBED_API_KEY") or os.environ.get("ROADWATCH_API_KEY", "")
    if not (model and api_key and os.environ.get("ROADWATCH_MONGODB_URI")):
        return None
    try:
        dimensions = int(os.environ.get("ROADWATCH_EMBED_DIMENSIONS") or 768)
    except ValueError:
        dimensions = 768
    return {"model": model, "api_key": api_key, "dimensions": dimensions, "key": f"{model}:{dimensions}",
            "index": f"chunk_vectors_{dimensions}",
            "base_url": os.environ.get("ROADWATCH_EMBED_BASE_URL") or os.environ.get("ROADWATCH_BASE_URL") or DEFAULT_BASE_URL}


def available():
    return settings() is not None


def pieces(text, size=PIECE_CHARACTERS):
    """(offset, piece) cuts of a page, broken at line ends where possible, each at most `size` characters."""
    found, start, length = [], 0, 0
    for line in text.splitlines(keepends=True):
        while len(line) > size:
            if length:
                found.append((start, text[start:start + length]))
                start, length = start + length, 0
            found.append((start, line[:size]))
            start, line = start + size, line[size:]
        if length + len(line) > size:
            found.append((start, text[start:start + length]))
            start, length = start + length, 0
        length += len(line)
    if length:
        found.append((start, text[start:start + length]))
    return [(offset, piece) for offset, piece in found if piece.strip()]


def embed(texts):
    """One vector per text. Waits and retries when the provider's rate limit is hit."""
    config, vectors = settings(), []
    with OpenAI(api_key=config["api_key"], base_url=config["base_url"], timeout=60, max_retries=0) as client:
        for start in range(0, len(texts), 16):
            for attempt in range(3):
                try:
                    response = client.embeddings.create(model=config["model"], input=texts[start:start + 16],
                                                        dimensions=config["dimensions"])
                    break
                except RateLimitError:
                    if attempt == 2:
                        raise
                    time.sleep(20 * (attempt + 1))
            if len(response.data) != len(texts[start:start + 16]):
                raise ValueError("the embedding model returned a different number of vectors than texts")
            vectors += [item.embedding for item in response.data]
    return vectors


def _vectors(db, config, texts):
    """One vector per text, asking the embedding model only for text it has not embedded before. Refreshing a
    case's open data rewrites its documents, but most pages come back word for word the same; their vectors are
    kept in `vectors` by a hash of model and text for VECTOR_DAYS, so a refresh costs only the pages that changed."""
    keys = [hashlib.sha256(f"{config['key']}\n{text}".encode()).hexdigest() for text in texts]
    known = {row["_id"]: row["embedding"] for row in db.vectors.find({"_id": {"$in": keys}})}
    missing = {key: text for key, text in zip(keys, texts) if key not in known}
    if missing:
        fresh = embed(list(missing.values()))
        known.update(zip(missing, fresh))
        try:
            db.vectors.insert_many([{"_id": key, "embedding": vector, "at": datetime.now(timezone.utc)} for key, vector in zip(missing, fresh)],
                                   ordered=False)
        except BulkWriteError:
            pass  # another worker stored the same text first
    return [known[key] for key in keys]


def _cases(db):
    """(case names by id, owning case id by evidence part id) for the parts cases currently use."""
    names, owner = {}, {}
    for row in db.projects.find({}, {"body.name": 1, "parts.evidence": 1}):
        names[row["_id"]] = row["body"].get("name")
        owner.update({part_id: row["_id"] for part_id in row["parts"]["evidence"]})
    return names, owner


def pending(db=None):
    """Evidence parts in use that have not been embedded with the current model."""
    config, db = settings(), db if db is not None else store_mongo._db()
    owner = _cases(db)[1]
    done = {row["_id"] for row in db.parts.find({"kind": "evidence", "embedded": config["key"]}, {"_id": 1})}
    return {part_id: project_id for part_id, project_id in owner.items() if part_id not in done}


def _ensure_indexes(db, config):
    db.chunks.create_index([("part_id", ASCENDING)])
    db.chunks.create_index([("project_id", ASCENDING)])
    db.vectors.create_index([("at", ASCENDING)], expireAfterSeconds=VECTOR_DAYS * 86400)
    try:
        if not any(index["name"] == config["index"] for index in db.chunks.list_search_indexes()):
            db.chunks.create_search_index(SearchIndexModel(name=config["index"], type="vectorSearch", definition={"fields": [
                {"type": "vector", "path": "embedding", "numDimensions": config["dimensions"], "similarity": "cosine"},
                {"type": "filter", "path": "model"}]}))
    except OperationFailure:
        pass


def index_pending():
    """Embed every evidence part that is not indexed yet. Returns how many parts were embedded."""
    config, db = settings(), store_mongo._db()
    _ensure_indexes(db, config)
    waiting = pending(db)
    for part_id, project_id in waiting.items():
        document = store_mongo._bodies(db, [part_id]).get(part_id)
        if document is None:
            continue
        title = document.get("title") or "Untitled document"
        rows = [{"_id": f"{part_id}:{page.get('number')}:{offset}", "part_id": part_id, "project_id": project_id,
                 "evidence_id": document.get("id"), "title": title, "page": page.get("number"), "text": piece, "model": config["key"]}
                for page in document.get("pages") or [] for offset, piece in pieces(page.get("text") or "")]
        vectors = _vectors(db, config, [f"{title}\n{row['text']}" for row in rows]) if rows else []
        db.chunks.delete_many({"part_id": part_id})
        if rows:
            db.chunks.insert_many([{**row, "embedding": vector} for row, vector in zip(rows, vectors)])
        if not db.parts.update_one({"_id": part_id}, {"$set": {"embedded": config["key"]}}).matched_count:
            db.chunks.delete_many({"part_id": part_id})
    return len(waiting)


def _run():
    try:
        index_pending()
        _state["error"] = None
    except Exception as error:
        _state["error"] = type(error).__name__
        if isinstance(error, RateLimitError):  # the provider's quota is spent: asking again every minute only wastes it
            _state["paused_until"] = time.time() + QUOTA_PAUSE
        logging.getLogger("roadwatch").warning("embedding evidence failed: %s", type(error).__name__)


def start():
    """Embed evidence that is not indexed yet in the background; one run at a time, at most twice a minute."""
    if not available():
        return
    with _lock:
        if (_state["thread"] and _state["thread"].is_alive()) or time.time() - _state["started"] < 30 or time.time() < _state["paused_until"]:
            return
        _state["started"] = time.time()
        _state["thread"] = threading.Thread(target=_run, daemon=True)
        _state["thread"].start()


def status():
    return {"pending": len(pending()), "running": bool(_state["thread"] and _state["thread"].is_alive()), "error": _state["error"]}


def _index_ready(db, config):
    marker = (db.name, config["index"])
    if marker not in _queryable:
        try:
            if any(index["name"] == config["index"] and index.get("queryable") for index in db.chunks.list_search_indexes()):
                _queryable.add(marker)
        except OperationFailure:
            pass
    return marker in _queryable


def _nearest(db, config, vector, count):
    if _index_ready(db, config):
        return list(db.chunks.aggregate([
            {"$vectorSearch": {"index": config["index"], "path": "embedding", "queryVector": vector, "numCandidates": max(200, count * 10),
                               "limit": count, "filter": {"model": config["key"]}}},
            {"$set": {"score": {"$meta": "vectorSearchScore"}}}, {"$unset": "embedding"}]))
    length = sum(value * value for value in vector) ** 0.5 or 1.0
    rows = []
    for row in db.chunks.find({"model": config["key"]}):
        other = row.pop("embedding")
        cosine = sum(a * b for a, b in zip(vector, other)) / (length * (sum(b * b for b in other) ** 0.5 or 1.0))
        rows.append({**row, "score": (1 + cosine) / 2})
    return sorted(rows, key=lambda row: row["score"], reverse=True)[:count]


def search(query, limit=15):
    """The evidence pages closest in meaning to `query`, best first: one hit per page, in the shape of
    store.search_evidence plus a `score` between 0 and 1."""
    config, db = settings(), store_mongo._db()
    names, owner = _cases(db)
    hits, seen = [], set()
    for row in _nearest(db, config, embed([query])[0], limit * 4):
        page = (row["part_id"], row["page"])
        if row["part_id"] not in owner or page in seen:
            continue
        seen.add(page)
        text = " ".join(row["text"].split())
        hits.append({"project_id": owner[row["part_id"]], "project_name": names[owner[row["part_id"]]], "evidence_id": row["evidence_id"],
                     "title": row["title"], "page": row["page"], "snippet": text[:260] + (" …" if len(text) > 260 else ""),
                     "score": round(row["score"], 3)})
        if len(hits) >= limit:
            break
    return hits
