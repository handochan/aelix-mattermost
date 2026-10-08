"""Admission control, user/thread sessions, reply delivery and bot command handling."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .mattermost import MattermostClient, MattermostError, split_message
from .routing import Request, route_event
from .rpc import RpcError, RpcProcess, RpcTimeout
from .storage import Store, private_directory, write_context, write_json

log = logging.getLogger(__name__)

HELP = (
    "**Aelix Mattermost**\n"
    "DM으로 질문하거나 채널·그룹 DM에서 `@aelix 질문`을 보내세요.\n"
    "같은 질문의 후속 대화는 스레드에서 이어가세요.\n"
    "`!help`: 사용법 · `!cancel`: 내가 실행 중인 요청 취소 · "
    "`!reset`: 이 대화의 모델 컨텍스트 초기화\n"
    "이 버전은 텍스트 대화를 지원합니다. 첨부파일 자동 다운로드는 지원하지 않습니다."
)
PREPARING = "응답을 준비하고 있습니다…"
DELIVERED = "응답을 아래 스레드에 게시했습니다."
PARTIAL = "응답 일부를 전송하지 못했습니다. 다시 요청해주세요."
CANCELLED = "요청을 취소했습니다."
TIMED_OUT = "실행 시간 제한을 초과하여 요청을 중단했습니다."
FAILED = "요청을 완료하지 못했습니다. 관리자에게 Gateway와 모델 연결 상태 확인을 요청해주세요."
RESTARTED = "게이트웨이가 재시작되어 이 요청이 중단되었습니다. 다시 요청해주세요."
STOPPED = "게이트웨이가 종료되어 이 요청이 중단되었습니다. 다시 요청해주세요."
BUSY = "현재 요청이 많습니다. 잠시 후 다시 요청해주세요."
EMPTY = "응답 내용이 없습니다."
HEALTH_VERSION = 1
# The gateway composes these messages itself; they never carry provider or user text.
_OWN_ERRORS = (RpcError, MattermostError)
# What a stored placeholder shows once Mattermost accepts the edit, by its request's final
# status; a request that was still running shows STOPPED or RESTARTED instead.
_SETTLED = {"done": DELIVERED, "failed": FAILED, "cancelled": CANCELLED}


def _describe(exc: BaseException) -> str:
    message = str(exc) if isinstance(exc, _OWN_ERRORS) else ""
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


def _transient(exc: BaseException) -> bool:
    """A Mattermost failure worth retrying later: network, 401, 429 or 5xx."""
    status = getattr(exc, "status", None) if isinstance(exc, MattermostError) else None
    return status is None or status in (401, 429) or status >= 500


@dataclass
class Session:
    key: str
    work_dir: Path
    session_dir: Path
    context_file: Path
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    rpc: RpcProcess | None = None
    # An evicted child that may still be exiting: Aelix keeps the transcript locked until then.
    retired: RpcProcess | None = None
    pending: int = 0
    owner: str | None = None
    running: asyncio.Task | None = None
    cancelled: bool = False
    last_used: float = field(default_factory=time.monotonic)


class Gateway:
    queue_size = 1000  # received events waiting for the consumer
    health_interval = 10.0  # health.json is rewritten at least this often
    prune_interval = 3600.0  # old dedup rows are removed this often
    notice_timeout = 5.0  # budget for shutdown notices in close()
    recovery_timeout = 30.0  # budget for restart notices in run()

    def __init__(self, config: Config, client: MattermostClient, store: Store,
                 bot_id: str, bot_username: str) -> None:
        self.config, self.client, self.store = config, client, store
        self.bot_id, self.bot_username = bot_id, bot_username
        self.sessions: dict[str, Session] = {}
        self._sessions_lock = asyncio.Lock()
        self._slots = asyncio.Semaphore(config.max_concurrent_runs)
        self._tasks: set[asyncio.Task] = set()
        self._placeholders: dict[str, str] = {}  # request post ID -> unretired placeholder
        # Placeholders a previous process left behind; run() tells their requesters.
        self._interrupted = store.placeholders()
        self._closing = False

    # -- event intake -----------------------------------------------------------

    async def run(self) -> None:
        """Serve until the WebSocket reader fails (an AuthenticationError ends it).

        The reader only queues events, so slow handling never stalls the WebSocket
        heartbeat; one consumer handles them in order."""
        queue: asyncio.Queue[dict] = asyncio.Queue(self.queue_size)
        recovery = asyncio.create_task(self._recover())
        tasks = [asyncio.create_task(x) for x in (self._read(queue), self._consume(queue),
                                                  self._reap(), self._beat())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            for task in (*tasks, recovery):
                task.cancel()
            await asyncio.gather(*tasks, recovery, return_exceptions=True)

    async def _read(self, queue: asyncio.Queue[dict]) -> None:
        dropped, warned = 0, -float("inf")
        async for event in self.client.events():
            if event.get("event") != "posted":
                continue  # typing, status and other events never become requests
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                dropped += 1
                if time.monotonic() - warned >= 10:
                    log.warning("Event queue is full (%d); dropped %d Mattermost post(s)",
                                queue.maxsize, dropped)
                    dropped, warned = 0, time.monotonic()

    async def _consume(self, queue: asyncio.Queue[dict]) -> None:
        while True:
            event = await queue.get()
            try:
                await self.handle(event)
            except Exception as exc:
                log.warning("Could not handle a Mattermost event (%s)", _describe(exc))

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
                log.warning("Command failed post=%s (%s)", request.post_id[:8], _describe(exc))
                self.store.finish(request.post_id, "failed")
            return
        session = await self._get_session(request.session_key)
        if self._closing:  # close() began while this post was admitted: it starts nothing
            self.store.finish(request.post_id, "interrupted")
            return
        if session is None or session.pending >= self.config.max_queue_per_session + 1:
            self.store.finish(request.post_id, "rejected")
            await self._safe_reply(request, BUSY)
            return
        session.pending += 1
        task = asyncio.create_task(self._answer(session, request))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _get_session(self, key: str) -> Session | None:
        async with self._sessions_lock:
            if key in self.sessions:
                return self.sessions[key]
            if len(self.sessions) >= self.config.max_sessions:
                idle = [s for s in self.sessions.values() if s.pending == 0 and not s.lock.locked()]
                if not idle:
                    return None
                oldest = min(idle, key=lambda s: s.last_used)
                for rpc in (oldest.rpc, oldest.retired):
                    if rpc is not None:
                        await rpc.close()
                self.sessions.pop(oldest.key)
            work = self.config.work_dir / key
            session_dir = self.config.state_dir / "sessions" / key
            for path in (work, session_dir):
                private_directory(path)
            # Resolved once, so stored transcripts compare equal across symlinks (/var on macOS).
            work, session_dir = work.resolve(), session_dir.resolve()
            result = Session(key, work, session_dir, session_dir / "request-context.json")
            self.sessions[key] = result
            return result

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

    # -- one request ------------------------------------------------------------

    async def _answer(self, session: Session, request: Request) -> None:
        try:
            async with session.lock:
                await self._perform(session, request)
        finally:
            session.pending -= 1
            session.last_used = time.monotonic()

    async def _perform(self, session: Session, request: Request) -> None:
        """Run one prompt; the answer and every final notice are new thread posts."""
        placeholder: str | None = None
        try:
            try:
                async with self._slots:
                    session.owner, session.cancelled = request.user_id, False
                    session.running = asyncio.current_task()
                    write_context(session.context_file, request.context(self.config.url))
                    placeholder = await self._placeholder(request)
                    rpc = await self._child(session)
                    answer = await rpc.run(request.text)
                    assert rpc.session_file is not None
                    self.store.save_session(session.key, rpc.session_file)
                    self._run_ended(session)  # answered: nothing is left to cancel
            except asyncio.CancelledError:
                if not session.cancelled:
                    raise
                task = asyncio.current_task()
                if task is not None:
                    task.uncancel()  # handled: the requester asked for it
                status, notice = "cancelled", CANCELLED
            except Exception as exc:
                self._report(session, request, exc)
                status = "cancelled" if session.cancelled else "failed"
                notice = (CANCELLED if session.cancelled else
                          TIMED_OUT if isinstance(exc, RpcTimeout) else FAILED)
            else:
                self.store.finish(request.post_id, await self._deliver(request, placeholder, answer))
                return
            self._run_ended(session)  # a late !cancel must not cancel the final notice
            await self._conclude(request, placeholder, notice)
            self.store.finish(request.post_id, status)
        except asyncio.CancelledError:
            # Shutdown: close() (or the next start) tells the requester.
            self.store.finish(request.post_id, "interrupted")
            raise
        finally:
            session.owner = session.running = None
            session.cancelled = False  # a later shutdown is not this requester's cancel

    @staticmethod
    def _run_ended(session: Session) -> None:
        session.running, session.last_used = None, time.monotonic()

    async def _child(self, session: Session) -> RpcProcess:
        """The session's live Aelix child, started or resumed from its transcript on demand."""
        if session.rpc is not None:
            if session.rpc.alive:
                return session.rpc
            await session.rpc.close()  # a dead child may still hold its process
            session.rpc = None
        if session.retired is not None:  # Aelix refuses a transcript a live process owns
            await session.retired.close()
            session.retired = None
        await self._make_room(session)
        session.rpc = RpcProcess(self.config, session.work_dir, session.session_dir,
                                 session.context_file, self._transcript(session))
        await session.rpc.start()
        return session.rpc

    async def _make_room(self, current: Session) -> None:
        """Close least-recently-used idle children until one more fits max_live_processes.

        A child is idle when its session has no run in progress and Aelix is not compacting
        after an answer; a request that only waits for a run slot needs no child until it
        gets one. When every other child is busy, the cap is exceeded."""
        while True:
            held = [s for s in self.sessions.values() if s is not current and s.rpc is not None]
            if len(held) < self.config.max_live_processes:
                return
            for victim in sorted(held, key=lambda s: (s.rpc is not None and s.rpc.alive, s.last_used)):
                rpc = victim.rpc
                if rpc is None or victim.running is not None or not await rpc.idle():
                    continue
                if victim.rpc is rpc and victim.running is None:  # still idle after the probe
                    break
            else:
                return
            # Its session lock may belong to a request waiting for a run slot, so the child is
            # detached instead; that request's _child() waits until it has exited.
            victim.rpc, victim.retired = None, rpc
            await rpc.close()

    def _transcript(self, session: Session) -> Path | None:
        """The stored transcript to resume, if it is a file inside the session directory.

        A mapping that points elsewhere (a moved state_dir finds its file by name) or to a
        missing file is dropped, so the conversation starts fresh instead of failing."""
        stored = self.store.session_file(session.key)
        if stored is None:
            return None
        root = session.session_dir.resolve()
        for candidate in (stored, root / stored.name):
            try:
                resolved = candidate.resolve()
                if resolved.is_relative_to(root) and resolved.is_file():
                    return resolved
            except (OSError, RuntimeError):
                continue
        log.warning("Dropped an unusable stored transcript session=%s; starting a new conversation",
                    session.key[:8])
        self.store.reset_session(session.key)
        return None

    def _report(self, session: Session, request: Request, exc: BaseException) -> None:
        log.warning("Request failed post=%s session=%s (%s)",
                    request.post_id[:8], session.key[:8], _describe(exc))
        if self.config.log_aelix_stderr and session.rpc is not None:
            tail = session.rpc.stderr_tail()  # redacted by RpcProcess
            if tail:
                log.warning("Aelix stderr post=%s session=%s:\n%s", request.post_id[:8], session.key[:8], tail)

    # -- delivery -----------------------------------------------------------------

    async def _placeholder(self, request: Request) -> str:
        post = await self.client.post(request.channel_id, request.root_id, PREPARING)
        placeholder = post.get("id")
        if not isinstance(placeholder, str) or not placeholder:
            raise MattermostError("Mattermost did not return a placeholder post ID")
        self._placeholders[request.post_id] = placeholder
        self.store.set_placeholder(request.post_id, placeholder)
        return placeholder

    async def _deliver(self, request: Request, placeholder: str, answer: str) -> str:
        """Post the answer as new thread posts, which notify (edits do not), then remove
        the placeholder. A chunk that fails stops delivery; delivered chunks stay as they are."""
        chunks = split_message(answer, self.config.max_post_chars) or [EMPTY]
        for index, chunk in enumerate(chunks):
            try:
                await self.client.post(request.channel_id, request.root_id, chunk)
            except Exception as exc:
                log.warning("Could not deliver an answer post=%s chunk=%d/%d (%s)",
                            request.post_id[:8], index + 1, len(chunks), _describe(exc))
                await self._conclude(request, placeholder, PARTIAL if index else FAILED)
                return "failed"
        await self._retire(request, placeholder, DELIVERED)
        return "done"

    async def _conclude(self, request: Request, placeholder: str | None, notice: str) -> None:
        """Post a final notice as a new thread post and remove the placeholder; when the
        notice cannot be posted, the placeholder shows it instead."""
        try:
            await self.client.post(request.channel_id, request.root_id, notice)
        except Exception as exc:
            log.warning("Could not post a notice post=%s (%s)", request.post_id[:8], _describe(exc))
            if placeholder is not None:
                await self._retire(request, placeholder, notice, delete=False)
            return
        if placeholder is not None:
            await self._retire(request, placeholder, notice)

    async def _retire(self, request: Request, placeholder: str, text: str, delete: bool = True) -> None:
        """Delete the placeholder, or patch it to `text` when that fails; then forget it.

        After a transient failure it stays stored, so close() or the next start edits it."""
        if delete:
            try:
                await self.client.delete_post(placeholder)
            except Exception as exc:
                log.warning("Could not delete a placeholder post=%s (%s)", request.post_id[:8], _describe(exc))
            else:
                self._forget(request.post_id)
                return
        try:
            await self.client.patch(placeholder, text)
        except Exception as exc:
            log.warning("Could not update a placeholder post=%s (%s)", request.post_id[:8], _describe(exc))
            if _transient(exc):
                return
        self._forget(request.post_id)

    def _forget(self, post_id: str) -> None:
        self._placeholders.pop(post_id, None)
        self.store.set_placeholder(post_id, None)

    async def _late_notice(self, post_id: str, placeholder: str, unfinished: str) -> None:
        """Patch a stored placeholder to its request's outcome (`unfinished` if it never
        finished); keep it for later on transient errors."""
        text = _SETTLED.get(self.store.status(post_id) or "", unfinished)
        try:
            await self.client.patch(placeholder, text)
        except Exception as exc:
            if _transient(exc):
                log.warning("Could not update a stored placeholder post=%s (%s)", post_id[:8], _describe(exc))
                return
            # 403/404: the placeholder is gone, so there is nobody left to tell.
        self._forget(post_id)

    async def _safe_reply(self, request: Request, text: str) -> None:
        try:
            await self.client.reply(request.channel_id, request.root_id, text)
        except Exception as exc:
            log.warning("Could not deliver a bot reply post=%s (%s)", request.post_id[:8], _describe(exc))

    # -- background work ----------------------------------------------------------

    async def _recover(self) -> None:
        """Placeholders a previous process left get a restart notice (or their outcome)."""
        rows, self._interrupted = self._interrupted, []
        try:
            async with asyncio.timeout(self.recovery_timeout):
                for post_id, placeholder in rows:
                    await self._late_notice(post_id, placeholder, RESTARTED)
        except TimeoutError:
            log.warning("Restart notices are incomplete; the rest follow on the next start")

    async def _reap(self) -> None:
        """Stop idle children and forget dedup rows older than dedup_days."""
        interval = min(30, self.config.idle_timeout)
        pruned = time.monotonic()
        while True:
            await asyncio.sleep(interval)
            try:
                if time.monotonic() - pruned >= self.prune_interval:
                    pruned = time.monotonic()
                    self.store.prune()
                await self._stop_idle_children()
            except Exception as exc:
                log.warning("Idle cleanup failed (%s)", _describe(exc))

    async def _stop_idle_children(self) -> None:
        for session in tuple(self.sessions.values()):
            if session.pending or session.lock.locked():
                continue
            async with session.lock:
                if session.pending == 0 and time.monotonic() - session.last_used > self.config.idle_timeout:
                    if session.rpc is not None:
                        await session.rpc.close()
                        session.rpc = None

    async def _beat(self) -> None:
        while not self._closing:
            self._write_health()
            await asyncio.sleep(self.health_interval)

    def _write_health(self) -> None:
        """state_dir/health.json for `aelix-mattermost healthcheck` (written atomically, 0600)."""
        connected = bool(self.client.connected) and not self._closing
        data = {"version": HEALTH_VERSION, "pid": os.getpid(), "updated_at": time.time(),
                "websocket_connected": connected,
                "connected_since": self.client.connected_since if connected else None,
                "last_event_at": self.client.last_event_at}
        try:
            write_json(self.config.state_dir / "health.json", data)
        except OSError as exc:
            log.warning("Could not write health.json (%s)", type(exc).__name__)

    async def drain(self) -> None:
        """Wait for admitted requests; also useful for local contract tests."""
        while self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)

    async def close(self) -> None:
        """Cancel requests, tell their requesters and stop every child, all concurrently."""
        self._closing = True
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        children = [rpc for s in self.sessions.values() for rpc in (s.rpc, s.retired) if rpc is not None]
        await asyncio.gather(asyncio.gather(*tasks, return_exceptions=True), self._shutdown_notices(),
                             *(rpc.close() for rpc in children), return_exceptions=True)
        self._write_health()

    async def _shutdown_notices(self) -> None:
        pending = list(self._placeholders.items())
        try:
            async with asyncio.timeout(self.notice_timeout):
                await asyncio.gather(*(self._late_notice(post_id, placeholder, STOPPED)
                                       for post_id, placeholder in pending))
        except TimeoutError:
            log.warning("Shutdown notices are incomplete; the rest follow on the next start")
