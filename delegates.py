"""Per-person delegate profiles.

Each person owns one bot. Their briefing lives here, keyed by that bot, and is
only ever injected into that one agent's prompt and that one agent's reviewer.
No other agent receives it - the roundtable passes public messages, never
configuration.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from briefing import Briefing


@dataclass
class DelegateConfig:
    """Static wiring for one person's bot, from YAML."""

    key: str
    name: str
    token: str


@dataclass
class DelegateProfile:
    """What the person told their own agent, in private."""

    key: str
    display_name: str
    preferences: str = ""
    updated_at: float = 0.0
    briefing: Briefing | None = None

    @property
    def configured(self) -> bool:
        if self.briefing is not None and not self.briefing.is_empty():
            return True
        return bool(self.preferences.strip())

    def prompt_block(self) -> str:
        """The text both the agent and its reviewer work from."""
        if self.briefing is not None and not self.briefing.is_empty():
            block = self.briefing.to_prompt()
            if self.preferences.strip():
                block += (
                    "\n\nEXTRA NOTES FROM THEM (soft):\n"
                    f"- {self.preferences.strip()}"
                )
            return block

        legacy = self.preferences.strip()
        return (
            "HARD CONSTRAINTS - never violate, never soften:\n"
            "- (none declared)\n\n"
            "Rules about this list:\n"
            "- You have no hard constraints. Do not invent one.\n"
            "- Nothing below is a hard constraint; treat it as a soft note.\n"
            + (f"\nSOFT NOTE:\n- {legacy}" if legacy else "")
        )


class DelegateStore:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS delegate_profiles (
                key           TEXT PRIMARY KEY,
                display_name  TEXT NOT NULL,
                preferences   TEXT NOT NULL DEFAULT '',
                updated_at    REAL NOT NULL,
                briefing_json TEXT NOT NULL DEFAULT ''
            )
            """
        )
        try:
            self._db.execute(
                "ALTER TABLE delegate_profiles "
                "ADD COLUMN briefing_json TEXT NOT NULL DEFAULT ''"
            )
        except sqlite3.OperationalError:
            pass  # already present
        self._db.commit()

    def get(self, key: str, default_name: str = "") -> DelegateProfile:
        with self._lock:
            row = self._db.execute(
                "SELECT key, display_name, preferences, updated_at, briefing_json "
                "FROM delegate_profiles WHERE key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return DelegateProfile(key=key, display_name=default_name or key.title())
        return DelegateProfile(
            key=row[0],
            display_name=row[1],
            preferences=row[2],
            updated_at=row[3],
            briefing=Briefing.from_json(row[4]),
        )

    def set_briefing(
        self, key: str, display_name: str, briefing: Briefing
    ) -> DelegateProfile:
        with self._lock:
            self._db.execute(
                """
                INSERT INTO delegate_profiles
                    (key, display_name, preferences, updated_at, briefing_json)
                VALUES (?, ?, '', ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    display_name  = excluded.display_name,
                    preferences   = '',
                    updated_at    = excluded.updated_at,
                    briefing_json = excluded.briefing_json
                """,
                (key, display_name, time.time(), briefing.to_json()),
            )
            self._db.commit()
        return self.get(key, display_name)

    def set_preferences(
        self, key: str, display_name: str, preferences: str
    ) -> DelegateProfile:
        """Legacy free-text path, kept for compatibility."""
        with self._lock:
            self._db.execute(
                """
                INSERT INTO delegate_profiles (key, display_name, preferences, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    display_name = excluded.display_name,
                    preferences  = excluded.preferences,
                    updated_at   = excluded.updated_at
                """,
                (key, display_name, preferences.strip(), time.time()),
            )
            self._db.commit()
        return self.get(key, display_name)

    def clear(self, key: str) -> bool:
        with self._lock:
            cur = self._db.execute(
                "UPDATE delegate_profiles SET preferences = '', briefing_json = '', "
                "updated_at = ? WHERE key = ?",
                (time.time(), key),
            )
            self._db.commit()
            return cur.rowcount > 0

    def all(self) -> list[DelegateProfile]:
        with self._lock:
            rows = self._db.execute(
                "SELECT key, display_name, preferences, updated_at, briefing_json "
                "FROM delegate_profiles ORDER BY key"
            ).fetchall()
        return [
            DelegateProfile(
                key=r[0],
                display_name=r[1],
                preferences=r[2],
                updated_at=r[3],
                briefing=Briefing.from_json(r[4]),
            )
            for r in rows
        ]

    def close(self) -> None:
        with self._lock:
            self._db.close()
