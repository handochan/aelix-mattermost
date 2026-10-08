"""Mattermost v4 REST and header-authenticated, resumable WebSocket transport."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import re
import time
import uuid
from collections.abc import AsyncIterator
from urllib.parse import urlencode, urlsplit, urlunsplit

import aiohttp

from .config import Config
from .mentions import neutralize_mentions

log = logging.getLogger(__name__)

# Sent on every create and every patch: a patch replaces all props, so from_bot keeps the
# BOT badge on servers older than 11.10, and unsafe_links stops server-side link fetching.
POST_PROPS = {"unsafe_links": "true", "from_bot": "true"}
HELLO_TIMEOUT = 15.0
MAX_ATTEMPTS = 3
MAX_RETRY_DELAY = 10.0
# The server deduplicates a pending_post_id for 30 s from the start of the create, so a
# create retry must reach it well within that: short attempts, no retry after 25 s.
CREATE_TIMEOUT = 10.0
CREATE_RETRY_WINDOW = 25.0
MAX_WS_MESSAGE = 8 * 1024 * 1024
_WS_CLOSED = {aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSED,
              aiohttp.WSMsgType.ERROR}


class MattermostError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class AuthenticationError(MattermostError):
    """The token was rejected (HTTP 401, or a WebSocket the server never authenticates)."""


class ForbiddenError(MattermostError):
    """HTTP 403: the token is valid but not allowed (e.g. patching a deleted post)."""


class _NoHello(MattermostError):
    """The WebSocket closed or stayed silent before proving it was authenticated."""


class FileTooLarge(MattermostError):
    """A download exceeded its size limit."""


def _segment(value: str) -> str:
    """A server-provided id used as a URL path segment."""
    if not value or not value.isascii() or not value.isalnum():
        raise MattermostError("Invalid Mattermost id")
    return value


# ---------------------------------------------------------------------------
# Message splitting

_FENCE = re.compile(r"( {0,3})(`{3,}|~{3,})(.*)")


def split_message(text: str, limit: int) -> list[str]:
    """Split text into chunks of at most ``limit`` characters, preferring line breaks.

    A fenced code block cut between chunks is closed at the end of one chunk and reopened
    with the same info string at the start of the next one."""
    if limit < 1:
        raise ValueError("limit must be positive")
    chunks: list[str] = []
    fence: str | None = None
    while text:
        head = fence + "\n" if fence else ""
        if len(head) + len(text) <= limit:
            chunks.append(head + text)
            break
        if len(head) > limit // 2:  # absurd info string: stop carrying the fence
            head, fence = "", None
        body, tail, fence = _take(text, limit - len(head), fence)
        chunks.append(head + body + tail)
        text = text[len(body):]
    return chunks


def _take(text: str, budget: int, fence: str | None) -> tuple[str, str, str | None]:
    """Cut the next chunk body, reserving room to close a fence the cut leaves open."""
    reserve = 0
    while True:
        body = text[:_cut(text, budget - reserve)]
        still_open = _fence_state(body, fence)
        if still_open is None:
            return body, "", None
        match = _FENCE.fullmatch(still_open)
        assert match is not None
        tail = ("" if body.endswith("\n") else "\n") + match[1] + match[2]
        if len(tail) <= reserve:
            return body, tail, still_open
        if len(tail) > budget // 2:  # absurd fence: plain cut
            return text[:_cut(text, budget)], "", None
        reserve = len(tail)


def _cut(text: str, budget: int) -> int:
    """Cut at the last line break in the second half of the budget, else hard-cut,
    but never inside a fence line."""
    newline = text.rfind("\n", 0, budget)
    if newline >= budget // 2:
        return newline + 1
    start, stop = newline + 1, text.find("\n", budget)
    line = text[start:stop if stop >= 0 else len(text)].rstrip("\r")
    return start if start and _FENCE.fullmatch(line) else budget


def _fence_state(text: str, fence: str | None) -> str | None:
    """Return the opening fence line still open after the complete lines of text."""
    for line in text.split("\n")[:-1]:
        match = _FENCE.fullmatch(line.rstrip("\r"))
        if match is None:
            continue
        if fence is None:
            if match[2][0] == "~" or "`" not in match[3]:
                fence = line.rstrip("\r")
            continue
        opening = _FENCE.fullmatch(fence)
        assert opening is not None
        if match[2][0] == opening[2][0] and len(match[2]) >= len(opening[2]) and not match[3].strip():
            fence = None
    return fence


# ---------------------------------------------------------------------------
# REST and WebSocket client


def _retry_after(value: str | None) -> float | None:
    """Seconds from a Retry-After header; HTTP dates fall back to the normal backoff."""
    try:
        seconds = float(value) if value else math.nan
    except ValueError:
        return None
    return max(seconds, 0.0) if math.isfinite(seconds) else None


class MattermostClient:
    reconnect_delay = 1.0

    def __init__(self, config: Config) -> None:
        self.config = config
        self.base = config.url.rstrip("/") + "/api/v4"
        self.session: aiohttp.ClientSession | None = None
        self.connected = False
        self.connected_since: float | None = None
        self.last_event_at: float | None = None
        self.hello_timeout = HELLO_TIMEOUT
        self._connection_id: str | None = None
        self._sequence: int | None = None  # next event seq expected on _connection_id
        self._action_seq = 0

    async def __aenter__(self) -> MattermostClient:
        connector = aiohttp.TCPConnector(ssl=self.config.ssl_context())
        self.session = aiohttp.ClientSession(
            connector=connector, headers={"Authorization": f"Bearer {self.config.token}"},
            timeout=aiohttp.ClientTimeout(total=30),
        )
        return self

    async def __aexit__(self, *_args: object) -> None:
        if self.session is not None:
            await self.session.close()

    async def api(self, method: str, path: str, data: dict | None = None,
                  attempts: int = MAX_ATTEMPTS) -> dict:
        """Call the REST API, retrying network errors, HTTP 5xx and 429 (Retry-After).

        POST is repeated only with a pending_post_id, and only while the server still
        deduplicates it (CREATE_TIMEOUT per attempt, CREATE_RETRY_WINDOW in total)."""
        result = await self._api(method, path, data, attempts)
        if not isinstance(result, dict):
            raise MattermostError("Invalid Mattermost API result")
        return result

    async def api_list(self, method: str, path: str, data: object = None) -> object:
        """Like api(), for endpoints whose body or result is a JSON array."""
        return await self._api(method, path, data, MAX_ATTEMPTS)

    async def _api(self, method: str, path: str, data: object, attempts: int) -> object:
        if self.session is None:
            raise MattermostError("Client is not open")
        name = path.split("/")[0]
        create = method == "POST" and isinstance(data, dict) and bool(data.get("pending_post_id"))
        if method == "POST" and not create:
            attempts = 1
        options = {"timeout": aiohttp.ClientTimeout(total=CREATE_TIMEOUT)} if create else {}
        first = time.monotonic()
        for attempt in range(1, attempts + 1):
            delay: float | None = None
            try:
                async with self.session.request(
                    method, self.base + "/" + path.lstrip("/"), json=data, allow_redirects=False, **options,
                ) as response:
                    status = response.status
                    if status == 401:
                        raise AuthenticationError(f"Mattermost denied {method} {name}", status)
                    if status == 403:
                        raise ForbiddenError(f"Mattermost forbade {method} {name}", status)
                    if status != 429 and status < 500:
                        if not 200 <= status < 300:
                            raise MattermostError(f"Mattermost HTTP {status}", status)
                        return await self._json(response)
                    error = MattermostError(f"Mattermost HTTP {status}", status)
                    delay = _retry_after(response.headers.get("Retry-After"))
            except (aiohttp.ClientConnectionError, aiohttp.ClientPayloadError, TimeoutError) as exc:
                error = MattermostError(f"Mattermost {method} {name} failed ({type(exc).__name__})")
                error.__cause__ = exc
            if attempt == attempts:
                raise error
            if delay is None:
                delay = 0.5 * 2 ** (attempt - 1) * random.uniform(0.8, 1.2)
            delay = min(delay, MAX_RETRY_DELAY)
            if create and time.monotonic() - first + delay > CREATE_RETRY_WINDOW:
                raise error  # the server may have forgotten the pending_post_id: no duplicate
            log.info("Retrying Mattermost %s %s in %.1fs (%s)", method, name, delay, error)
            await asyncio.sleep(delay)
        raise AssertionError("unreachable")

    @staticmethod
    async def _json(response: aiohttp.ClientResponse) -> dict | list:
        try:
            result = await response.json()
        except (ValueError, aiohttp.ContentTypeError) as exc:
            raise MattermostError("Mattermost returned a non-JSON API response") from exc
        if not isinstance(result, (dict, list)):
            raise MattermostError("Invalid Mattermost API result")
        return result

    async def me(self) -> dict:
        return await self.api("GET", "users/me")

    async def post(self, channel_id: str, root_id: str, text: str,
                   file_ids: list[str] | None = None) -> dict:
        """Create a post; its pending_post_id makes retries idempotent on the server."""
        body = {
            "channel_id": channel_id, "root_id": root_id, "message": neutralize_mentions(text),
            "props": dict(POST_PROPS), "pending_post_id": uuid.uuid4().hex,
        }
        if file_ids:
            body["file_ids"] = list(file_ids)
        return await self.api("POST", "posts", body)

    async def thread(self, root_id: str) -> list[dict]:
        """Every post of a thread, oldest first."""
        result = await self.api("GET", f"posts/{_segment(root_id)}/thread")
        posts, order = result.get("posts"), result.get("order")
        if not isinstance(posts, dict):
            raise MattermostError("Invalid Mattermost thread")
        items = [x for x in posts.values() if isinstance(x, dict)]
        if isinstance(order, list):
            rank = {post_id: index for index, post_id in enumerate(order) if isinstance(post_id, str)}
            items.sort(key=lambda x: rank.get(x.get("id"), -1))  # unknown ids first, then by order
        return sorted(items, key=lambda x: x.get("create_at") if isinstance(x.get("create_at"), int) else 0)

    async def usernames(self, user_ids: list[str]) -> dict[str, str]:
        """user id -> username for the ids the server knows."""
        if not user_ids:
            return {}
        users = await self.api_list("POST", "users/ids", list(user_ids))
        return {x["id"]: x["username"] for x in users
                if isinstance(x, dict) and isinstance(x.get("id"), str) and isinstance(x.get("username"), str)}

    async def channel(self, channel_id: str) -> dict:
        return await self.api("GET", f"channels/{_segment(channel_id)}")

    async def direct_channel(self, user_a: str, user_b: str) -> str:
        """The DM channel of two users (created on first use)."""
        channel = await self.api_list("POST", "channels/direct", [user_a, user_b])
        channel_id = channel.get("id") if isinstance(channel, dict) else None
        if not isinstance(channel_id, str) or not channel_id:
            raise MattermostError("Mattermost did not return a direct channel")
        return channel_id

    async def typing(self, user_id: str, channel_id: str, parent_id: str = "") -> None:
        await self.api("POST", f"users/{_segment(user_id)}/typing",
                       {"channel_id": channel_id, "parent_id": parent_id})

    async def react(self, user_id: str, post_id: str, emoji: str) -> None:
        await self.api("POST", "reactions", {"user_id": user_id, "post_id": post_id, "emoji_name": emoji})

    async def file_info(self, file_id: str) -> dict:
        return await self.api("GET", f"files/{_segment(file_id)}/info")

    async def download(self, file_id: str, limit: int) -> bytes:
        """A file's bytes; FileTooLarge as soon as it exceeds `limit`."""
        if self.session is None:
            raise MattermostError("Client is not open")
        url = f"{self.base}/files/{_segment(file_id)}"
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                async with self.session.get(url, allow_redirects=False,
                                            timeout=aiohttp.ClientTimeout(total=120)) as response:
                    status = response.status
                    if status == 401:
                        raise AuthenticationError("Mattermost denied GET files", status)
                    if status == 403:
                        raise ForbiddenError("Mattermost forbade GET files", status)
                    if 200 <= status < 300:
                        if (response.content_length or 0) > limit:
                            raise FileTooLarge(f"file is larger than {limit} bytes")
                        data = bytearray()
                        async for chunk in response.content.iter_chunked(64 * 1024):
                            data += chunk
                            if len(data) > limit:
                                raise FileTooLarge(f"file is larger than {limit} bytes")
                        return bytes(data)
                    error = MattermostError(f"Mattermost HTTP {status}", status)
                    if status != 429 and status < 500:
                        raise error
            except (aiohttp.ClientConnectionError, aiohttp.ClientPayloadError, TimeoutError) as exc:
                error = MattermostError(f"Mattermost GET files failed ({type(exc).__name__})")
                error.__cause__ = exc
            if attempt == MAX_ATTEMPTS:
                raise error
            await asyncio.sleep(0.5 * 2 ** (attempt - 1) * random.uniform(0.8, 1.2))
        raise AssertionError("unreachable")

    async def upload(self, channel_id: str, name: str, data: bytes, content_type: str) -> str:
        """Upload one file to a channel; returns its file id for a post's file_ids."""
        if self.session is None:
            raise MattermostError("Client is not open")
        form = aiohttp.FormData()
        form.add_field("channel_id", channel_id)
        form.add_field("files", data, filename=name, content_type=content_type)
        try:
            async with self.session.post(f"{self.base}/files", data=form, allow_redirects=False,
                                         timeout=aiohttp.ClientTimeout(total=300)) as response:
                status = response.status
                if status == 401:
                    raise AuthenticationError("Mattermost denied POST files", status)
                if status == 403:
                    raise ForbiddenError("Mattermost forbade POST files", status)
                if not 200 <= status < 300:
                    raise MattermostError(f"Mattermost HTTP {status}", status)
                result = await self._json(response)
        except (aiohttp.ClientConnectionError, aiohttp.ClientPayloadError, TimeoutError) as exc:
            raise MattermostError(f"Mattermost POST files failed ({type(exc).__name__})") from exc
        infos = result.get("file_infos")
        file_id = infos[0].get("id") if isinstance(infos, list) and infos and isinstance(infos[0], dict) else None
        if not isinstance(file_id, str) or not file_id:
            raise MattermostError("Mattermost did not return an uploaded file id")
        return file_id

    async def patch(self, post_id: str, text: str) -> dict:
        return await self.api("PUT", f"posts/{post_id}/patch", {
            "message": neutralize_mentions(text), "props": dict(POST_PROPS),
        })

    async def delete_post(self, post_id: str) -> None:
        """Delete a post; one that is already gone (HTTP 404) counts as deleted."""
        try:
            await self.api("DELETE", f"posts/{post_id}")
        except MattermostError as exc:
            if exc.status != 404:
                raise

    async def reply(self, channel_id: str, root_id: str, text: str,
                    placeholder_id: str | None = None) -> None:
        chunks = split_message(text, self.config.max_post_chars) or ["응답 내용이 없습니다."]
        for index, chunk in enumerate(chunks):
            if index == 0 and placeholder_id:
                await self.patch(placeholder_id, chunk)
            else:
                await self.post(channel_id, root_id, chunk)

    def websocket_url(self, resume: bool = False) -> str:
        u = urlsplit(self.base + "/websocket")
        query = ""
        if resume and self._connection_id and self._sequence is not None:
            query = urlencode({"connection_id": self._connection_id, "sequence_number": self._sequence})
        return urlunsplit(("wss" if u.scheme == "https" else "ws", u.netloc, u.path, query, ""))

    @staticmethod
    def _packet(message: aiohttp.WSMessage) -> dict | None:
        if message.type != aiohttp.WSMsgType.TEXT:
            return None
        try:
            value = json.loads(message.data)
        except ValueError:
            return None
        return value if isinstance(value, dict) else None

    async def events(self) -> AsyncIterator[dict]:
        """Yield WebSocket events forever, reconnecting with jittered backoff.

        The Authorization header authenticates the upgrade; the server ignores
        authentication_challenge on such a connection, so it is never sent. Reconnects
        resume the session (connection_id + sequence_number) so missed events are replayed."""
        if self.session is None:
            raise MattermostError("Client is not open")
        delay = self.reconnect_delay
        while True:
            started = time.monotonic()
            resume = self._connection_id is not None and self._sequence is not None
            try:
                async with self.session.ws_connect(
                    self.websocket_url(resume), heartbeat=30, max_msg_size=MAX_WS_MESSAGE,
                ) as websocket:
                    hello, buffered = await self._handshake(websocket, resume, self.hello_timeout, True)
                    self.connected, self.connected_since = True, time.time()
                    log.info("Mattermost WebSocket %s", "connected" if hello is not None else "resumed")
                    for packet in buffered:
                        yield packet
                    async for message in websocket:
                        packet = self._packet(message)
                        if packet is not None and "event" in packet:
                            self._seen(packet)
                            yield packet
                        elif message.type == aiohttp.WSMsgType.ERROR:
                            if getattr(message.data, "code", None) == aiohttp.WSCloseCode.MESSAGE_TOO_BIG:
                                self._sequence = None  # a replay would resend it forever
                            raise MattermostError("Mattermost WebSocket failed")
                    log.warning("Mattermost WebSocket closed; reconnecting")
            except AuthenticationError:
                raise
            except _NoHello:
                if resume:
                    self._sequence = None  # start a fresh session next time
                await self._check_token()
                log.warning("Mattermost WebSocket closed before hello; reconnecting (if this "
                            "persists, check that proxies forward the Authorization header)")
            except aiohttp.WSServerHandshakeError as exc:
                if exc.status in {401, 403}:
                    raise AuthenticationError("Mattermost denied the WebSocket connection", exc.status) from exc
                log.warning("Mattermost WebSocket handshake failed (HTTP %s); reconnecting", exc.status)
            except (aiohttp.ClientError, MattermostError, TimeoutError, OSError) as exc:
                log.warning("Mattermost WebSocket disconnected (%s); reconnecting", type(exc).__name__)
            finally:
                self.connected, self.connected_since = False, None
            if time.monotonic() - started > 30:
                delay = self.reconnect_delay
            await asyncio.sleep(delay * random.uniform(0.8, 1.2))
            delay = min(delay * 2, 30.0)

    async def probe_websocket(self, timeout: float = 10.0) -> dict:
        """Open a separate WebSocket, wait for "hello" and return its data."""
        if self.session is None:
            raise MattermostError("Client is not open")
        try:
            async with asyncio.timeout(timeout):
                async with self.session.ws_connect(self.websocket_url(), max_msg_size=MAX_WS_MESSAGE) as websocket:
                    hello, _ = await self._handshake(websocket, False, timeout, False)
        except _NoHello:
            await self._check_token()
            raise MattermostError("Mattermost closed the WebSocket before hello") from None
        except TimeoutError:
            raise MattermostError(f"No Mattermost WebSocket hello within {timeout:g}s") from None
        except aiohttp.WSServerHandshakeError as exc:
            if exc.status in {401, 403}:
                raise AuthenticationError("Mattermost denied the WebSocket connection", exc.status) from exc
            message = f"Mattermost WebSocket handshake failed (HTTP {exc.status})"
            raise MattermostError(message, exc.status) from exc
        except (aiohttp.ClientError, OSError) as exc:
            raise MattermostError(f"Mattermost WebSocket failed ({type(exc).__name__})") from exc
        return hello or {}

    async def _handshake(self, websocket: aiohttp.ClientWebSocketResponse, resume: bool,
                         timeout: float, track: bool) -> tuple[dict | None, list[dict]]:
        """Wait for proof that the server authenticated this connection.

        A new session starts with "hello". A resumed one gets no hello (unless the server
        lost it), so a replayed event or the reply to a ping proves it instead. An
        unauthenticated connection receives nothing and is closed by the server."""
        ping = None
        if resume:
            self._action_seq += 1
            ping = self._action_seq
            await websocket.send_json({"seq": ping, "action": "ping"})
        buffered: list[dict] = []
        try:
            async with asyncio.timeout(timeout):
                while True:
                    message = await websocket.receive()
                    if message.type in _WS_CLOSED:
                        raise _NoHello("Mattermost closed the WebSocket before hello")
                    packet = self._packet(message)
                    if packet is None:
                        continue
                    if packet.get("event") == "hello":
                        if track:
                            self._hello(packet)
                        data = packet.get("data")
                        return (data if isinstance(data, dict) else {}), buffered
                    if "event" in packet:
                        if track and resume:  # before hello, a seq belongs to no known session
                            self._seen(packet)
                        buffered.append(packet)
                        if resume:
                            return None, buffered
                        if len(buffered) > 100:
                            raise MattermostError("Too many events before WebSocket hello")
                    elif ping is not None and packet.get("seq_reply") == ping:
                        return None, buffered
        except TimeoutError as exc:
            raise _NoHello("No Mattermost WebSocket hello") from exc

    def _hello(self, packet: dict) -> None:
        data = packet.get("data")
        connection_id = data.get("connection_id") if isinstance(data, dict) else None
        if not isinstance(connection_id, str) or not connection_id:
            connection_id = None  # no reliable WebSocket support: never resume
        elif self._connection_id is not None and connection_id != self._connection_id:
            log.warning("Mattermost started a new WebSocket session; events may have been missed")
        self._connection_id = connection_id
        self._seen(packet)

    def _seen(self, packet: dict) -> None:
        self.last_event_at = time.time()
        seq = packet.get("seq")
        if isinstance(seq, int) and not isinstance(seq, bool):
            self._sequence = seq + 1

    async def _check_token(self) -> None:
        """After a WebSocket closed before hello, tell an invalid token from an outage."""
        try:
            await self.api("GET", "users/me", attempts=1)
        except AuthenticationError:
            raise
        except (MattermostError, aiohttp.ClientError, TimeoutError, OSError):
            pass
