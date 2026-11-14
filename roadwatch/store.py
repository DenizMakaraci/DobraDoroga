import functools
import hashlib
import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def identifier(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def now():
    return datetime.now(timezone.utc).isoformat()


def _backend(function):
    """Send the call to the MongoDB store when ROADWATCH_MONGODB_URI is set; SQLite is the default."""
    @functools.wraps(function)
    def call(*args, **kwargs):
        if not os.environ.get("ROADWATCH_MONGODB_URI"):
            return function(*args, **kwargs)
        try:
            from . import store_mongo
        except ImportError as error:
            raise RuntimeError("ROADWATCH_MONGODB_URI is set but pymongo is not installed. Run: pip install 'roadwatch[mongo]'") from error
        return getattr(store_mongo, function.__name__)(*args, **kwargs)
    return call


@contextmanager
def database():
    path = Path(os.environ.get("ROADWATCH_DB", Path(__file__).parent.parent / "data" / "roadwatch.sqlite3"))
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=20)
    try:
        connection.row_factory = sqlite3.Row
        with connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY, body TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS ai_usage (day TEXT PRIMARY KEY, runs INTEGER NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS members (id TEXT PRIMARY KEY, name TEXT NOT NULL, role TEXT NOT NULL, "
                "key_hash TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1)"
            )
            existing = {row[1] for row in connection.execute("PRAGMA table_info(members)")}
            for column, definition in (("email", "TEXT"), ("password_hash", "TEXT"), ("invite_hash", "TEXT"),
                                       ("invite_expires", "TEXT"), ("token_salt", "TEXT NOT NULL DEFAULT ''"),
                                       ("last_login", "TEXT"), ("verified", "INTEGER NOT NULL DEFAULT 1")):
                if column not in existing:
                    connection.execute(f"ALTER TABLE members ADD COLUMN {column} {definition}")
            connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS members_email ON members(email) WHERE email IS NOT NULL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS comments (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, body TEXT NOT NULL, "
                "created_at TEXT NOT NULL, deleted INTEGER NOT NULL DEFAULT 0)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS finding_status (project_id TEXT NOT NULL, finding_key TEXT NOT NULL, body TEXT NOT NULL, "
                "PRIMARY KEY (project_id, finding_key))"
            )
            yield connection
    finally:
        connection.close()


@_backend
def delete_project(project_id):
    with database() as connection:
        return connection.execute("DELETE FROM projects WHERE id = ?", (project_id,)).rowcount > 0


@_backend
def reserve_ai_run(daily_limit):
    """Atomically count an AI run against today's cap. Returns False when the cap is reached."""
    day = datetime.now(timezone.utc).date().isoformat()
    with database() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT runs FROM ai_usage WHERE day = ?", (day,)).fetchone()
        runs = row["runs"] if row else 0
        if runs >= daily_limit:
            return False
        connection.execute("INSERT OR REPLACE INTO ai_usage VALUES (?, ?)", (day, runs + 1))
    return True


@_backend
def ai_runs_today():
    day = datetime.now(timezone.utc).date().isoformat()
    with database() as connection:
        row = connection.execute("SELECT runs FROM ai_usage WHERE day = ?", (day,)).fetchone()
    return row["runs"] if row else 0


@_backend
def list_projects():
    with database() as connection:
        projects = [json.loads(row["body"]) for row in connection.execute("SELECT body FROM projects")]
    return sorted(projects, key=lambda project: project["created_at"], reverse=True)


@_backend
def project_summaries():
    """The fields the case list shows."""
    return [{k: p.get(k) for k in ("id", "name", "road", "contractor", "demo", "created_at", "anchor")} for p in list_projects()]


def _upgrade(project):
    """Cases created before the map feature lack these keys."""
    if project.get("location") and not project.get("locations"):
        project["locations"] = [project["location"]]
    project.pop("location", None)
    project.setdefault("locations", [])
    project.setdefault("open_data", [])
    project.setdefault("show_condition", True)
    project.setdefault("anchor", "street")
    project.setdefault("vendor", None)
    return project


@_backend
def search_evidence(query, limit=40):
    """Pages of evidence, in any case, that contain every word of `query`."""
    from . import search
    wanted, hits = search.words(query), []
    for project in list_projects() if wanted else []:
        for document in project["evidence"]:
            hits += search.evidence_hits(project["id"], project["name"], document, wanted, limit - len(hits))
            if len(hits) >= limit:
                return hits
    return hits


@_backend
def get_project(project_id):
    with database() as connection:
        row = connection.execute("SELECT body FROM projects WHERE id = ?", (project_id,)).fetchone()
    return _upgrade(json.loads(row["body"])) if row else None


@_backend
def create_project(fields, *, demo=False):
    project = {
        "locations": [], "anchor": "street", "vendor": None, **fields, "id": identifier("project"), "created_at": now(),
        "demo": demo, "revision": 0, "evidence": [], "finances": [], "observations": [], "reports": [],
        "open_data": [],
    }
    with database() as connection:
        connection.execute("INSERT INTO projects VALUES (?, ?)", (project["id"], json.dumps(project)))
    return project


@_backend
def mutate(project_id, callback):
    """Serialize edits so concurrent requests cannot overwrite evidence."""
    with database() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT body FROM projects WHERE id = ?", (project_id,)).fetchone()
        if not row:
            raise KeyError(project_id)
        project = _upgrade(json.loads(row["body"]))
        callback(project)
        connection.execute("UPDATE projects SET body = ? WHERE id = ?", (json.dumps(project), project_id))
    return project


@_backend
def append_item(project_id, collection, item):
    def update(project):
        project[collection].append(item)
        project["revision"] += 1
    return mutate(project_id, update)


# ---- team members ---------------------------------------------------------------------------------

PUBLIC_MEMBER = "id, name, role, email, created_at, active, last_login, verified, (password_hash IS NOT NULL) AS has_password, (invite_hash IS NOT NULL) AS invited"
SECRET_MEMBER = PUBLIC_MEMBER + ", password_hash, invite_hash, invite_expires, token_salt, key_hash"


def _row(row):
    return dict(row) if row else None


@_backend
def members(include_inactive=False):
    with database() as connection:
        rows = connection.execute(f"SELECT {PUBLIC_MEMBER} FROM members ORDER BY created_at").fetchall()
    return [dict(r) for r in rows if include_inactive or r["active"]]


@_backend
def add_member(name, role, key_hash, email=None, invite_hash=None, invite_expires=None, password_hash=None, verified=1):
    member = {"id": identifier("member"), "name": name, "role": role, "created_at": now(), "active": 1}
    with database() as connection:
        connection.execute("INSERT INTO members (id, name, role, key_hash, created_at, active, email, invite_hash, invite_expires, token_salt, "
                           "password_hash, verified) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, '', ?, ?)",
                           (member["id"], name, role, key_hash, member["created_at"], email, invite_hash, invite_expires, password_hash, verified))
    return public_member(member["id"])


@_backend
def member_by_key_hash(key_hash):
    with database() as connection:
        row = connection.execute(f"SELECT {SECRET_MEMBER} FROM members WHERE key_hash = ? AND active = 1", (key_hash,)).fetchone()
    return _row(row)


@_backend
def member_by_email(email):
    with database() as connection:
        row = connection.execute(f"SELECT {SECRET_MEMBER} FROM members WHERE lower(email) = lower(?)", (email,)).fetchone()
    return _row(row)


@_backend
def member_by_invite_hash(invite_hash):
    with database() as connection:
        row = connection.execute(f"SELECT {SECRET_MEMBER} FROM members WHERE invite_hash = ? AND active = 1", (invite_hash,)).fetchone()
    return _row(row)


@_backend
def member(member_id):
    """Full row including secrets - for auth only."""
    with database() as connection:
        row = connection.execute(f"SELECT {SECRET_MEMBER} FROM members WHERE id = ?", (member_id,)).fetchone()
    return _row(row)


@_backend
def public_member(member_id):
    with database() as connection:
        row = connection.execute(f"SELECT {PUBLIC_MEMBER} FROM members WHERE id = ?", (member_id,)).fetchone()
    return _row(row)


@_backend
def update_member(member_id, **fields):
    allowed = {k: v for k, v in fields.items()
               if k in ("name", "role", "active", "key_hash", "email", "password_hash", "invite_hash", "invite_expires", "token_salt", "last_login", "verified")}
    if allowed:
        with database() as connection:
            connection.execute(f"UPDATE members SET {', '.join(f'{k} = ?' for k in allowed)} WHERE id = ?", (*allowed.values(), member_id))
    return public_member(member_id)


# ---- discussion -------------------------------------------------------------------------------------

@_backend
def comments(project_id):
    """All non-deleted comments of a case, or of several discussion ids at once, oldest first; the JSON body holds
    author, anchor, citations and assessment fields."""
    ids = [project_id] if isinstance(project_id, str) else list(project_id)
    with database() as connection:
        rows = connection.execute(f"SELECT body FROM comments WHERE project_id IN ({', '.join('?' * len(ids))}) AND deleted = 0 "
                                  "ORDER BY created_at", ids).fetchall()
    return [json.loads(r["body"]) for r in rows]


@_backend
def add_comment(project_id, comment):
    comment = {**comment, "id": identifier("comment"), "project_id": project_id, "created_at": now(), "edited_at": None}
    with database() as connection:
        connection.execute("INSERT INTO comments VALUES (?, ?, ?, ?, 0)", (comment["id"], project_id, json.dumps(comment), comment["created_at"]))
    return comment


@_backend
def get_comment(comment_id):
    with database() as connection:
        row = connection.execute("SELECT body FROM comments WHERE id = ? AND deleted = 0", (comment_id,)).fetchone()
    return json.loads(row["body"]) if row else None


@_backend
def save_comment(comment):
    with database() as connection:
        connection.execute("UPDATE comments SET body = ? WHERE id = ?", (json.dumps(comment), comment["id"]))
    return comment


@_backend
def delete_comment(comment_id):
    with database() as connection:
        return connection.execute("UPDATE comments SET deleted = 1 WHERE id = ?", (comment_id,)).rowcount > 0


def discussion_id(project):
    """Where a case's discussion is kept. Street cases on the same set of streets in one city, in any order, share
    one discussion; a supplier case, or a case with no street yet, keeps its own."""
    from .opendata.common import street_key
    locations = project.get("locations") or []
    roads = sorted({" ".join(part for part in street_key(location["road"]) if part) for location in locations} - {""})
    if project.get("anchor") == "vendor" or not roads:
        return project["id"]
    return "roads-" + hashlib.sha256("|".join([locations[0].get("city_key") or "", *roads]).encode()).hexdigest()[:20]


def discussion(project):
    """The comments a case shows: the discussion shared by its set of streets, plus any posted on the case itself
    before discussions were shared."""
    shared = discussion_id(project)
    return comments(shared if shared == project["id"] else [shared, project["id"]])


@_backend
def finding_statuses(project_id):
    with database() as connection:
        rows = connection.execute("SELECT body FROM finding_status WHERE project_id = ?", (project_id,)).fetchall()
    return {json.loads(r["body"])["finding_key"]: json.loads(r["body"]) for r in rows}


@_backend
def set_finding_status(project_id, finding_key, status):
    status = {**status, "project_id": project_id, "finding_key": finding_key, "updated_at": now()}
    with database() as connection:
        connection.execute("INSERT OR REPLACE INTO finding_status VALUES (?, ?, ?)", (project_id, finding_key, json.dumps(status)))
    return status
