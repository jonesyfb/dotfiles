"""
Three-tier memory: structured facts (sqlite) + conversation history (sqlite)
+ semantic vector search (sqlite-vec over nomic-embed-text embeddings).
"""
import json
import sqlite3
import struct
from contextlib import contextmanager
from pathlib import Path

from config import DB_PATH

_VEC_DIM = 768  # nomic-embed-text output dimension
_VEC_AVAILABLE = False


def _load_vec(c: sqlite3.Connection) -> bool:
    try:
        import sqlite_vec
        c.enable_load_extension(True)
        sqlite_vec.load(c)
        c.enable_load_extension(False)
        return True
    except Exception:
        return False


def _conn() -> sqlite3.Connection:
    global _VEC_AVAILABLE
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(DB_PATH))
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    _VEC_AVAILABLE = _load_vec(c)
    _init(c)
    return c


def _init(c: sqlite3.Connection) -> None:
    c.executescript("""
        CREATE TABLE IF NOT EXISTS facts (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            ts    INTEGER DEFAULT (unixepoch())
        );
        CREATE TABLE IF NOT EXISTS history (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            role    TEXT NOT NULL,
            content TEXT NOT NULL,
            ts      INTEGER DEFAULT (unixepoch())
        );
        CREATE TABLE IF NOT EXISTS tasks (
            id         TEXT PRIMARY KEY,
            label      TEXT NOT NULL,
            command    TEXT NOT NULL,
            status     TEXT NOT NULL DEFAULT 'pending',
            created_at INTEGER DEFAULT (unixepoch()),
            started_at INTEGER,
            done_at    INTEGER,
            result     TEXT
        );
        CREATE TABLE IF NOT EXISTS memory_items (
            id     INTEGER PRIMARY KEY AUTOINCREMENT,
            text   TEXT NOT NULL,
            source TEXT NOT NULL,
            ts     INTEGER DEFAULT (unixepoch())
        );
        CREATE TABLE IF NOT EXISTS activity_log (
            id     INTEGER PRIMARY KEY AUTOINCREMENT,
            app_id TEXT NOT NULL,
            title  TEXT NOT NULL,
            ts     INTEGER DEFAULT (unixepoch())
        );
        CREATE TABLE IF NOT EXISTS screenshots (
            id     INTEGER PRIMARY KEY AUTOINCREMENT,
            path   TEXT NOT NULL,
            app_id TEXT NOT NULL,
            ts     INTEGER DEFAULT (unixepoch())
        );
        CREATE TABLE IF NOT EXISTS gate_verdicts (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            target   TEXT NOT NULL,
            approved INTEGER NOT NULL,
            message  TEXT NOT NULL,
            ts       INTEGER DEFAULT (unixepoch())
        );
        CREATE TABLE IF NOT EXISTS ambient_events (
            id   INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            text TEXT NOT NULL,
            ts   INTEGER DEFAULT (unixepoch())
        );
        CREATE TABLE IF NOT EXISTS entity_identities (
            key        TEXT PRIMARY KEY,
            data       TEXT NOT NULL,
            ts         INTEGER DEFAULT (unixepoch())
        );
        CREATE TABLE IF NOT EXISTS ambient_snoozes (
            scope      TEXT PRIMARY KEY,
            expires_at REAL NOT NULL,
            origin     TEXT NOT NULL DEFAULT 'manual',
            reason     TEXT,
            created_at INTEGER DEFAULT (unixepoch())
        );
        CREATE TABLE IF NOT EXISTS ambient_baselines (
            kind   TEXT NOT NULL,
            metric TEXT NOT NULL,
            value  REAL NOT NULL,
            ts     INTEGER DEFAULT (unixepoch()),
            PRIMARY KEY (kind, metric)
        );
    """)
    if _VEC_AVAILABLE:
        try:
            c.execute(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_items "
                f"USING vec0(embedding float[{_VEC_DIM}])"
            )
        except Exception:
            pass
    c.commit()


def _pack(v: list[float]) -> bytes:
    return struct.pack(f"{len(v)}f", *v)


@contextmanager
def db():
    c = _conn()
    try:
        yield c
        c.commit()
    finally:
        c.close()


# ── Facts ─────────────────────────────────────────────────────────────────────

def set_fact(key: str, value: str) -> None:
    with db() as c:
        c.execute(
            "INSERT INTO facts(key, value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, ts=unixepoch()",
            (key, value),
        )


def get_fact(key: str) -> str | None:
    with db() as c:
        row = c.execute("SELECT value FROM facts WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None


def all_facts() -> dict[str, str]:
    with db() as c:
        rows = c.execute("SELECT key, value FROM facts ORDER BY key").fetchall()
        return {r["key"]: r["value"] for r in rows}


def delete_fact(key: str) -> None:
    with db() as c:
        c.execute("DELETE FROM facts WHERE key=?", (key,))


# ── Conversation history ───────────────────────────────────────────────────────

def add_turn(role: str, content: str | list) -> None:
    text = content if isinstance(content, str) else json.dumps(content)
    with db() as c:
        c.execute("INSERT INTO history(role, content) VALUES(?,?)", (role, text))


def get_history(limit: int = 40) -> list[dict]:
    with db() as c:
        rows = c.execute(
            "SELECT role, content FROM history ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    out = []
    for r in reversed(rows):
        try:
            content = json.loads(r["content"])
        except (json.JSONDecodeError, TypeError):
            content = r["content"]
        out.append({"role": r["role"], "content": content})
    return out


def clear_history() -> None:
    with db() as c:
        c.execute("DELETE FROM history")


def session_snapshot() -> list[dict]:
    return get_history(limit=20)


# ── Semantic (vector) memory ──────────────────────────────────────────────────

def store_vec(text: str, source: str, embedding: list[float]) -> None:
    """Store a text item and its embedding. Silently skips if vec unavailable."""
    if not _VEC_AVAILABLE:
        return
    with db() as c:
        cur = c.execute(
            "INSERT INTO memory_items(text, source) VALUES(?,?)", (text, source)
        )
        row_id = cur.lastrowid
        c.execute(
            "INSERT INTO vec_items(rowid, embedding) VALUES(?,?)",
            (row_id, _pack(embedding)),
        )


def semantic_search(embedding: list[float], limit: int = 5) -> list[dict]:
    """Return the closest memory_items by cosine-ish distance."""
    if not _VEC_AVAILABLE:
        return []
    with db() as c:
        rows = c.execute(
            """
            SELECT mi.text, mi.source, vi.distance
            FROM vec_items vi
            JOIN memory_items mi ON mi.id = vi.rowid
            WHERE vi.embedding MATCH ?
              AND k = ?
            ORDER BY vi.distance
            """,
            (_pack(embedding), limit),
        ).fetchall()
        return [{"text": r["text"], "source": r["source"], "distance": r["distance"]} for r in rows]


# ── Tasks ──────────────────────────────────────────────────────────────────────

def enqueue_task(task_id: str, label: str, command: str) -> None:
    with db() as c:
        c.execute(
            "INSERT OR IGNORE INTO tasks(id, label, command) VALUES(?,?,?)",
            (task_id, label, command),
        )


def get_pending_tasks() -> list[dict]:
    with db() as c:
        rows = c.execute(
            "SELECT id, label, command FROM tasks WHERE status='pending' ORDER BY created_at"
        ).fetchall()
        return [dict(r) for r in rows]


def update_task_status(task_id: str, status: str, result: str | None = None) -> None:
    with db() as c:
        if status == "running":
            c.execute(
                "UPDATE tasks SET status=?, started_at=unixepoch() WHERE id=?",
                (status, task_id),
            )
        else:
            c.execute(
                "UPDATE tasks SET status=?, done_at=unixepoch(), result=? WHERE id=?",
                (status, result, task_id),
            )


def get_all_tasks(limit: int = 10) -> list[dict]:
    with db() as c:
        rows = c.execute(
            "SELECT id, label, status, result FROM tasks ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


# ── Gatekeeper: activity log, screenshots, verdicts ────────────────────────────

def log_activity(app_id: str, title: str) -> None:
    with db() as c:
        c.execute("INSERT INTO activity_log(app_id, title) VALUES(?,?)", (app_id, title))


def prune_activity(older_than_seconds: int = 48 * 3600) -> None:
    with db() as c:
        c.execute(
            "DELETE FROM activity_log WHERE ts < unixepoch() - ?", (older_than_seconds,)
        )


def activity_since(seconds_ago: int) -> list[dict]:
    with db() as c:
        rows = c.execute(
            "SELECT app_id, title, ts FROM activity_log WHERE ts >= unixepoch() - ? ORDER BY ts",
            (seconds_ago,),
        ).fetchall()
        return [dict(r) for r in rows]


def save_screenshot(path: str, app_id: str) -> None:
    with db() as c:
        c.execute("INSERT INTO screenshots(path, app_id) VALUES(?,?)", (path, app_id))


def recent_screenshots(limit: int = 5) -> list[str]:
    with db() as c:
        rows = c.execute(
            "SELECT path FROM screenshots ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
        return [r["path"] for r in rows]


def save_verdict(target: str, approved: bool, message: str) -> None:
    with db() as c:
        c.execute(
            "INSERT INTO gate_verdicts(target, approved, message) VALUES(?,?,?)",
            (target, int(approved), message),
        )


def recent_verdicts(target: str, limit: int = 5) -> list[dict]:
    with db() as c:
        rows = c.execute(
            "SELECT approved, message, ts FROM gate_verdicts "
            "WHERE target=? ORDER BY ts DESC LIMIT ?",
            (target, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def last_verdict(target: str, within_seconds: int) -> dict | None:
    with db() as c:
        row = c.execute(
            "SELECT approved, message, ts FROM gate_verdicts "
            "WHERE target=? AND ts >= unixepoch() - ? ORDER BY ts DESC LIMIT 1",
            (target, within_seconds),
        ).fetchone()
        return dict(row) if row else None


# ── Ambient interruption policy: cooldown/budget/dedup state ────────────────────
# Only actually-spoken events are logged here (not denied attempts) — this
# table's meaning is "what Huginn has said ambiently," which is exactly what
# cooldown/budget/dedup need to reason about.

def log_ambient_event(kind: str, text: str) -> None:
    with db() as c:
        c.execute("INSERT INTO ambient_events(kind, text) VALUES(?,?)", (kind, text))


def last_ambient_event(kind: str) -> dict | None:
    with db() as c:
        row = c.execute(
            "SELECT text, ts FROM ambient_events WHERE kind=? ORDER BY ts DESC LIMIT 1",
            (kind,),
        ).fetchone()
        return dict(row) if row else None


def recent_ambient_events(kind: str, within_seconds: int) -> list[dict]:
    with db() as c:
        rows = c.execute(
            "SELECT text, ts FROM ambient_events "
            "WHERE kind=? AND ts >= unixepoch() - ? ORDER BY ts DESC",
            (kind, within_seconds),
        ).fetchall()
        return [dict(r) for r in rows]


# ── Entity lens: user-defined identity overrides ────────────────────────────────
# Only user overrides ever land here — builtins live in code (entities.py) and
# ephemeral literal-name inferences are never persisted at all.

def get_entity_identity(key: str) -> dict | None:
    with db() as c:
        row = c.execute("SELECT data FROM entity_identities WHERE key=?", (key,)).fetchone()
        return json.loads(row["data"]) if row else None


def set_entity_identity(key: str, data: dict) -> None:
    with db() as c:
        c.execute(
            "INSERT INTO entity_identities(key, data) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET data=excluded.data, ts=unixepoch()",
            (key, json.dumps(data)),
        )


def delete_entity_identity(key: str) -> None:
    with db() as c:
        c.execute("DELETE FROM entity_identities WHERE key=?", (key,))


def all_entity_identity_keys() -> list[str]:
    with db() as c:
        rows = c.execute("SELECT key FROM entity_identities").fetchall()
        return [r["key"] for r in rows]


# ── Ambient snooze/dismissal state ──────────────────────────────────────────────
# Explicit, user/manual-originated suppression — distinct from the automatic
# cooldown above. One row per scope ("global" or "kind:<event_kind>"). Persisted
# in the same sqlite file as everything else, so it survives a daemon restart
# for free, same as facts/tasks/gate_verdicts already do.

def set_snooze(scope: str, expires_at: float, origin: str = "manual", reason: str | None = None) -> None:
    with db() as c:
        c.execute(
            "INSERT INTO ambient_snoozes(scope, expires_at, origin, reason) VALUES(?,?,?,?) "
            "ON CONFLICT(scope) DO UPDATE SET expires_at=excluded.expires_at, "
            "origin=excluded.origin, reason=excluded.reason, created_at=unixepoch()",
            (scope, expires_at, origin, reason),
        )


def get_snooze(scope: str) -> dict | None:
    with db() as c:
        row = c.execute(
            "SELECT scope, expires_at, origin, reason FROM ambient_snoozes WHERE scope=?",
            (scope,),
        ).fetchone()
        return dict(row) if row else None


def clear_snooze(scope: str) -> None:
    with db() as c:
        c.execute("DELETE FROM ambient_snoozes WHERE scope=?", (scope,))


# ── Ambient worthiness: per-kind numeric baselines ──────────────────────────────

def get_baseline(kind: str, metric: str) -> float | None:
    with db() as c:
        row = c.execute(
            "SELECT value FROM ambient_baselines WHERE kind=? AND metric=?", (kind, metric)
        ).fetchone()
        return row["value"] if row else None


def set_baseline(kind: str, metric: str, value: float) -> None:
    with db() as c:
        c.execute(
            "INSERT INTO ambient_baselines(kind, metric, value) VALUES(?,?,?) "
            "ON CONFLICT(kind, metric) DO UPDATE SET value=excluded.value, ts=unixepoch()",
            (kind, metric, value),
        )
