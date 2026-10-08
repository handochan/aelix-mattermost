"""Private durable session mappings, inbound idempotency records, placeholders and pairing."""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import tempfile
import time
from pathlib import Path

# PRAGMA user_version; 0 is the 0.1.0 schema, 1 added posts.placeholder (0.2.0),
# 2 added posts.session, answers, session_meta and the pairing tables (0.3.0).
SCHEMA_VERSION = 2


def private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        path.chmod(0o700)


def write_json(path: Path, data: dict) -> None:
    """Atomically replace `path` with JSON readable only by the owner (mkstemp: mode 0600)."""
    write_text(path, json.dumps(data, ensure_ascii=False))


def write_text(path: Path, text: str) -> None:
    """Atomically replace `path` with text readable only by the owner (mkstemp: mode 0600)."""
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


write_context = write_json  # the per-turn caller identity a domain tool reads


class Store:
    def __init__(self, directory: Path, dedup_days: int = 30, owner: bool = True) -> None:
        """`owner` is the gateway itself: it marks requests a previous process left as
        interrupted. CLI tools that share the database with a running gateway pass False."""
        private_directory(directory)
        self.path = directory / "gateway.db"
        self.dedup_days = dedup_days
        self.db = sqlite3.connect(self.path)
        try:
            self._open(owner)
        except BaseException:
            self.db.close()
            raise

    def _open(self, owner: bool) -> None:
        if os.name == "posix":
            self.path.chmod(0o600)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript(
            "CREATE TABLE IF NOT EXISTS sessions (key TEXT PRIMARY KEY, file TEXT NOT NULL);"
            "CREATE TABLE IF NOT EXISTS posts (id TEXT PRIMARY KEY, status TEXT NOT NULL, at REAL NOT NULL);"
        )
        self._migrate()
        if owner:
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
        if version < 2:
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(posts)")}
            if "session" not in columns:  # the conversation a request (or command) belonged to
                self.db.execute("ALTER TABLE posts ADD COLUMN session TEXT")
            self.db.executescript(
                # Posts the gateway made for a conversation (answers, notices); thread history
                # leaves them and the conversation's own requests out.
                "CREATE TABLE IF NOT EXISTS answers (post_id TEXT PRIMARY KEY, session TEXT NOT NULL,"
                " at REAL NOT NULL);"
                "CREATE TABLE IF NOT EXISTS pairing_blocks (user_id TEXT PRIMARY KEY, until REAL NOT NULL);"
                # A session's model override and the newest thread post it has seen (ms).
                "CREATE TABLE IF NOT EXISTS session_meta (key TEXT PRIMARY KEY, model TEXT,"
                " last_seen INTEGER);"
                "CREATE TABLE IF NOT EXISTS pairing_codes (code TEXT PRIMARY KEY,"
                " user_id TEXT NOT NULL UNIQUE, channel_id TEXT NOT NULL, created REAL NOT NULL,"
                " expires REAL NOT NULL);"
                "CREATE TABLE IF NOT EXISTS paired_users (user_id TEXT PRIMARY KEY,"
                " approved_at REAL NOT NULL, approved_by TEXT NOT NULL);"
            )
        self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self.db.commit()

    # -- inbound posts ------------------------------------------------------------

    def claim(self, post_id: str, session: str | None = None) -> bool:
        cursor = self.db.execute(
            "INSERT OR IGNORE INTO posts (id, status, at, session) VALUES (?, 'accepted', ?, ?)",
            (post_id, time.time(), session),
        )
        self.db.commit()
        return cursor.rowcount == 1

    def add_answer(self, post_id: str, session: str) -> None:
        self.db.execute("INSERT OR IGNORE INTO answers VALUES (?, ?, ?)", (post_id, session, time.time()))
        self.db.commit()

    def session_posts(self, session: str, post_ids: list[str]) -> set[str]:
        """Which of `post_ids` were the conversation's own requests or the gateway's posts for it."""
        found: set[str] = set()
        for start in range(0, len(post_ids), 500):
            chunk = post_ids[start:start + 500]
            marks = ",".join("?" * len(chunk))
            rows = self.db.execute(
                f"SELECT id FROM posts WHERE session=? AND id IN ({marks}) "
                f"UNION SELECT post_id FROM answers WHERE session=? AND post_id IN ({marks})",
                (session, *chunk, session, *chunk))
            found.update(row[0] for row in rows)
        return found

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
        """Forget post IDs older than dedup_days and expired pairing codes; returns how many
        post IDs were removed."""
        cutoff = time.time() - self.dedup_days * 86400
        cursor = self.db.execute("DELETE FROM posts WHERE at < ?", (cutoff,))
        self.db.execute("DELETE FROM answers WHERE at < ?", (cutoff,))
        self.db.execute("DELETE FROM pairing_codes WHERE expires < ?", (time.time(),))
        self.db.execute("DELETE FROM pairing_blocks WHERE until < ?", (time.time(),))
        self.db.commit()
        return cursor.rowcount

    # -- sessions -----------------------------------------------------------------

    def session_file(self, key: str) -> Path | None:
        row = self.db.execute("SELECT file FROM sessions WHERE key=?", (key,)).fetchone()
        return Path(row[0]) if row else None

    def save_session(self, key: str, path: Path) -> None:
        self.db.execute("INSERT OR REPLACE INTO sessions VALUES (?, ?)", (key, str(path)))
        self.db.commit()

    def reset_session(self, key: str) -> None:
        """Forget the transcript and the thread posts it saw; a model override stays."""
        self.db.execute("DELETE FROM sessions WHERE key=?", (key,))
        self.db.execute("UPDATE session_meta SET last_seen=NULL WHERE key=?", (key,))
        self.db.commit()

    def session_model(self, key: str) -> str | None:
        row = self.db.execute("SELECT model FROM session_meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_session_model(self, key: str, model: str | None) -> None:
        self.db.execute("INSERT INTO session_meta (key, model) VALUES (?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET model=excluded.model", (key, model))
        self.db.commit()

    def last_seen(self, key: str) -> int | None:
        row = self.db.execute("SELECT last_seen FROM session_meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_last_seen(self, key: str, millis: int) -> None:
        """Remember the newest thread post (create_at, ms) the session has seen; never goes back."""
        self.db.execute("INSERT INTO session_meta (key, last_seen) VALUES (?, ?) ON CONFLICT(key) DO UPDATE"
                        " SET last_seen=MAX(COALESCE(last_seen, 0), excluded.last_seen)", (key, millis))
        self.db.commit()

    # -- pairing ------------------------------------------------------------------

    def is_paired(self, user_id: str) -> bool:
        return self.db.execute("SELECT 1 FROM paired_users WHERE user_id=?", (user_id,)).fetchone() is not None

    def paired_users(self) -> list[tuple[str, float, str]]:
        rows = self.db.execute("SELECT user_id, approved_at, approved_by FROM paired_users ORDER BY approved_at")
        return [(row[0], row[1], row[2]) for row in rows]

    def pairing_for(self, user_id: str) -> tuple[str, float] | None:
        """(code, expires) of the user's unexpired pairing request."""
        row = self.db.execute("SELECT code, expires FROM pairing_codes WHERE user_id=? AND expires>=?",
                              (user_id, time.time())).fetchone()
        return (row[0], row[1]) if row else None

    def pending_pairings(self) -> list[tuple[str, str, str, float]]:
        """(code, user_id, channel_id, expires) of unexpired pairing requests, oldest first."""
        rows = self.db.execute("SELECT code, user_id, channel_id, expires FROM pairing_codes WHERE expires>=?"
                               " ORDER BY created", (time.time(),))
        return [(row[0], row[1], row[2], row[3]) for row in rows]

    def add_pairing(self, code: str, user_id: str, channel_id: str, ttl: float, limit: int) -> bool:
        """Store a new pairing request; False when `limit` requests are already pending."""
        now = time.time()
        self.db.execute("DELETE FROM pairing_codes WHERE expires<? OR user_id=?", (now, user_id))
        pending = self.db.execute("SELECT COUNT(*) FROM pairing_codes").fetchone()[0]
        if pending >= limit:
            self.db.commit()
            return False
        self.db.execute("INSERT INTO pairing_codes VALUES (?, ?, ?, ?, ?)", (code, user_id, channel_id, now, now + ttl))
        self.db.commit()
        return True

    def take_pairing(self, code: str) -> tuple[str, str] | None:
        """Remove an unexpired pairing request; returns its (user_id, channel_id)."""
        row = self.db.execute("SELECT user_id, channel_id FROM pairing_codes WHERE code=? AND expires>=?",
                              (code, time.time())).fetchone()
        self.db.execute("DELETE FROM pairing_codes WHERE code=?", (code,))
        self.db.commit()
        return (row[0], row[1]) if row else None

    def block_pairing(self, user_id: str, seconds: float) -> None:
        """A denied user gets no new code (and admins no new notice) for a while."""
        self.db.execute("INSERT OR REPLACE INTO pairing_blocks VALUES (?, ?)", (user_id, time.time() + seconds))
        self.db.commit()

    def pairing_blocked(self, user_id: str) -> bool:
        row = self.db.execute("SELECT 1 FROM pairing_blocks WHERE user_id=? AND until>=?",
                              (user_id, time.time())).fetchone()
        return row is not None

    def pair(self, user_id: str, approved_by: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO paired_users VALUES (?, ?, ?)", (user_id, time.time(), approved_by))
        self.db.execute("DELETE FROM pairing_codes WHERE user_id=?", (user_id,))
        self.db.execute("DELETE FROM pairing_blocks WHERE user_id=?", (user_id,))
        self.db.commit()

    def unpair(self, user_id: str) -> bool:
        cursor = self.db.execute("DELETE FROM paired_users WHERE user_id=?", (user_id,))
        self.db.commit()
        return cursor.rowcount == 1

    def close(self) -> None:
        self.db.close()
