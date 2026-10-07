"""Strict TOML configuration; credentials stay in environment variables."""

from __future__ import annotations

import os
import ssl
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


class ConfigError(ValueError):
    pass


def _strings(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(x, str) or not x for x in value):
        raise ConfigError(f"{name} must be an array of non-empty strings")
    return tuple(value)


@dataclass(frozen=True)
class Config:
    url: str
    token: str = field(repr=False)
    token_env: str = "MATTERMOST_TOKEN"
    allowed_users: tuple[str, ...] = ()
    allowed_channels: tuple[str, ...] = ()
    allow_all_users: bool = False
    require_mention: bool = True
    session_scope: str = "user"
    allow_insecure_http: bool = False
    ca_file: Path | None = None
    state_dir: Path = Path("var")
    work_dir: Path = Path("workspace")
    command: tuple[str, ...] = ("aelix",)
    model: str | None = None
    offline: bool = True
    allowed_tools: tuple[str, ...] = ()
    extensions: tuple[str, ...] = ()
    max_concurrent_runs: int = 3
    max_sessions: int = 64
    max_queue_per_session: int = 4
    idle_timeout: float = 300
    run_timeout: float = 180
    rpc_timeout: float = 20
    max_tool_calls: int = 30
    max_input_chars: int = 20000
    max_output_chars: int = 60000
    max_post_chars: int = 3500
    dedup_days: int = 30

    def validate(self) -> Config:
        u = urlsplit(self.url)
        if u.scheme not in {"http", "https"} or not u.hostname or u.username or u.password:
            raise ConfigError("mattermost.url must be an HTTP(S) server URL without credentials")
        if u.query or u.fragment:
            raise ConfigError("mattermost.url cannot contain a query or fragment")
        if u.scheme == "http" and not self.allow_insecure_http:
            raise ConfigError("HTTP requires allow_insecure_http = true; use HTTPS in production")
        if not self.token or any(x in self.token for x in "\r\n"):
            raise ConfigError(f"Set a valid bot token in {self.token_env}")
        if not self.allowed_users and not self.allow_all_users:
            raise ConfigError("Set allowed_users, or explicitly opt into allow_all_users = true")
        if self.session_scope not in {"user", "thread"}:
            raise ConfigError("session_scope must be 'user' or 'thread'")
        if not self.command or any(not x for x in self.command):
            raise ConfigError("aelix.command cannot be empty")
        for name in (
            "max_concurrent_runs", "max_sessions", "max_queue_per_session", "idle_timeout",
            "run_timeout", "rpc_timeout", "max_tool_calls", "max_input_chars", "max_output_chars",
            "max_post_chars", "dedup_days",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ConfigError(f"{name} must be positive")
        for name in (
            "max_concurrent_runs", "max_sessions", "max_queue_per_session", "max_tool_calls",
            "max_input_chars", "max_output_chars", "max_post_chars", "dedup_days",
        ):
            if not isinstance(getattr(self, name), int):
                raise ConfigError(f"{name} must be an integer")
        if not 100 <= self.max_post_chars <= 3500:
            raise ConfigError("max_post_chars must be between 100 and 3500")
        if self.ca_file is not None and not self.ca_file.is_file():
            raise ConfigError("ca_file does not exist")
        return self

    def ssl_context(self) -> ssl.SSLContext:
        return ssl.create_default_context(cafile=str(self.ca_file) if self.ca_file else None)


def load_config(path: Path) -> Config:
    path = path.expanduser().resolve()
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"Cannot read TOML configuration: {path}") from exc
    if set(raw) - {"mattermost", "aelix", "gateway"}:
        raise ConfigError("Unknown top-level configuration section")
    sections = {name: raw.get(name, {}) for name in ("mattermost", "aelix", "gateway")}
    for name, value in sections.items():
        if not isinstance(value, dict):
            raise ConfigError(f"{name} must be a table")
    valid = {
        "mattermost": {"url", "token_env", "allowed_users", "allowed_channels", "allow_all_users",
                       "require_mention", "allow_insecure_http", "ca_file", "max_post_chars"},
        "aelix": {"command", "model", "offline", "allowed_tools", "extensions", "work_dir"},
        "gateway": {"state_dir", "session_scope", "max_concurrent_runs", "max_sessions",
                    "max_queue_per_session", "idle_timeout", "run_timeout", "rpc_timeout",
                    "max_tool_calls", "max_input_chars", "max_output_chars", "dedup_days"},
    }
    values: dict[str, object] = {}
    for name, data in sections.items():
        unknown = set(data) - valid[name]
        if unknown:
            raise ConfigError(f"Unknown {name} setting: {', '.join(sorted(unknown))}")
        values.update(data)
    for key in ("allowed_users", "allowed_channels", "command", "allowed_tools", "extensions"):
        if key in values:
            values[key] = _strings(values[key], key)
    for key in ("allow_all_users", "require_mention", "allow_insecure_http", "offline"):
        if key in values and not isinstance(values[key], bool):
            raise ConfigError(f"{key} must be a boolean")
    for key in ("url", "token_env", "model", "session_scope"):
        if key in values and not isinstance(values[key], str):
            raise ConfigError(f"{key} must be a string")
    for key in ("state_dir", "work_dir", "ca_file"):
        value = values.get(key, {"state_dir": "var", "work_dir": "workspace"}.get(key))
        if value is not None:
            if not isinstance(value, str):
                raise ConfigError(f"{key} must be a path string")
            p = Path(value).expanduser()
            values[key] = (p if p.is_absolute() else path.parent / p).resolve()
    if "extensions" in values:
        values["extensions"] = tuple(
            str((Path(x) if Path(x).is_absolute() else path.parent / x).resolve())
            for x in values["extensions"]
        )
    values["token"] = os.environ.get(str(values.get("token_env", "MATTERMOST_TOKEN")), "")
    if "url" not in values:
        raise ConfigError("mattermost.url is required")
    return Config(**values).validate()  # type: ignore[arg-type]
