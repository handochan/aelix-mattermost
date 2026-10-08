"""Pure inbound routing and conversation boundaries."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from .config import Config
from .mentions import text_ranges

# Integrations post with a person's user_id: an incoming webhook with its owner's, a custom
# slash command response with its caller's. Their props reveal them. A plugin slash command
# that posts through the plugin API as the caller sets none of these and cannot be detected.
INTEGRATION_PROPS = ("from_webhook", "from_oauth_app", "from_plugin")
_SYSTEM_MENTIONS = frozenset({"@here", "@channel", "@all"})
_SUFFIX = ".-:_"
_LEADING_LINE = re.compile(r"(?:[ \t]*\r?\n)*([ \t]*)")


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


def _word_char(char: str) -> bool:
    return char in ":.-_@" or unicodedata.category(char)[0] in "LN"


def _fields(text: str, start: int, stop: int, keep: Callable[[str], bool]) -> Iterator[tuple[int, int]]:
    """Go's strings.FieldsFunc over text[start:stop]: maximal runs of kept characters."""
    begin = -1
    for index in range(start, stop):
        if keep(text[index]):
            if begin < 0:
                begin = index
        elif begin >= 0:
            yield begin, index
            begin = -1
    if begin >= 0:
        yield begin, stop


def _same(word: str, keyword: str) -> bool:
    # Lower-casing never shortens a string, so a longer word can never match.
    return len(word) <= len(keyword) and word.lower() == keyword


def _without_suffix(word: str, keyword: str) -> bool:
    """Whether dropping trailing ".-:_" one character at a time reaches the keyword."""
    stem = len(word.rstrip(_SUFFIX))
    return any(_same(word[:size], keyword) for size in range(min(len(word) - 1, len(keyword)), stem - 1, -1))


def mention_spans(text: str, username: str) -> list[tuple[int, int]]:
    """Spans of `text` that Mattermost counts as a mention of @username.

    A port of StandardMentionParser.ProcessText (server/channels/app/mention_parser_standard.go,
    unchanged from v9.11 to v11.11) for the single keyword "@username", run like
    getExplicitMentions on markdown text only: code spans, code blocks, link destinations
    and bare URLs never mention anyone. Words are runs of letters, digits and ":.-_@"; a
    ":word:" is an emoji; leading ":.-_" is dropped and trailing ".-:_" is dropped one
    character at a time; a word that is not an @word is split again at ".-:". Case is
    ignored. A span covers the matched word including its dropped trailing characters.

    Exotic input can still differ from the server: HTML character references glued to a
    name ("@name&#97;"), link text that the server splits at "w", ":" or escapes, and the
    rare markdown in which mentions.text_ranges reads code, link targets and bare URLs as
    text and splits no words at emoji (a vertical tab or form feed, or a reference label
    whose match needs Unicode case folding)."""
    keyword = "@" + username.lower()
    if keyword not in text.lower():
        return []
    spans: list[tuple[int, int]] = []
    for start, stop in text_ranges(text):
        for begin, end in _fields(text, start, stop, _word_char):
            if text[begin] == ":" and text[end - 1] == ":":
                continue
            while begin < end and text[begin] in ":.-_":
                begin += 1
            word = text[begin:end]
            if _same(word, keyword) or _without_suffix(word, keyword):
                spans.append((begin, end))
            elif not word.startswith("@") or word in _SYSTEM_MENTIONS:
                spans.extend((a, b) for a, b in _fields(text, begin, end, lambda c: c not in ".-:")
                             if _same(text[a:b], keyword))
    return spans


def _trim(text: str) -> str:
    """strip(), except that a leading indented code block keeps its indentation."""
    indent = _LEADING_LINE.match(text)
    assert indent is not None
    if len(indent[1].expandtabs(4)) >= 4:
        return text[indent.start(1):].rstrip()
    return text.strip()


def strip_mentions(text: str, spans: list[tuple[int, int]]) -> str:
    """Remove the mention spans and the blanks they leave behind; code stays as it is."""
    pieces, cursor = [], 0
    for start, end in spans:
        if start == 0 or text[start - 1].isspace():
            while end < len(text) and text[end] in " \t":
                end += 1
        if end == len(text) or text[end] in "\r\n":
            while start > cursor and text[start - 1] in " \t":
                start -= 1
        pieces.append(text[cursor:start])
        cursor = end
    pieces.append(text[cursor:])
    return _trim("".join(pieces))


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
    props = post.get("props")
    if isinstance(props, dict) and any(props.get(name) in ("true", True) for name in INTEGRATION_PROPS):
        return None
    if not config.allow_all_users and fields["user_id"] not in config.allowed_users:
        return None
    channel_type = data.get("channel_type")
    if channel_type not in {"D", "G", "O", "P"}:
        return None
    is_dm = channel_type == "D"
    if not is_dm and config.allowed_channels and fields["channel_id"] not in config.allowed_channels:
        return None
    spans = mention_spans(fields["message"], bot_username)
    if not is_dm and config.require_mention and not spans:
        return None
    text = strip_mentions(fields["message"], spans)
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
