"""Mattermost v4 REST and authenticated/reconnecting WebSocket transport."""

from __future__ import annotations

import asyncio
import json
import logging
import random
from collections.abc import AsyncIterator
from urllib.parse import urlsplit, urlunsplit

import aiohttp

from .config import Config

log = logging.getLogger(__name__)


class MattermostError(RuntimeError):
    pass


class AuthenticationError(MattermostError):
    pass


def split_message(text: str, limit: int) -> list[str]:
    chunks: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        else:
            cut += 1
        chunks.append(text[:cut])
        text = text[cut:]
    if text:
        chunks.append(text)
    return chunks


class MattermostClient:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.base = config.url.rstrip("/") + "/api/v4"
        self.session: aiohttp.ClientSession | None = None

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

    async def api(self, method: str, path: str, data: dict | None = None) -> dict:
        if self.session is None:
            raise MattermostError("Client is not open")
        async with self.session.request(
            method, self.base + "/" + path.lstrip("/"), json=data, allow_redirects=False,
        ) as response:
            if response.status in {401, 403}:
                raise AuthenticationError(f"Mattermost denied {method} {path.split('/')[0]}")
            if not 200 <= response.status < 300:
                raise MattermostError(f"Mattermost HTTP {response.status}")
            try:
                result = await response.json()
            except (ValueError, aiohttp.ContentTypeError) as exc:
                raise MattermostError("Mattermost returned a non-JSON API response") from exc
            if not isinstance(result, dict):
                raise MattermostError("Invalid Mattermost API result")
            return result

    async def me(self) -> dict:
        return await self.api("GET", "users/me")

    async def post(self, channel_id: str, root_id: str, text: str) -> dict:
        return await self.api("POST", "posts", {
            "channel_id": channel_id, "root_id": root_id, "message": text,
            "props": {"disable_mentions": True},
        })

    async def patch(self, post_id: str, text: str) -> dict:
        return await self.api("PUT", f"posts/{post_id}/patch", {
            "message": text, "props": {"disable_mentions": True},
        })

    async def reply(self, channel_id: str, root_id: str, text: str,
                    placeholder_id: str | None = None) -> None:
        chunks = split_message(text, self.config.max_post_chars) or ["응답 내용이 없습니다."]
        for index, chunk in enumerate(chunks):
            if index == 0 and placeholder_id:
                await self.patch(placeholder_id, chunk)
            else:
                await self.post(channel_id, root_id, chunk)

    def websocket_url(self) -> str:
        u = urlsplit(self.base + "/websocket")
        return urlunsplit(("wss" if u.scheme == "https" else "ws", u.netloc, u.path, "", ""))

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
        if self.session is None:
            raise MattermostError("Client is not open")
        delay = 1.0
        while True:
            connected_at = asyncio.get_running_loop().time()
            try:
                async with self.session.ws_connect(
                    self.websocket_url(), heartbeat=30, max_msg_size=1024 * 1024,
                ) as websocket:
                    await websocket.send_json({"seq": 1, "action": "authentication_challenge",
                                               "data": {"token": self.config.token}})
                    buffered: list[dict] = []
                    async with asyncio.timeout(15):
                        while True:
                            message = await websocket.receive()
                            if message.type in {aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR,
                                                aiohttp.WSMsgType.CLOSE}:
                                raise MattermostError("WebSocket closed before authentication")
                            packet = self._packet(message)
                            if packet is None:
                                continue
                            if packet.get("seq_reply") == 1:
                                if packet.get("status") != "OK":
                                    raise AuthenticationError("Mattermost WebSocket authentication failed")
                                break
                            if packet.get("event") == "posted":
                                if len(buffered) >= 100:
                                    raise MattermostError("Too many events before WebSocket authentication")
                                buffered.append(packet)
                    log.info("Mattermost WebSocket connected")
                    for packet in buffered:
                        yield packet
                    async for message in websocket:
                        packet = self._packet(message)
                        if packet is not None:
                            yield packet
                        elif message.type == aiohttp.WSMsgType.ERROR:
                            raise MattermostError("Mattermost WebSocket failed")
            except AuthenticationError:
                raise
            except aiohttp.WSServerHandshakeError as exc:
                if exc.status in {401, 403}:
                    raise AuthenticationError("Mattermost denied the WebSocket connection") from exc
                log.warning("WebSocket handshake failed; reconnecting")
            except (aiohttp.ClientError, MattermostError, TimeoutError, OSError):
                log.warning("WebSocket disconnected; reconnecting")
            if asyncio.get_running_loop().time() - connected_at > 30:
                delay = 1
            await asyncio.sleep(delay * random.uniform(0.8, 1.2))
            delay = min(delay * 2, 30)
