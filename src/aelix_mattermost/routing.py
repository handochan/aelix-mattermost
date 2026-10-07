"""Pure inbound routing and conversation boundaries."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from .config import Config


@dataclass(frozen=True)
class Request:
    post_id: str
    channel_id: str
    user_id: str
    root_id: str
    text: str
    is_dm: bool
    session_key: str

    def context(self, server: str) -> dict[str, str]:
        return {"server": server, "post_id": self.post_id, "channel_id": self.channel_id,
                "user_id": self.user_id, "root_id": self.root_id}


def route_event(event: dict, config: Config, bot_id: str, bot_username: str) -> Request | None:
    if event.get("event") != "posted":
        return None
    data = event.get("data")
    if not isinstance(data, dict):
        return None
    try:
        post = json.loads(data.get("post", ""))
    except (TypeError, ValueError):
        return None
    if not isinstance(post, dict):
        return None
    fields = {name: post.get(name) for name in ("id", "channel_id", "user_id", "message")}
    if any(not isinstance(value, str) for value in fields.values()):
        return None
    if any(not fields[name] for name in ("id", "channel_id", "user_id")):
        return None
    if post.get("type") or post.get("delete_at") or fields["user_id"] == bot_id:
        return None
    if not config.allow_all_users and fields["user_id"] not in config.allowed_users:
        return None
    channel_type = data.get("channel_type")
    if channel_type not in {"D", "G", "O", "P"}:
        return None
    is_dm = channel_type == "D"
    if not is_dm and config.allowed_channels and fields["channel_id"] not in config.allowed_channels:
        return None
    mention = re.compile(r"(?<![\w@])@" + re.escape(bot_username) + r"(?![\w.\-])", re.IGNORECASE)
    text = fields["message"]
    if not is_dm and config.require_mention and not mention.search(text):
        return None
    text = mention.sub("", text).strip()
    if not text or len(text) > config.max_input_chars:
        return None
    root = post.get("root_id") or fields["id"]
    if not isinstance(root, str):
        return None
    parts = [config.url.rstrip("/"), bot_id, fields["channel_id"]]
    if not is_dm:
        parts.append(root)
        if config.session_scope == "user":
            parts.append(fields["user_id"])
    digest = hashlib.sha256(json.dumps(parts, separators=(",", ":")).encode()).hexdigest()
    return Request(fields["id"], fields["channel_id"], fields["user_id"], root, text, is_dm, digest)
