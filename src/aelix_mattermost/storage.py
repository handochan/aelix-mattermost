"""Private durable session mappings and inbound idempotency records."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path


def private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        path.chmod(0o700)


def write_context(path: Path, data: dict) -> None:
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        if os.name == "posix":
            os.fchmod(handle.fileno(), 0o600)
        json.dump(data, handle, ensure_ascii=False)
    temporary.replace(path)


class Store:
    def __init__(self, directory: Path, dedup_days: int = 30) -> None:
        private_directory(directory)
        self.path = directory / "gateway.db"
        self.db = sqlite3.connect(self.path)
        if os.name == "posix":
            self.path.chmod(0o600)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript(
            "CREATE TABLE IF NOT EXISTS sessions (key TEXT PRIMARY KEY, file TEXT NOT NULL);"
            "CREATE TABLE IF NOT EXISTS posts (id TEXT PRIMARY KEY, status TEXT NOT NULL, at REAL NOT NULL);"
        )
        self.db.execute("UPDATE posts SET status='interrupted' WHERE status='accepted'")
        self.db.execute("DELETE FROM posts WHERE at < ?", (time.time() - dedup_days * 86400,))
        self.db.commit()

    def claim(self, post_id: str) -> bool:
        cursor = self.db.execute(
            "INSERT OR IGNORE INTO posts VALUES (?, 'accepted', ?)", (post_id, time.time())
        )
        self.db.commit()
        return cursor.rowcount == 1

    def finish(self, post_id: str, status: str = "done") -> None:
        self.db.execute("UPDATE posts SET status=? WHERE id=?", (status, post_id))
        self.db.commit()

    def session_file(self, key: str) -> Path | None:
        row = self.db.execute("SELECT file FROM sessions WHERE key=?", (key,)).fetchone()
        return Path(row[0]) if row else None

    def save_session(self, key: str, path: Path) -> None:
        self.db.execute("INSERT OR REPLACE INTO sessions VALUES (?, ?)", (key, str(path)))
        self.db.commit()

    def reset_session(self, key: str) -> None:
        self.db.execute("DELETE FROM sessions WHERE key=?", (key,))
        self.db.commit()

    def close(self) -> None:
        self.db.close()
