"""Per-user agent registry for shared-bot mode.

One Telegram bot, many people. Each person gets their own persona, their own
optional custom instructions, and their own conversation memory.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Tenant:
    user_id: int
    display_name: str
    persona_key: str
    username: str | None = None
    custom_prompt: str = ""
    created_at: float = 0.0

    @property
    def memory_scope(self) -> str:
        """Memory namespace: this user's own history, per persona."""
        return f"u{self.user_id}:{self.persona_key}"


class TenantStore:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS tenants (
                user_id       INTEGER PRIMARY KEY,
                username      TEXT,
                display_name  TEXT NOT NULL,
                persona_key   TEXT NOT NULL,
                custom_prompt TEXT NOT NULL DEFAULT '',
                created_at    REAL NOT NULL
            )
            """
        )
        self._db.commit()

    def get(self, user_id: int) -> Tenant | None:
        with self._lock:
            row = self._db.execute(
                """
                SELECT user_id, username, display_name, persona_key,
                       custom_prompt, created_at
                  FROM tenants WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
        if row is None:
            return None
        return Tenant(
            user_id=row[0],
            username=row[1],
            display_name=row[2],
            persona_key=row[3],
            custom_prompt=row[4],
            created_at=row[5],
        )

    def upsert(
        self,
        user_id: int,
        display_name: str,
        persona_key: str,
        username: str | None = None,
        custom_prompt: str = "",
    ) -> Tenant:
        now = time.time()
        with self._lock:
            self._db.execute(
                """
                INSERT INTO tenants
                    (user_id, username, display_name, persona_key, custom_prompt, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username      = excluded.username,
                    display_name  = excluded.display_name,
                    persona_key   = excluded.persona_key,
                    custom_prompt = excluded.custom_prompt
                """,
                (user_id, username, display_name, persona_key, custom_prompt, now),
            )
            self._db.commit()
        return self.get(user_id)  # type: ignore[return-value]

    def set_persona(self, user_id: int, persona_key: str) -> bool:
        with self._lock:
            cur = self._db.execute(
                "UPDATE tenants SET persona_key = ? WHERE user_id = ?",
                (persona_key, user_id),
            )
            self._db.commit()
            return cur.rowcount > 0

    def set_custom_prompt(self, user_id: int, prompt: str) -> bool:
        with self._lock:
            cur = self._db.execute(
                "UPDATE tenants SET custom_prompt = ? WHERE user_id = ?",
                (prompt, user_id),
            )
            self._db.commit()
            return cur.rowcount > 0

    def delete(self, user_id: int) -> bool:
        with self._lock:
            cur = self._db.execute("DELETE FROM tenants WHERE user_id = ?", (user_id,))
            self._db.commit()
            return cur.rowcount > 0

    def all(self) -> list[Tenant]:
        with self._lock:
            rows = self._db.execute(
                """
                SELECT user_id, username, display_name, persona_key,
                       custom_prompt, created_at
                  FROM tenants ORDER BY created_at
                """
            ).fetchall()
        return [
            Tenant(
                user_id=r[0],
                username=r[1],
                display_name=r[2],
                persona_key=r[3],
                custom_prompt=r[4],
                created_at=r[5],
            )
            for r in rows
        ]

    def count(self) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM tenants").fetchone()[0]

    def close(self) -> None:
        with self._lock:
            self._db.close()
