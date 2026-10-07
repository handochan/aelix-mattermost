"""Admission control, user/thread sessions and bot command handling."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .mattermost import MattermostClient
from .routing import Request, route_event
from .rpc import RpcError, RpcProcess, RpcTimeout
from .storage import Store, private_directory, write_context

log = logging.getLogger(__name__)

HELP = (
    "**Aelix Mattermost**\n"
    "DM으로 질문하거나 채널·그룹 DM에서 `@aelix 질문`을 보내세요.\n"
    "같은 질문의 후속 대화는 스레드에서 이어가세요.\n"
    "`!help`: 사용법 · `!cancel`: 내가 실행 중인 요청 취소 · "
    "`!reset`: 이 대화의 모델 컨텍스트 초기화\n"
    "이 버전은 텍스트 대화를 지원합니다. 첨부파일 자동 다운로드는 지원하지 않습니다."
)


@dataclass
class Session:
    key: str
    work_dir: Path
    session_dir: Path
    context_file: Path
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    rpc: RpcProcess | None = None
    pending: int = 0
    owner: str | None = None
    running: asyncio.Task | None = None
    cancelled: bool = False
    last_used: float = field(default_factory=time.monotonic)


class Gateway:
    def __init__(self, config: Config, client: MattermostClient, store: Store,
                 bot_id: str, bot_username: str) -> None:
        self.config, self.client, self.store = config, client, store
        self.bot_id, self.bot_username = bot_id, bot_username
        self.sessions: dict[str, Session] = {}
        self._sessions_lock = asyncio.Lock()
        self._slots = asyncio.Semaphore(config.max_concurrent_runs)
        self._tasks: set[asyncio.Task] = set()
        self._closing = False

    async def _get_session(self, key: str) -> Session | None:
        async with self._sessions_lock:
            if key in self.sessions:
                return self.sessions[key]
            if len(self.sessions) >= self.config.max_sessions:
                idle = [s for s in self.sessions.values() if s.pending == 0 and not s.lock.locked()]
                if not idle:
                    return None
                oldest = min(idle, key=lambda s: s.last_used)
                if oldest.rpc is not None:
                    await oldest.rpc.close()
                self.sessions.pop(oldest.key)
            work = self.config.work_dir / key
            session_dir = self.config.state_dir / "sessions" / key
            for path in (work, session_dir):
                private_directory(path)
            result = Session(key, work, session_dir, session_dir / "request-context.json")
            self.sessions[key] = result
            return result

    async def handle(self, event: dict) -> None:
        if self._closing:
            return
        request = route_event(event, self.config, self.bot_id, self.bot_username)
        if request is None or not self.store.claim(request.post_id):
            return
        if request.text in {"!help", "!cancel", "!reset"}:
            try:
                await self._command(request)
                self.store.finish(request.post_id)
            except Exception as exc:
                log.warning("Command failed (%s)", type(exc).__name__)
                self.store.finish(request.post_id, "failed")
            return
        session = await self._get_session(request.session_key)
        if session is None or session.pending >= self.config.max_queue_per_session + 1:
            self.store.finish(request.post_id, "rejected")
            await self._safe_reply(request, "현재 요청이 많습니다. 잠시 후 다시 요청해주세요.")
            return
        session.pending += 1
        task = asyncio.create_task(self._answer(session, request))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _command(self, request: Request) -> None:
        if request.text == "!help":
            await self._safe_reply(request, HELP.replace("@aelix", "@" + self.bot_username))
            return
        session = self.sessions.get(request.session_key)
        if request.text == "!cancel":
            if session is None or session.owner != request.user_id or session.running is None:
                await self._safe_reply(request, "이 대화에 취소할 내 실행 요청이 없습니다.")
                return
            if not session.cancelled:
                session.cancelled = True
                session.running.cancel()
            return
        # Reset is not deletion: retained transcripts remain available to the operator.
        if session is not None:
            if session.pending:
                await self._safe_reply(request, "요청 완료 또는 취소 후 다시 초기화해주세요.")
                return
            async with session.lock:
                if session.rpc is not None:
                    await session.rpc.close()
                    session.rpc = None
                self.store.reset_session(request.session_key)
        else:
            self.store.reset_session(request.session_key)
        await self._safe_reply(request, "이 대화의 모델 컨텍스트를 초기화했습니다.")

    async def _answer(self, session: Session, request: Request) -> None:
        try:
            async with session.lock:
                await self._perform(session, request)
        finally:
            session.pending -= 1
            session.last_used = time.monotonic()

    async def _perform(self, session: Session, request: Request) -> None:
        placeholder: str | None = None
        try:
            async with self._slots:
                session.owner = request.user_id
                session.running = asyncio.current_task()
                session.cancelled = False
                write_context(session.context_file, request.context(self.config.url))
                post = await self.client.post(request.channel_id, request.root_id, "응답을 준비하고 있습니다…")
                placeholder = post.get("id")
                if not isinstance(placeholder, str) or not placeholder:
                    raise RpcError("Mattermost did not return a placeholder post ID")
                if session.rpc is None or not session.rpc.alive:
                    stored = self.store.session_file(session.key)
                    if stored is not None and not stored.resolve().is_relative_to(session.session_dir):
                        raise RpcError("Stored session escaped its conversation directory")
                    session.rpc = RpcProcess(self.config, session.work_dir, session.session_dir,
                                             session.context_file, stored)
                    await session.rpc.start()
                answer = await session.rpc.run(request.text)
                assert session.rpc.session_file is not None
                self.store.save_session(session.key, session.rpc.session_file)
                await self.client.reply(request.channel_id, request.root_id, answer, placeholder)
                self.store.finish(request.post_id)
        except asyncio.CancelledError:
            self.store.finish(request.post_id, "cancelled" if session.cancelled else "interrupted")
            if session.rpc is not None:
                await session.rpc.close()
            if session.cancelled:
                await self._safe_reply(request, "요청을 취소했습니다.", placeholder)
                return
            raise
        except Exception as exc:
            self.store.finish(request.post_id, "failed")
            log.warning("Request failed (%s)", type(exc).__name__)
            if session.cancelled:
                message = "요청을 취소했습니다."
            elif isinstance(exc, RpcTimeout):
                message = "실행 시간 제한을 초과하여 요청을 중단했습니다."
            else:
                message = "요청을 완료하지 못했습니다. 관리자에게 Gateway와 모델 연결 상태 확인을 요청해주세요."
            await self._safe_reply(request, message, placeholder)
        finally:
            session.owner = None
            session.running = None

    async def _safe_reply(self, request: Request, text: str, placeholder: str | None = None) -> None:
        try:
            await self.client.reply(request.channel_id, request.root_id, text, placeholder)
        except Exception as exc:
            log.warning("Could not deliver bot reply (%s)", type(exc).__name__)

    async def _reap(self) -> None:
        interval = min(30, self.config.idle_timeout)
        while True:
            await asyncio.sleep(interval)
            for session in tuple(self.sessions.values()):
                if session.pending or session.lock.locked():
                    continue
                async with session.lock:
                    if session.pending == 0 and time.monotonic() - session.last_used > self.config.idle_timeout:
                        if session.rpc is not None:
                            await session.rpc.close()
                            session.rpc = None

    async def run(self) -> None:
        reaper = asyncio.create_task(self._reap())
        try:
            async for event in self.client.events():
                await self.handle(event)
        finally:
            reaper.cancel()
            await asyncio.gather(reaper, return_exceptions=True)

    async def drain(self) -> None:
        """Wait for admitted requests; also useful for local contract tests."""
        while self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)

    async def close(self) -> None:
        self._closing = True
        for task in tuple(self._tasks):
            task.cancel()
        await asyncio.gather(*tuple(self._tasks), return_exceptions=True)
        for session in self.sessions.values():
            if session.rpc is not None:
                await session.rpc.close()
