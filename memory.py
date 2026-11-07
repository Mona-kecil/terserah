"""SQLite-backed conversation memory, scoped per (persona, chat)."""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path


class Memory:
    def __init__(self, db_path: Path, max_history: int = 20) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.max_history = max_history
        self._lock = threading.Lock()
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                persona  TEXT    NOT NULL,
                chat_id  INTEGER NOT NULL,
                role     TEXT    NOT NULL,
                content  TEXT    NOT NULL,
                ts       REAL    NOT NULL
            )
            """
        )
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_scope ON messages(persona, chat_id, id)"
        )
        self._db.commit()

    def history(self, persona: str, chat_id: int) -> list[dict[str, str]]:
        with self._lock:
            rows = self._db.execute(
                """
                SELECT role, content FROM messages
                 WHERE persona = ? AND chat_id = ?
                 ORDER BY id DESC LIMIT ?
                """,
                (persona, chat_id, self.max_history),
            ).fetchall()
        return [{"role": r, "content": c} for r, c in reversed(rows)]

    def add(self, persona: str, chat_id: int, role: str, content: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO messages (persona, chat_id, role, content, ts) VALUES (?, ?, ?, ?, ?)",
                (persona, chat_id, role, content, time.time()),
            )
            self._db.commit()

    def clear(self, persona: str, chat_id: int) -> int:
        with self._lock:
            cur = self._db.execute(
                "DELETE FROM messages WHERE persona = ? AND chat_id = ?",
                (persona, chat_id),
            )
            self._db.commit()
            return cur.rowcount

    def close(self) -> None:
        with self._lock:
            self._db.close()
