"""Persistent record of every roundtable session.

The game master sees everything by design. This is where "everything" lives:
who said what, in which round, and how it ended.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Session:
    id: int
    chat_id: int
    topic: str
    status: str
    verdict: str
    started_at: float
    ended_at: float

    @property
    def duration(self) -> float:
        if not self.ended_at:
            return max(0.0, time.time() - self.started_at)
        return max(0.0, self.ended_at - self.started_at)

    @property
    def running(self) -> bool:
        return self.status == "running"


@dataclass
class TurnRecord:
    round_no: int
    speaker: str
    text: str
    ts: float
    status: str = "posted"

    @property
    def blocked(self) -> bool:
        return self.status.startswith("blocked")


class SessionStore:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id    INTEGER NOT NULL,
                topic      TEXT    NOT NULL,
                status     TEXT    NOT NULL,
                verdict    TEXT    NOT NULL DEFAULT '',
                started_at REAL    NOT NULL,
                ended_at   REAL    NOT NULL DEFAULT 0
            )
            """
        )
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS session_turns (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                round_no   INTEGER NOT NULL,
                speaker    TEXT    NOT NULL,
                text       TEXT    NOT NULL,
                ts         REAL    NOT NULL,
                status     TEXT    NOT NULL DEFAULT 'posted'
            )
            """
        )
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_turns ON session_turns(session_id, id)"
        )
        try:
            self._db.execute(
                "ALTER TABLE session_turns ADD COLUMN status TEXT NOT NULL DEFAULT 'posted'"
            )
        except sqlite3.OperationalError:
            pass  # column already present
        self._db.commit()

    def start(self, chat_id: int, topic: str) -> int:
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO sessions (chat_id, topic, status, started_at) "
                "VALUES (?, ?, 'running', ?)",
                (chat_id, topic, time.time()),
            )
            self._db.commit()
            return int(cur.lastrowid)

    def add_turn(
        self,
        session_id: int,
        round_no: int,
        speaker: str,
        text: str,
        status: str = "posted",
    ) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO session_turns "
                "(session_id, round_no, speaker, text, ts, status) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, round_no, speaker, text, time.time(), status),
            )
            self._db.commit()

    def finish(self, session_id: int, status: str, verdict: str = "") -> None:
        with self._lock:
            self._db.execute(
                "UPDATE sessions SET status = ?, verdict = ?, ended_at = ? "
                "WHERE id = ?",
                (status, verdict, time.time(), session_id),
            )
            self._db.commit()

    def get(self, session_id: int) -> Session | None:
        with self._lock:
            row = self._db.execute(
                "SELECT id, chat_id, topic, status, verdict, started_at, ended_at "
                "FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        return _row_to_session(row) if row else None

    def latest(self, limit: int = 1) -> list[Session]:
        with self._lock:
            rows = self._db.execute(
                "SELECT id, chat_id, topic, status, verdict, started_at, ended_at "
                "FROM sessions ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_row_to_session(r) for r in rows]

    def turns(self, session_id: int) -> list[TurnRecord]:
        with self._lock:
            rows = self._db.execute(
                "SELECT round_no, speaker, text, ts, status FROM session_turns "
                "WHERE session_id = ? ORDER BY id",
                (session_id,),
            ).fetchall()
        return [
            TurnRecord(
                round_no=r[0], speaker=r[1], text=r[2], ts=r[3], status=r[4]
            )
            for r in rows
        ]

    def recent_verdicts(self, chat_id: int, limit: int = 3) -> list[str]:
        """What this group decided last time, most recent first."""
        with self._lock:
            rows = self._db.execute(
                "SELECT verdict FROM sessions "
                "WHERE chat_id = ? AND status = 'decided' AND verdict != '' "
                "ORDER BY id DESC LIMIT ?",
                (chat_id, limit),
            ).fetchall()
        return [r[0] for r in rows]

    def mark_stale(self) -> int:
        """A killed process leaves sessions stuck in 'running' forever."""
        with self._lock:
            cur = self._db.execute(
                "UPDATE sessions SET status = 'interrupted', ended_at = ? "
                "WHERE status = 'running'",
                (time.time(),),
            )
            self._db.commit()
            return cur.rowcount

    def count(self) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]

    def close(self) -> None:
        with self._lock:
            self._db.close()


def _row_to_session(row) -> Session:
    return Session(
        id=row[0],
        chat_id=row[1],
        topic=row[2],
        status=row[3],
        verdict=row[4],
        started_at=row[5],
        ended_at=row[6],
    )
