"""The optional Mattermost custom slash command endpoint: "/aelix status", "/aelix new", ...

Mattermost POSTs a form (token, team_id, channel_id, user_id, root_id, text, response_url,
...) to the command's Request URL and shows the JSON answer to the caller only
("ephemeral"). Commands that need longer than the answer budget reply through response_url,
which is rewritten to this gateway's mattermost.url so that it never reaches other hosts."""

from __future__ import annotations

import asyncio
import hmac
import logging
import re
import uuid
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from aiohttp import web

from .commands import Command, parse_command
from .config import listen_address
from .routing import Request, session_key

if TYPE_CHECKING:
    from .gateway import Gateway

log = logging.getLogger(__name__)

ANSWER_BUDGET = 20.0  # seconds; Mattermost waits OutgoingIntegrationRequestsTimeout (30 s)
_HOOK = re.compile(r"(?:/.*)?/hooks/commands/([a-z0-9]{26})")
_ID = re.compile(r"[a-z0-9]{26}")
SESSION_COMMANDS = frozenset({"new", "status", "model", "usage", "compact"})
# Whoever holds the command token can name any user: admin actions stay in DMs and the CLI.
MESSAGE_ONLY = {"pair": "승인 관련 명령은 봇과의 DM에서 `!pair`로 사용해주세요.",
                "steer": "`/{trigger} steer`는 지원하지 않습니다. 메시지로 `!steer 내용`을 보내주세요.",
                "queue": "`/{trigger} queue`는 지원하지 않습니다. 메시지로 `!queue 내용`을 보내주세요."}
UNKNOWN = "알 수 없는 명령입니다. `/{trigger} help`로 사용법을 확인하세요."
DENIED = "이 봇을 사용할 권한이 없습니다. 봇에게 DM을 보내 사용 승인을 요청하세요."
IN_THREAD = ("채널에서는 대화가 스레드마다 따로 있습니다. 해당 스레드의 답글 입력창에서 "
             "`/{trigger} {name}`을 실행해주세요.")


def ephemeral(text: str) -> web.Response:
    return web.json_response({"response_type": "ephemeral", "text": text})


class SlashServer:
    def __init__(self, gateway: Gateway) -> None:
        self.gateway = gateway
        self.config = gateway.config
        self._runner: web.AppRunner | None = None
        self._tasks: set[asyncio.Task] = set()
        self.port: int | None = None

    async def start(self) -> None:
        assert self.config.slash_listen is not None
        host, port = listen_address(self.config.slash_listen)
        app = web.Application(client_max_size=64 * 1024)
        app.router.add_post("/{tail:.*}", self.handle)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, host, port)
        await site.start()
        sockets = getattr(site._server, "sockets", None) or []  # type: ignore[union-attr]
        self.port = sockets[0].getsockname()[1] if sockets else port
        log.info("Slash command endpoint listening on %s:%s", host, self.port)

    async def close(self) -> None:
        for task in tuple(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._runner is not None:
            await self._runner.cleanup()

    async def handle(self, http: web.Request) -> web.Response:
        try:
            form = await http.post()
        except Exception:
            return web.Response(status=400)
        token = form.get("token")
        if not isinstance(token, str) or not hmac.compare_digest(token.encode(), self.config.slash_token.encode()):
            return web.Response(status=401)
        fields = {name: form.get(name) for name in ("user_id", "channel_id", "root_id", "text", "response_url",
                                                    "trigger_id")}
        user, channel = fields["user_id"], fields["channel_id"]
        if not isinstance(user, str) or not _ID.fullmatch(user) or not isinstance(channel, str) \
                or not _ID.fullmatch(channel):
            return web.Response(status=400)
        trigger = self.config.slash_trigger
        if not self.gateway.authorized(user):
            return ephemeral(DENIED)
        text = fields["text"] if isinstance(fields["text"], str) else ""
        command = parse_command("!" + text.strip()) if text.strip() else Command("help")
        if command is None:
            return ephemeral(UNKNOWN.format(trigger=trigger))
        if command.name in MESSAGE_ONLY:
            return ephemeral(MESSAGE_ONLY[command.name].format(trigger=trigger))
        try:
            kind, name = await self.gateway.channel_kind(channel)
        except Exception:
            return ephemeral("채널 정보를 가져오지 못했습니다. 잠시 후 다시 시도해주세요.")
        is_dm = kind == "D"
        if not is_dm and self.config.allowed_channels and channel not in self.config.allowed_channels:
            return ephemeral("이 채널에서는 봇을 사용할 수 없습니다.")
        root = fields["root_id"] if isinstance(fields["root_id"], str) and _ID.fullmatch(fields["root_id"]) else ""
        if not is_dm and not root:
            if command.name == "stop":
                return ephemeral(await self.gateway.stop_in_channel(channel, user))
            if command.name in SESSION_COMMANDS:
                return ephemeral(IN_THREAD.format(trigger=trigger, name=command.name))
        identifier = fields["trigger_id"] if isinstance(fields["trigger_id"], str) else ""
        request = Request(f"slash:{identifier[:32] or uuid.uuid4().hex}", channel, user, root or channel, "",
                          is_dm, session_key(self.config, self.gateway.bot_id, channel, root, user, is_dm),
                          kind, 0, (), (), "", name)
        work = asyncio.ensure_future(self.gateway.command(request, command, slash=True))
        try:
            answer = await asyncio.wait_for(asyncio.shield(work), ANSWER_BUDGET)
        except TimeoutError:
            task = asyncio.ensure_future(self._follow_up(work, fields["response_url"]))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            return ephemeral("처리 중입니다. 끝나면 알려드리겠습니다.")
        except Exception as exc:
            log.warning("Slash command failed (%s)", type(exc).__name__)
            return ephemeral("명령을 처리하지 못했습니다.")
        return ephemeral(answer or "완료했습니다.")

    async def _follow_up(self, work: asyncio.Future, response_url: object) -> None:
        try:
            answer = await work
        except Exception as exc:
            log.warning("Slash command failed (%s)", type(exc).__name__)
            answer = "명령을 처리하지 못했습니다."
        path = urlsplit(response_url).path if isinstance(response_url, str) else ""
        hook = _HOOK.fullmatch(path)
        if hook is None:
            return
        # The hook id is kept, the host and any sub-path come from mattermost.url.
        url = f"{self.config.url.rstrip('/')}/hooks/commands/{hook[1]}"
        session = self.gateway.client.session
        if session is None:
            return
        try:
            async with session.post(url, json={"response_type": "ephemeral", "text": answer or "완료했습니다."},
                                    allow_redirects=False) as response:
                if response.status >= 300:
                    log.warning("Slash command follow-up failed (HTTP %s)", response.status)
        except Exception as exc:
            log.warning("Slash command follow-up failed (%s)", type(exc).__name__)
