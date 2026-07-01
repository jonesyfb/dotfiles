"""
Two-tier memory: structured facts (sqlite) + conversation history (sqlite).
sqlite-vec semantic search planned for a future iteration.
"""
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from config import DB_PATH


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(DB_PATH))
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
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
    """)
    c.commit()


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
