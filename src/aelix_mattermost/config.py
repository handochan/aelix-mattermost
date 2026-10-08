"""Strict TOML configuration; the bot token comes from an environment variable or a file."""

from __future__ import annotations

import os
import re
import ssl
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


class ConfigError(ValueError):
    pass


MIB = 1024 * 1024
_ID = re.compile(r"[a-z0-9]{26}")  # a Mattermost channel or user id
_TRIGGER = re.compile(r"[a-z0-9_\-]{1,128}")


def _strings(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(x, str) or not x for x in value):
        raise ConfigError(f"{name} must be an array of non-empty strings")
    return tuple(value)


def _read_secret(path: Path, name: str) -> str:
    """A Docker/Kubernetes secret file: UTF-8, surrounding whitespace removed."""
    try:
        token = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"Cannot read {name}: {path}") from exc
    if not token:
        raise ConfigError(f"{name} is empty: {path}")
    return token


def _read_token(path: Path) -> str:
    return _read_secret(path, "mattermost.token_file")


@dataclass(frozen=True)
class ChannelSettings:
    """A [channels."<channel id>"] table; None means "as configured globally"."""

    prompt: str = ""
    require_mention: bool | None = None
    allowed_tools: tuple[str, ...] | None = None


@dataclass(frozen=True)
class Config:
    url: str
    token: str = field(repr=False)
    token_env: str = "MATTERMOST_TOKEN"
    token_file: Path | None = None
    allowed_users: tuple[str, ...] = ()
    allowed_channels: tuple[str, ...] = ()
    allow_all_users: bool = False
    require_mention: bool = True
    pairing: bool = False
    admins: tuple[str, ...] = ()
    session_scope: str = "user"
    allow_insecure_http: bool = False
    ca_file: Path | None = None
    state_dir: Path = Path("var")
    work_dir: Path = Path("workspace")
    command: tuple[str, ...] = ("aelix",)
    model: str | None = None
    models: tuple[str, ...] = ()
    offline: bool = True
    allowed_tools: tuple[str, ...] = ()
    extensions: tuple[str, ...] = ()
    mcp_config: Path | None = None
    system_prompt: str = ""
    max_concurrent_runs: int = 3
    max_live_processes: int = 8
    max_sessions: int = 64
    max_queue_per_session: int = 4
    idle_timeout: float = 300
    run_timeout: float = 180
    rpc_timeout: float = 20
    startup_timeout: float = 60
    max_tool_calls: int = 30
    max_input_chars: int = 20000
    max_output_chars: int = 60000
    max_post_chars: int = 3500
    dedup_days: int = 30
    log_aelix_stderr: bool = False
    busy_mode: str = "steer"
    progress: str = "status"
    progress_interval: float = 3.0
    thread_history_posts: int = 30
    thread_history_chars: int = 12000
    max_attachments: int = 10
    max_attachment_bytes: int = 20 * MIB
    max_inline_text_chars: int = 30000
    max_upload_bytes: int = 50 * MIB
    channels: dict[str, ChannelSettings] = field(default_factory=dict)
    slash_listen: str | None = None
    slash_token: str = field(default="", repr=False)
    slash_token_env: str = "MATTERMOST_SLASH_TOKEN"
    slash_token_file: Path | None = None
    slash_trigger: str = "aelix"

    def validate(self) -> Config:
        u = urlsplit(self.url)
        if u.scheme not in {"http", "https"} or not u.hostname or u.username or u.password:
            raise ConfigError("mattermost.url must be an HTTP(S) server URL without credentials")
        if u.query or u.fragment:
            raise ConfigError("mattermost.url cannot contain a query or fragment")
        if u.scheme == "http" and not self.allow_insecure_http:
            raise ConfigError("HTTP requires allow_insecure_http = true; use HTTPS in production")
        if not self.token or any(x in self.token for x in "\r\n"):
            source = f"mattermost.token_file ({self.token_file})" if self.token_file else self.token_env
            raise ConfigError(f"Set a valid bot token in {source}")
        if not self.allowed_users and not self.allow_all_users and not self.admins and not self.pairing:
            raise ConfigError("Set allowed_users, enable pairing, or explicitly opt into allow_all_users = true")
        if self.session_scope not in {"user", "thread"}:
            raise ConfigError("session_scope must be 'user' or 'thread'")
        if self.busy_mode not in {"steer", "interrupt", "queue"}:
            raise ConfigError("busy_mode must be 'steer', 'interrupt' or 'queue'")
        if self.progress not in {"off", "status", "stream"}:
            raise ConfigError("progress must be 'off', 'status' or 'stream'")
        if not self.command or any(not x for x in self.command):
            raise ConfigError("aelix.command cannot be empty")
        for name in (
            "max_concurrent_runs", "max_live_processes", "max_sessions", "max_queue_per_session",
            "idle_timeout", "run_timeout", "rpc_timeout", "startup_timeout", "max_tool_calls",
            "max_input_chars", "max_output_chars", "max_post_chars", "dedup_days", "progress_interval",
            "thread_history_chars", "max_attachment_bytes", "max_inline_text_chars", "max_upload_bytes",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ConfigError(f"{name} must be positive")
        for name in ("thread_history_posts", "max_attachments"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ConfigError(f"{name} must be a non-negative integer")
        for name in (
            "max_concurrent_runs", "max_live_processes", "max_sessions", "max_queue_per_session",
            "max_tool_calls", "max_input_chars", "max_output_chars", "max_post_chars", "dedup_days",
            "thread_history_chars", "max_attachment_bytes", "max_inline_text_chars", "max_upload_bytes",
        ):
            if not isinstance(getattr(self, name), int):
                raise ConfigError(f"{name} must be an integer")
        if self.max_live_processes < self.max_concurrent_runs:
            raise ConfigError("max_live_processes must be at least max_concurrent_runs")
        if not 100 <= self.max_post_chars <= 3500:
            raise ConfigError("max_post_chars must be between 100 and 3500")
        if self.progress_interval < 1:
            raise ConfigError("progress_interval must be at least 1 second")
        if self.max_attachments > 10:
            raise ConfigError("max_attachments cannot exceed 10 (Mattermost's files per post)")
        if self.ca_file is not None and not self.ca_file.is_file():
            raise ConfigError("ca_file does not exist")
        if self.mcp_config is not None and not self.mcp_config.is_file():
            raise ConfigError("aelix.mcp_config does not exist")
        for model in (*self.models, *([self.model] if self.model else [])):
            if not model.strip() or any(c.isspace() for c in model):
                raise ConfigError(f"Invalid model name: {model!r}")
        for name in self.channels:
            if not _ID.fullmatch(name):
                raise ConfigError(f"channels.{name}: a channel id is 26 lowercase letters and digits")
        if self.slash_listen is not None:
            listen_address(self.slash_listen)
            if not self.slash_token or any(x in self.slash_token for x in "\r\n"):
                source = (f"slash_command.token_file ({self.slash_token_file})" if self.slash_token_file
                          else self.slash_token_env)
                raise ConfigError(f"slash_command.listen requires the command token in {source}")
            if not _TRIGGER.fullmatch(self.slash_trigger):
                raise ConfigError("slash_command.trigger must be 1-128 lowercase letters, digits, '-' or '_'")
        return self

    def ssl_context(self) -> ssl.SSLContext:
        return ssl.create_default_context(cafile=str(self.ca_file) if self.ca_file else None)

    def channel(self, channel_id: str) -> ChannelSettings:
        return self.channels.get(channel_id) or ChannelSettings()

    def mention_required(self, channel_id: str) -> bool:
        own = self.channel(channel_id).require_mention
        return self.require_mention if own is None else own

    def tools_for(self, channel_id: str) -> tuple[str, ...]:
        own = self.channel(channel_id).allowed_tools
        return self.allowed_tools if own is None else own

    def is_admin(self, user_id: str) -> bool:
        return user_id in self.admins


def listen_address(value: str) -> tuple[str, int]:
    """"host:port" or "[v6]:port" of slash_command.listen."""
    host, sep, port = value.rpartition(":")
    host = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    if not sep or not host or not port.isdigit() or not 0 < int(port) < 65536:
        raise ConfigError("slash_command.listen must be host:port, e.g. 127.0.0.1:8066")
    return host, int(port)


_SECTIONS = {
    "mattermost": {"url", "token_env", "token_file", "allowed_users", "allowed_channels",
                   "allow_all_users", "require_mention", "pairing", "admins", "allow_insecure_http",
                   "ca_file", "max_post_chars"},
    "aelix": {"command", "model", "models", "offline", "allowed_tools", "extensions", "work_dir",
              "mcp_config", "system_prompt"},
    "gateway": {"state_dir", "session_scope", "max_concurrent_runs", "max_live_processes",
                "max_sessions", "max_queue_per_session", "idle_timeout", "run_timeout",
                "rpc_timeout", "startup_timeout", "max_tool_calls", "max_input_chars",
                "max_output_chars", "dedup_days", "log_aelix_stderr", "busy_mode", "progress",
                "progress_interval", "thread_history_posts", "thread_history_chars",
                "max_attachments", "max_attachment_bytes", "max_inline_text_chars", "max_upload_bytes"},
    "slash_command": {"listen", "token_env", "token_file", "trigger"},
}
_RENAMED = {"slash_command": {"listen": "slash_listen", "token_env": "slash_token_env",
                              "token_file": "slash_token_file", "trigger": "slash_trigger"}}


_MODULE = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*(?::[A-Za-z_]\w*)?")


def _extension(value: str, base: Path) -> str:
    """An aelix.extensions entry: a path (a .py file or a directory, relative to the config
    file) or the module of an installed extension package ("pkg.module" or "pkg:setup")."""
    path = Path(value).expanduser()
    path = path if path.is_absolute() else base / path
    if path.exists() or not _MODULE.fullmatch(value) or value.endswith(".py"):
        return str(path.resolve())
    return value


def _channels(raw: object) -> dict[str, ChannelSettings]:
    if not isinstance(raw, dict):
        raise ConfigError("channels must be a table of [channels.\"<channel id>\"] tables")
    result = {}
    for name, value in raw.items():
        if not isinstance(value, dict):
            raise ConfigError(f"channels.{name} must be a table")
        unknown = set(value) - {"prompt", "require_mention", "allowed_tools"}
        if unknown:
            raise ConfigError(f"Unknown channels.{name} setting: {', '.join(sorted(unknown))}")
        prompt = value.get("prompt", "")
        if not isinstance(prompt, str):
            raise ConfigError(f"channels.{name}.prompt must be a string")
        mention = value.get("require_mention")
        if mention is not None and not isinstance(mention, bool):
            raise ConfigError(f"channels.{name}.require_mention must be a boolean")
        tools = value.get("allowed_tools")
        if tools is not None:
            if not isinstance(tools, list) or any(not isinstance(x, str) or not x for x in tools):
                raise ConfigError(f"channels.{name}.allowed_tools must be an array of non-empty strings")
            tools = tuple(tools)
        result[name] = ChannelSettings(prompt, mention, tools)
    return result


def load_config(path: Path) -> Config:
    path = path.expanduser().resolve()
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"Cannot read TOML configuration: {path}") from exc
    if set(raw) - {*_SECTIONS, "channels"}:
        raise ConfigError("Unknown top-level configuration section")
    sections = {name: raw.get(name, {}) for name in _SECTIONS}
    for name, value in sections.items():
        if not isinstance(value, dict):
            raise ConfigError(f"{name} must be a table")
    values: dict[str, object] = {}
    for name, data in sections.items():
        unknown = set(data) - _SECTIONS[name]
        if unknown:
            raise ConfigError(f"Unknown {name} setting: {', '.join(sorted(unknown))}")
        renamed = _RENAMED.get(name, {})
        values.update({renamed.get(key, key): value for key, value in data.items()})
    values["channels"] = _channels(raw.get("channels", {}))
    for key in ("allowed_users", "allowed_channels", "admins", "command", "models", "allowed_tools",
                "extensions"):
        if key in values:
            values[key] = _strings(values[key], key)
    for key in ("allow_all_users", "require_mention", "pairing", "allow_insecure_http", "offline",
                "log_aelix_stderr"):
        if key in values and not isinstance(values[key], bool):
            raise ConfigError(f"{key} must be a boolean")
    for key in ("url", "token_env", "model", "session_scope", "system_prompt", "busy_mode", "progress",
                "slash_listen", "slash_token_env", "slash_trigger"):
        if key in values and not isinstance(values[key], str):
            raise ConfigError(f"{key} must be a string")
    # Relative paths are relative to the configuration file, not the working directory.
    for key in ("state_dir", "work_dir", "ca_file", "token_file", "mcp_config", "slash_token_file"):
        value = values.get(key, {"state_dir": "var", "work_dir": "workspace"}.get(key))
        if value is not None:
            if not isinstance(value, str) or not value:
                raise ConfigError(f"{key} must be a path string")
            p = Path(value).expanduser()
            values[key] = (p if p.is_absolute() else path.parent / p).resolve()
    if "extensions" in values:
        values["extensions"] = tuple(_extension(x, path.parent) for x in values["extensions"])
    token_file = values.get("token_file")
    if isinstance(token_file, Path):  # takes precedence over token_env
        values["token"] = _read_token(token_file)
    else:
        values["token"] = os.environ.get(str(values.get("token_env", "MATTERMOST_TOKEN")), "")
    if values.get("slash_listen") is not None:
        slash_file = values.get("slash_token_file")
        if isinstance(slash_file, Path):
            values["slash_token"] = _read_secret(slash_file, "slash_command.token_file")
        else:
            values["slash_token"] = os.environ.get(
                str(values.get("slash_token_env", "MATTERMOST_SLASH_TOKEN")), "")
    if "url" not in values:
        raise ConfigError("mattermost.url is required")
    return Config(**values).validate()  # type: ignore[arg-type]
