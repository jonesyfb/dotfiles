"""
Huginn persistent memory — SQLite-backed.
Two stores:
  - messages: rolling conversation history (last N messages on load)
  - memories: permanent key/value facts Huginn saves explicitly
"""
import json
import sqlite3
from pathlib import Path

from config import Config

DB_PATH = Config.data_dir / "huginn.db"
HISTORY_LOAD_LIMIT = 20


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    Config.data_dir.mkdir(parents=True, exist_ok=True)
    with _connect() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS messages (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                role       TEXT NOT NULL,
                content    TEXT NOT NULL DEFAULT '',
                tool_calls TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS memories (
                key        TEXT PRIMARY KEY,
                value      TEXT NOT NULL,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
        """)


def save_message(role: str, content: str, tool_calls: list | None = None) -> None:
    tc = json.dumps(tool_calls) if tool_calls else None
    with _connect() as conn:
        conn.execute(
            "INSERT INTO messages (role, content, tool_calls) VALUES (?, ?, ?)",
            (role, content, tc),
        )


def load_recent_history() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT role, content, tool_calls FROM messages ORDER BY id DESC LIMIT ?",
            (HISTORY_LOAD_LIMIT,),
        ).fetchall()
    result = []
    for r in reversed(rows):
        msg: dict = {"role": r["role"], "content": r["content"]}
        if r["tool_calls"]:
            msg["tool_calls"] = json.loads(r["tool_calls"])
        result.append(msg)
    return result


def save_memory(key: str, value: str) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO memories (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP",
            (key, value),
        )


def load_memories() -> dict[str, str]:
    with _connect() as conn:
        rows = conn.execute("SELECT key, value FROM memories ORDER BY key").fetchall()
    return {r["key"]: r["value"] for r in rows}


def forget_memory(key: str) -> bool:
    with _connect() as conn:
        cur = conn.execute("DELETE FROM memories WHERE key = ?", (key,))
        return cur.rowcount > 0


# ── Conversation summarization ────────────────────────────────────────────────

SUMMARIZE_THRESHOLD = 30   # messages before compression kicks in
SUMMARIZE_KEEP      = 10   # most-recent messages to preserve verbatim


def should_summarize(history: list) -> bool:
    return len(history) > SUMMARIZE_THRESHOLD


def build_summary_prompt(messages: list) -> str:
    lines = []
    for m in messages:
        role = m["role"].upper()
        content = m.get("content", "")
        if content:
            lines.append(f"{role}: {content}")
    block = "\n".join(lines)
    return (
        "Summarize the following conversation in 3–5 sentences. "
        "Capture key facts, decisions, and context the assistant needs to remember. "
        "Be dense and factual — this will replace the raw history.\n\n"
        + block
    )


def compress_history(history: list, summary: str) -> list:
    """Replace old messages with a summary + keep the most recent ones."""
    recent = history[-SUMMARIZE_KEEP:]
    summary_entry = {
        "role":    "user",
        "content": f"[Summary of earlier conversation]: {summary}",
    }
    ack_entry = {
        "role":    "assistant",
        "content": "Understood, I have context from our earlier conversation.",
    }
    return [summary_entry, ack_entry] + recent
