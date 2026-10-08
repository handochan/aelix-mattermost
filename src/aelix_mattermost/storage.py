"""Private durable session mappings, inbound idempotency records and placeholders."""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import tempfile
import time
from pathlib import Path

SCHEMA_VERSION = 1  # PRAGMA user_version; 0 is the 0.1.0 schema


def private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        path.chmod(0o700)


def write_json(path: Path, data: dict) -> None:
    """Atomically replace `path` with JSON readable only by the owner (mkstemp: mode 0600)."""
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False)
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


write_context = write_json  # the per-turn caller identity a domain tool reads


class Store:
    def __init__(self, directory: Path, dedup_days: int = 30) -> None:
        private_directory(directory)
        self.path = directory / "gateway.db"
        self.dedup_days = dedup_days
        self.db = sqlite3.connect(self.path)
        try:
            self._open()
        except BaseException:
            self.db.close()
            raise

    def _open(self) -> None:
        if os.name == "posix":
            self.path.chmod(0o600)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript(
            "CREATE TABLE IF NOT EXISTS sessions (key TEXT PRIMARY KEY, file TEXT NOT NULL);"
            "CREATE TABLE IF NOT EXISTS posts (id TEXT PRIMARY KEY, status TEXT NOT NULL, at REAL NOT NULL);"
        )
        self._migrate()
        self.db.execute("UPDATE posts SET status='interrupted' WHERE status='accepted'")
        self.db.commit()
        self.prune()

    def _migrate(self) -> None:
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError("gateway.db was written by a newer aelix-mattermost")
        if version < 1:  # posts.placeholder: the "preparing" post of an unfinished request
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(posts)")}
            if "placeholder" not in columns:
                self.db.execute("ALTER TABLE posts ADD COLUMN placeholder TEXT")
        self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self.db.commit()

    def claim(self, post_id: str) -> bool:
        cursor = self.db.execute(
            "INSERT OR IGNORE INTO posts (id, status, at) VALUES (?, 'accepted', ?)", (post_id, time.time())
        )
        self.db.commit()
        return cursor.rowcount == 1

    def finish(self, post_id: str, status: str = "done") -> None:
        self.db.execute("UPDATE posts SET status=? WHERE id=?", (status, post_id))
        self.db.commit()

    def status(self, post_id: str) -> str | None:
        row = self.db.execute("SELECT status FROM posts WHERE id=?", (post_id,)).fetchone()
        return row[0] if row else None

    def set_placeholder(self, post_id: str, placeholder_id: str | None) -> None:
        self.db.execute("UPDATE posts SET placeholder=? WHERE id=?", (placeholder_id, post_id))
        self.db.commit()

    def placeholders(self) -> list[tuple[str, str]]:
        """(post id, placeholder id) of requests whose placeholder was never retired."""
        rows = self.db.execute("SELECT id, placeholder FROM posts WHERE placeholder IS NOT NULL ORDER BY at")
        return [(row[0], row[1]) for row in rows]

    def prune(self) -> int:
        """Forget post IDs older than dedup_days; returns how many were removed."""
        cursor = self.db.execute("DELETE FROM posts WHERE at < ?", (time.time() - self.dedup_days * 86400,))
        self.db.commit()
        return cursor.rowcount

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
