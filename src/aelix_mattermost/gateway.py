"""Admission control, user/thread sessions, reply delivery and bot command handling."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import attachments, pairing
from .commands import Command, help_text, parse_command
from .config import Config
from .mattermost import MattermostClient, MattermostError, split_message
from .prompt import HistoryPost, Place, attachments_block, history_block, system_prompt, turn_text
from .routing import INTEGRATION_PROPS, Request, route_event
from .rpc import Activity, NothingToCompact, RpcError, RpcProcess, RpcRunFailed, RpcTimeout, Turn
from .storage import Store, private_directory, write_context, write_json, write_text

log = logging.getLogger(__name__)

PREPARING = "응답을 준비하고 있습니다…"
DELIVERED = "응답을 아래 스레드에 게시했습니다."
MERGED = "이후 메시지와 함께 답변했습니다."
PARTIAL = "응답 일부를 전송하지 못했습니다. 다시 요청해주세요."
CANCELLED = "요청을 취소했습니다."
INTERRUPTED = "새 메시지가 와서 이 요청을 중단했습니다."
TIMED_OUT = "실행 시간 제한을 초과하여 요청을 중단했습니다."
FAILED = "요청을 완료하지 못했습니다. 관리자에게 Gateway와 모델 연결 상태 확인을 요청해주세요."
RESTARTED = "게이트웨이가 재시작되어 이 요청이 중단되었습니다. 다시 요청해주세요."
STOPPED = "게이트웨이가 종료되어 이 요청이 중단되었습니다. 다시 요청해주세요."
BUSY = "현재 요청이 많습니다. 잠시 후 다시 요청해주세요."
EMPTY = "응답 내용이 없습니다."
STEER_EMOJI = "eyes"
HEALTH_VERSION = 1
# The gateway composes these messages itself; they never carry provider or user text.
_OWN_ERRORS = (RpcError, MattermostError)
# What a stored placeholder shows once Mattermost accepts the edit, by its request's final
# status; a request that was still running shows STOPPED or RESTARTED instead.
_SETTLED = {"done": DELIVERED, "failed": FAILED, "cancelled": CANCELLED}
# Bot posts that are gateway notices, not answers: thread history leaves them out.
_NOTICES = frozenset({PREPARING, DELIVERED, MERGED, PARTIAL, CANCELLED, INTERRUPTED, TIMED_OUT, FAILED,
                      RESTARTED, STOPPED, BUSY, EMPTY})
_PHASES = {
    "starting": "⏳ 준비하는 중", "thinking": "⏳ 생각하는 중", "writing": "✍️ 답변을 작성하는 중",
    "tool": "🛠️ 도구 실행 중", "retrying": "🔁 모델 응답을 다시 요청하는 중",
    "compacting": "🗜️ 대화 컨텍스트를 압축하는 중",
}
PROGRESS_MARK = "`!stop`으로 중단"  # every progress text ends with it, so history can skip them
STOP_GRACE = 5.0  # seconds an abort may take before the request task is cancelled
TYPING_INTERVAL = 4.0


def _describe(exc: BaseException) -> str:
    message = str(exc) if isinstance(exc, _OWN_ERRORS) else ""
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


def _transient(exc: BaseException) -> bool:
    """A Mattermost failure worth retrying later: network, 401, 429 or 5xx."""
    status = getattr(exc, "status", None) if isinstance(exc, MattermostError) else None
    return status is None or status in (401, 429) or status >= 500


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class Session:
    key: str
    work_dir: Path
    session_dir: Path
    context_file: Path
    channel_id: str = ""
    channel_type: str = "D"
    channel_name: str = ""
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    rpc: RpcProcess | None = None
    # An evicted child that may still be exiting: Aelix keeps the transcript locked until then.
    retired: RpcProcess | None = None
    rpc_model: str | None = None  # the --model the live child was started with
    pending: int = 0
    owner: str | None = None
    running: asyncio.Task | None = None
    started: float = 0.0
    stopping: str | None = None  # "cancel" or "interrupt" while the running request is stopped
    turning: bool = False  # the running request has handed its prompt to RpcProcess.turn()
    slot_task: asyncio.Task | None = None  # the session's next request, waiting for a run slot
    requests: dict[int, Request] = field(default_factory=dict)  # of the running prompt (see Turn)
    # Admitted requests still waiting for the session lock, and those !stop cancelled.
    waiting: dict[str, tuple[asyncio.Task, Request]] = field(default_factory=dict)
    dropped: set[str] = field(default_factory=set)
    last_used: float = field(default_factory=time.monotonic)

    @property
    def place(self) -> Place:
        return Place(self.channel_type, "" if self.channel_type == "D" else self.channel_name)


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
        self._helpers: set[asyncio.Task] = set()
        self._intake: dict[str, list] = {}  # session key -> [lock, users]: see _ordered()
        self._placeholders: dict[str, str] = {}  # request post ID -> unretired placeholder
        # Placeholders a previous process left behind; run() tells their requesters.
        self._interrupted = store.placeholders()
        self._names: dict[str, str] = {bot_id: bot_username}  # user id -> username
        self._channels: dict[str, tuple[str, str]] = {}  # channel id -> (type, display name)
        self._pair_replies: dict[str, float] = {}  # unknown user -> last pairing reply
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
        """Admit a post and process it in a task of its own, in order within its conversation:
        a slow command or download never holds up other conversations."""
        if self._closing:
            return
        request = route_event(event, self.config, self.bot_id, self.bot_username, self.store.is_paired)
        if request is None or not self.store.claim(request.post_id, request.session_key):
            return
        self._spawn(self._dispatch(request))

    async def _dispatch(self, request: Request) -> None:
        try:
            async with self._ordered(request.session_key):
                await self._process(request)
        except asyncio.CancelledError:
            self._unfinished(request, "interrupted")
            raise
        except Exception as exc:
            log.warning("Could not handle a post=%s (%s)", request.post_id[:8], _describe(exc))
            self._unfinished(request, "failed")

    def _unfinished(self, request: Request, status: str) -> None:
        if self.store.status(request.post_id) == "accepted":
            self.store.finish(request.post_id, status)

    @contextlib.asynccontextmanager
    async def _ordered(self, key: str):
        """Posts of one conversation are processed one after another, in arrival order
        (tasks start in creation order and asyncio locks are first come, first served)."""
        entry = self._intake.setdefault(key, [asyncio.Lock(), 0])
        entry[1] += 1
        try:
            async with entry[0]:
                yield
        finally:
            entry[1] -= 1
            if entry[1] == 0 and self._intake.get(key) is entry:
                del self._intake[key]

    async def _process(self, request: Request) -> None:
        if not request.authorized:
            await self._guarded(request, self._pairing_request(request))
            return
        command = parse_command(request.text)
        if command is not None and command.name in ("steer", "queue") and command.args:
            await self._submit(replace(request, text=command.args), command.name)
            return
        if command is not None:
            await self._guarded(request, self._reply_command(request, command))
            return
        await self._submit(request, self.config.busy_mode)

    async def _guarded(self, request: Request, work) -> None:
        try:
            await work
            self.store.finish(request.post_id)
        except Exception as exc:
            log.warning("Command failed post=%s (%s)", request.post_id[:8], _describe(exc))
            self.store.finish(request.post_id, "failed")

    async def _submit(self, request: Request, mode: str) -> None:
        """Steer the running prompt, interrupt it, or queue a new one (see busy_mode)."""
        session = await self._get_session(request)
        if self._closing:  # close() began while this post was admitted: it starts nothing
            self.store.finish(request.post_id, "interrupted")
            return
        if session is None:
            self.store.finish(request.post_id, "rejected")
            await self._safe_reply(request, BUSY)
            return
        mine = session.running is not None and session.owner == request.user_id and not session.stopping
        if mine and mode == "steer" and await self._steer(session, request):
            return
        if mine and mode == "interrupt":
            await self._stop(session, "interrupt")
        if session.pending >= self.config.max_queue_per_session + 1:
            self.store.finish(request.post_id, "rejected")
            await self._safe_reply(request, BUSY)
            return
        session.pending += 1
        self._spawn(self._answer(session, request))

    def _spawn(self, coroutine) -> asyncio.Task:
        """An admitted request (drain() waits for these)."""
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _helper(self, coroutine) -> asyncio.Task:
        """Background work for a request (progress, stop fallback); close() cancels it."""
        task = asyncio.create_task(coroutine)
        self._helpers.add(task)
        task.add_done_callback(self._helpers.discard)
        return task

    async def _get_session(self, request: Request) -> Session | None:
        key = request.session_key
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
            result = Session(key, work, session_dir, session_dir / "request-context.json",
                             request.channel_id, request.channel_type, request.channel_name)
            self.sessions[key] = result
            return result

    # -- steering and stopping ----------------------------------------------------

    async def _steer(self, session: Session, request: Request) -> bool:
        """Inject a message into the owner's running prompt; False when it ended meanwhile."""
        rpc, task = session.rpc, session.running
        if rpc is None or task is None or not rpc.steerable:
            return False
        try:
            text, images, _ = await self._compose(session, request, rpc, history=False)
        except Exception as exc:
            log.warning("Could not steer post=%s (%s)", request.post_id[:8], _describe(exc))
            return False
        if (session.running is not task or session.rpc is not rpc or session.owner != request.user_id
                or session.stopping):
            return False  # that run ended while the attachments downloaded: queue instead
        # Mapped before sending: the run may finish (and deliver) before steer() returns.
        index = rpc.next_steer_index
        session.requests[index] = request
        try:
            sent = await rpc.steer(text, images)
        except Exception as exc:
            log.warning("Could not steer post=%s (%s)", request.post_id[:8], _describe(exc))
            sent = None
        if sent is None:
            if session.requests.get(index) is request:
                del session.requests[index]
            return False
        try:
            await self.client.react(self.bot_id, request.post_id, STEER_EMOJI)
        except Exception as exc:
            log.info("Could not react to a steered post=%s (%s)", request.post_id[:8], _describe(exc))
        return True

    async def _stop(self, session: Session, reason: str) -> bool:
        """Stop the running request: an abort keeps the child, a cancel is the fallback.

        A request that has not submitted its prompt yet never does (_perform checks
        `stopping` before the turn, and abort_run() covers a turn that has begun)."""
        task = session.running or session.slot_task
        if task is None or session.stopping:
            return False
        session.stopping = reason
        rpc = session.rpc
        if session.running is None or not session.turning or rpc is None or not rpc.alive:
            task.cancel()  # nothing runs in Aelix yet: stop the preparations at once
            return True
        try:
            await rpc.abort_run()
        except Exception:
            task.cancel()
            return True
        self._helper(self._stop_fallback(session, task))
        return True

    async def _stop_fallback(self, session: Session, task: asyncio.Task) -> None:
        deadline = time.monotonic() + STOP_GRACE
        while session.running is task and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        if session.running is task:
            task.cancel()

    # -- commands -------------------------------------------------------------------

    def authorized(self, user_id: str) -> bool:
        config = self.config
        return (config.allow_all_users or user_id in config.allowed_users or config.is_admin(user_id)
                or self.store.is_paired(user_id))

    async def channel_kind(self, channel_id: str) -> tuple[str, str]:
        """(type, display name) of a channel, cached."""
        known = self._channels.get(channel_id)
        if known is None:
            channel = await self.client.channel(channel_id)
            kind, name = channel.get("type"), channel.get("display_name")
            if kind not in {"D", "G", "O", "P"}:
                raise MattermostError("Unknown channel type")
            known = self._channels[channel_id] = (kind, name if isinstance(name, str) else "")
        return known

    async def stop_in_channel(self, channel_id: str, user_id: str) -> str:
        """Stop every request of a user running in a channel (a slash command outside threads)."""
        stopped = 0
        for session in tuple(self.sessions.values()):
            if session.channel_id != channel_id:
                continue
            if session.owner == user_id and not session.stopping:
                stopped += await self._stop(session, "cancel")
            stopped += self._drop_waiting(session, user_id)
        return f"내 요청 {stopped}개를 중단합니다." if stopped else "이 채널에서 중단할 내 실행 요청이 없습니다."

    async def _reply_command(self, request: Request, command: Command) -> None:
        text = await self.command(request, command)
        if text:
            await self._safe_reply(request, text)

    async def command(self, request: Request, command: Command, slash: bool = False) -> str:
        """Run a command; returns what to tell its sender (also used by /slash commands)."""
        name, args = command.name, command.args
        session = self.sessions.get(request.session_key)
        if name == "help":
            trigger = self.config.slash_trigger if self.config.slash_listen else None
            return help_text(self.bot_username, self.config.busy_mode, trigger, self.config.is_admin(request.user_id))
        if name == "stop":
            # The running request (if it is the sender's) and the sender's queued ones.
            stopped = (session is not None and session.owner == request.user_id
                       and await self._stop(session, "cancel"))
            dropped = self._drop_waiting(session, request.user_id) if session is not None else 0
            if not stopped and not dropped:
                return "이 대화에 취소할 내 실행 요청이 없습니다."
            return "" if not slash else "내 요청을 중단합니다."
        if name in ("steer", "queue"):
            return f"`!{name} 내용` 형식으로 보내주세요." if not slash else (
                f"`/{self.config.slash_trigger} {name}`은 지원하지 않습니다. 메시지로 `!{name} 내용`을 보내주세요.")
        if name == "new":
            return await self._reset(request, session)
        if name == "tools":
            return self._tools_text(request)
        if name == "pair":
            return await self._pair_command(request, args)
        if name == "status":
            return await self._status(request, session)
        if name == "model":
            return await self._model(request, session, args)
        if name == "usage":
            return await self._usage(request, session)
        if name == "compact":
            return await self._compact(request, session)
        return ""

    async def _reset(self, request: Request, session: Session | None) -> str:
        # Reset is not deletion: retained transcripts remain available to the operator.
        if session is not None:
            if session.pending:
                return "요청 완료 또는 취소 후 다시 초기화해주세요."
            async with session.lock:
                if session.rpc is not None:
                    await session.rpc.close()
                    session.rpc = None
                self.store.reset_session(request.session_key)
        else:
            self.store.reset_session(request.session_key)
        # Saved attachments and unsent outbox files belong to the old context.
        await asyncio.to_thread(attachments.purge, (self.config.work_dir / request.session_key).resolve())
        return "이 대화의 모델 컨텍스트를 초기화했습니다."

    def _tools_text(self, request: Request) -> str:
        tools = self._tools(request.channel_id)
        if not tools:
            return ("이 대화에서는 도구를 사용할 수 없습니다. 관리자가 `aelix.allowed_tools`(또는 채널별 "
                    "`allowed_tools`)에 허용할 도구를 지정할 수 있습니다.")
        return (f"이 대화에서 사용할 수 있는 도구: {', '.join(f'`{x}`' for x in tools)}\n"
                f"메시지당 도구 호출은 최대 {self.config.max_tool_calls}번입니다.")

    def _tools(self, channel_id: str) -> tuple[str, ...]:
        return self.config.tools_for(channel_id)

    def _desired_model(self, key: str) -> str | None:
        """The conversation's chosen model while aelix.models still offers it, else the default."""
        chosen = self.store.session_model(key)
        return chosen if chosen and chosen in self.config.models else self.config.model

    async def _live_child(self, request: Request, session: Session | None) -> RpcProcess | None:
        """The conversation's Aelix child, started on its stored transcript when it has one.

        Never waits for a request of the conversation (that would hold up a later !stop):
        RpcError when one is queued or running and the child is not up."""
        if session is not None and session.rpc is not None and session.rpc.alive:
            return session.rpc
        if self.store.session_file(request.session_key) is None:
            return None
        if session is not None and (session.pending or session.lock.locked()):
            raise RpcError("the conversation is busy")
        session = await self._get_session(request)
        if session is None:
            return None
        async with session.lock:
            try:
                return await self._child(session)
            finally:
                session.last_used = time.monotonic()

    async def _status(self, request: Request, session: Session | None) -> str:
        lines = ["**이 대화의 상태**"]
        if session is not None and session.running is not None:
            rpc = session.rpc
            activity = rpc.activity if rpc is not None else Activity()
            lines.append(f"- 실행 중: {_PHASES.get(activity.phase, activity.phase)} "
                         f"({time.monotonic() - session.started:.0f}초)")
        elif session is not None and session.slot_task is not None:
            lines.append("- 실행 순서를 기다리는 중 (동시 실행 한도)")
        else:
            lines.append("- 실행 중인 요청 없음")
        if session is not None:
            lines.append(f"- 대기 중인 요청: {max(0, session.pending - (session.running is not None))}개")
        rpc = session.rpc if session is not None else None
        live = rpc is not None and rpc.alive
        model = (rpc.model_name if live and rpc is not None else "") or self._desired_model(request.session_key)
        lines.append(f"- 모델: `{model or '기본값'}`")
        tools = self._tools(request.channel_id)
        lines.append(f"- 도구: {', '.join(f'`{x}`' for x in tools) if tools else '없음'}")
        lines.append(f"- 실행 중 새 메시지: {self.config.busy_mode}")
        if live and rpc is not None:
            try:
                usage = (await rpc.stats()).get("contextUsage")
            except RpcError:
                usage = None
            if isinstance(usage, dict) and isinstance(usage.get("percent"), (int, float)):
                lines.append(f"- 컨텍스트 사용량: {usage['percent']:.0f}%")
        elif self.store.session_file(request.session_key) is None:
            lines.append("- 저장된 대화 없음")
        return "\n".join(lines)

    async def _model(self, request: Request, session: Session | None, args: str) -> str:
        choices = list(dict.fromkeys([*([self.config.model] if self.config.model else []), *self.config.models]))
        current = self._desired_model(request.session_key)
        if not args:
            lines = [f"현재 모델: `{current or '기본값'}`"]
            if self.config.models:
                lines.append("선택할 수 있는 모델 (`!model 번호` 또는 `!model 이름`, `!model default`: 기본값):")
                lines += [f"{i}. `{name}`" for i, name in enumerate(choices, 1)]
            else:
                lines.append("관리자가 `aelix.models`를 설정하지 않아 모델을 바꿀 수 없습니다.")
            return "\n".join(lines)
        if not self.config.models:
            return "관리자가 `aelix.models`를 설정하지 않아 모델을 바꿀 수 없습니다."
        if args.lower() == "default":
            chosen = None
        elif args.isascii() and args.isdigit() and len(args) < 4 and 1 <= int(args) <= len(choices):
            chosen = choices[int(args) - 1]
        elif args in choices:
            chosen = args
        else:
            return f"`{args[:80]}`은(는) 선택할 수 있는 모델이 아닙니다. `!model`로 목록을 확인하세요."
        self.store.set_session_model(request.session_key, chosen)
        shown = chosen or self.config.model or "기본값"
        if session is not None and session.running is not None:
            return f"모델을 `{shown}`(으)로 바꿨습니다. 실행 중인 요청이 끝난 뒤부터 적용됩니다."
        return f"모델을 `{shown}`(으)로 바꿨습니다."

    async def _usage(self, request: Request, session: Session | None) -> str:
        try:
            rpc = await self._live_child(request, session)
            if rpc is None:
                return "이 대화에는 아직 사용량이 없습니다."
            stats = await rpc.stats()
        except RpcError:
            if session is not None and (session.pending or session.lock.locked()):
                return "요청이 끝난 뒤 다시 확인해주세요."
            return "사용량을 가져오지 못했습니다."
        tokens = stats.get("tokens") if isinstance(stats.get("tokens"), dict) else {}
        lines = ["**이 대화의 사용량**"]

        def count(name: str) -> str:
            value = tokens.get(name)
            return f"{value:,}" if isinstance(value, int) else "?"

        lines.append(f"- 토큰: 입력 {count('input')} · 출력 {count('output')} · 캐시 읽기 {count('cacheRead')}"
                     f" · 합계 {count('total')}")
        cost = stats.get("cost")
        if isinstance(cost, (int, float)):
            lines.append(f"- 비용: ${cost:.4f}")
        usage = stats.get("contextUsage")
        if isinstance(usage, dict) and isinstance(usage.get("percent"), (int, float)):
            window = usage.get("contextWindow")
            lines.append(f"- 컨텍스트: {usage['percent']:.0f}%" + (f" / {window:,} 토큰" if isinstance(window, int) else ""))
        messages = stats.get("userMessages")
        if isinstance(messages, int):
            lines.append(f"- 질문 수: {messages}")
        return "\n".join(lines)

    async def _compact(self, request: Request, session: Session | None) -> str:
        if self.store.session_file(request.session_key) is None:
            return "압축할 대화가 없습니다."
        if session is not None and (session.running is not None or session.pending):
            return "요청이 끝난 뒤 다시 시도해주세요."
        session = await self._get_session(request)
        if session is None:
            return BUSY
        async with session.lock:
            try:
                rpc = await self._child(session)
                result = await rpc.compact()
            except NothingToCompact:
                return "아직 압축할 만큼 대화가 길지 않습니다."
            except RpcError as exc:
                log.warning("Compaction failed session=%s (%s)", session.key[:8], _describe(exc))
                return "대화 컨텍스트를 압축하지 못했습니다."
            finally:
                session.last_used = time.monotonic()
        before = result.get("tokens_before", result.get("tokensBefore"))
        return "대화 컨텍스트를 압축했습니다." + (f" (압축 전 {before:,} 토큰)" if isinstance(before, int) else "")

    # -- pairing ------------------------------------------------------------------

    async def _pairing_request(self, request: Request) -> None:
        if self.store.pairing_blocked(request.user_id):
            return  # denied recently: no new code, no new notice
        now = time.time()
        if now - self._pair_replies.get(request.user_id, -float("inf")) < pairing.REPLY_INTERVAL:
            return
        self._pair_replies[request.user_id] = now
        existing = self.store.pairing_for(request.user_id)
        if existing is not None:
            await self._safe_reply(request, pairing.waiting_message(existing[0]))
            return
        code = pairing.new_code()
        if not self.store.add_pairing(code, request.user_id, request.channel_id, pairing.CODE_TTL,
                                      pairing.MAX_PENDING):
            await self._safe_reply(request, pairing.FULL)
            return
        log.info("Pairing requested by user=%s", request.user_id[:8])
        await self._safe_reply(request, pairing.request_message(code, int(pairing.CODE_TTL // 60)))
        username = (await self._usernames([request.user_id])).get(request.user_id, request.user_id)
        for admin in self.config.admins:
            try:
                channel = await self.client.direct_channel(self.bot_id, admin)
                await self.client.post(channel, "", pairing.admin_notice(username, code))
            except Exception as exc:
                log.warning("Could not notify an admin of a pairing request (%s)", _describe(exc))

    async def _pair_command(self, request: Request, args: str) -> str:
        if not self.config.is_admin(request.user_id):
            return "관리자만 사용할 수 있는 명령입니다."
        if not request.is_dm:
            return "승인 관련 명령은 봇과의 DM에서만 사용할 수 있습니다."
        action, _, value = args.partition(" ")
        action, value = action.lower(), value.strip()
        if action in ("", "list"):
            pending = self.store.pending_pairings()
            paired = self.store.paired_users()
            names = await self._usernames([x[1] for x in pending] + [x[0] for x in paired])
            lines = ["**승인 대기**"] + ([f"- `{pairing.shown(code)}` @{names.get(user, user)} "
                                         f"({max(0, int((expires - time.time()) // 60))}분 남음)"
                                         for code, user, _, expires in pending] or ["- 없음"])
            lines += ["**페어링으로 승인된 사용자**"] + ([f"- @{names.get(user, user)}" for user, _, _ in paired]
                                                  or ["- 없음"])
            return "\n".join(lines)
        if action in ("approve", "deny"):
            taken = self.store.take_pairing(pairing.normalize(value))
            if taken is None:
                return "유효한 승인 코드가 아닙니다 (만료되었거나 이미 처리됨)."
            user, channel = taken
            if action == "deny":
                self.store.block_pairing(user, pairing.DENY_BLOCK)
                return f"요청을 거절했습니다. 이 사용자는 {int(pairing.DENY_BLOCK // 3600)}시간 동안 새 코드를 받지 않습니다."
            self.store.pair(user, request.user_id)
            self._pair_replies.pop(user, None)
            try:
                await self.client.post(channel, "", pairing.APPROVED)
            except Exception as exc:
                log.warning("Could not tell a paired user (%s)", _describe(exc))
            name = (await self._usernames([user])).get(user, user)
            return f"@{name}의 봇 사용을 승인했습니다."
        if action == "revoke" and value:
            user = await self._user_id(value)
            if user is None or not self.store.unpair(user):
                return "페어링으로 승인된 사용자가 아닙니다. (설정 파일의 allowed_users는 설정에서 제거하세요.)"
            return "승인을 취소했습니다."
        return "사용법: `!pair` · `!pair approve 코드` · `!pair deny 코드` · `!pair revoke @사용자`"

    async def _user_id(self, value: str) -> str | None:
        """A user id from "@username", "username" or the id itself; names are looked up on
        the server every time (a username can move to another account)."""
        name = value.removeprefix("@").strip().lower()
        if self.store.is_paired(name):
            return name
        if not name or not all(c.isascii() and (c.isalnum() or c in "._-") for c in name):
            return None
        try:
            user = await self.client.api("GET", f"users/username/{name}")
        except MattermostError:
            return None
        user_id = user.get("id")
        return user_id if isinstance(user_id, str) else None

    async def _usernames(self, user_ids: list[str]) -> dict[str, str]:
        missing = [x for x in dict.fromkeys(user_ids) if x not in self._names]
        if missing:
            try:
                self._names.update(await self.client.usernames(missing))
            except Exception as exc:
                log.info("Could not look up usernames (%s)", _describe(exc))
        return {x: self._names[x] for x in user_ids if x in self._names}

    # -- one request ------------------------------------------------------------

    async def _answer(self, session: Session, request: Request) -> None:
        task = asyncio.current_task()
        assert task is not None
        session.waiting[request.post_id] = (task, request)
        try:
            try:
                await session.lock.acquire()
            except asyncio.CancelledError:
                if request.post_id in session.dropped and not self._closing:
                    task.uncancel()  # !stop by its sender: it never ran
                    self.store.finish(request.post_id, "cancelled")
                    await self._safe_reply(request, CANCELLED)
                    return
                self._unfinished(request, "interrupted")  # shutdown while it waited
                raise
            finally:
                session.waiting.pop(request.post_id, None)
                session.dropped.discard(request.post_id)
            try:
                await self._perform(session, request)
            finally:
                session.lock.release()
        finally:
            session.pending -= 1
            session.last_used = time.monotonic()

    def _drop_waiting(self, session: Session, user_id: str) -> int:
        """Cancel a user's requests that still wait for their turn in a conversation."""
        dropped = 0
        for post_id, (task, request) in tuple(session.waiting.items()):
            if request.user_id == user_id and post_id not in session.dropped:
                session.dropped.add(post_id)
                task.cancel()
                dropped += 1
        return dropped

    async def _perform(self, session: Session, request: Request) -> None:
        """Run one prompt (with the messages steered into it); answers and every final
        notice are new thread posts."""
        placeholder: str | None = None
        running: RpcProcess | None = None  # set once the turn has begun
        session.requests = {0: request}
        try:
            try:
                session.owner, session.stopping = request.user_id, None
                session.slot_task = asyncio.current_task()  # !stop can cancel it while it waits
                async with self._slots:
                    session.slot_task = None
                    session.running, session.started = asyncio.current_task(), time.monotonic()
                    write_context(session.context_file, request.context(self.config.url))
                    placeholder = await self._placeholder(request)
                    rpc = await self._child(session)
                    text, images, newest = await self._compose(session, request, rpc, history=True)
                    before = await attachments.outbox_snapshot(session.work_dir)
                    if session.stopping:  # stopped before the prompt was sent: it never is
                        raise RpcRunFailed("The request was stopped before it started")
                    running, session.turning = rpc, True
                    progress = self._helper(self._progress(session, request, placeholder, rpc))
                    try:
                        turn = await rpc.turn(text, images)
                    finally:
                        session.turning = False
                        progress.cancel()
                        await asyncio.gather(progress, return_exceptions=True)
                    assert rpc.session_file is not None
                    self.store.save_session(session.key, rpc.session_file)
                    self._saw(session, request, newest)
                    self._run_ended(session)  # answered: nothing is left to cancel
            except asyncio.CancelledError:
                if not session.stopping or self._closing:
                    raise  # shutdown: close() tells the requester
                task = asyncio.current_task()
                if task is not None:
                    task.uncancel()  # handled: the requester asked for it
                status, notice = "cancelled", self._stopped_notice(session)
                answers = running.answers if running is not None else []
            except Exception as exc:
                self._report(session, request, exc)
                stopped = session.stopping is not None
                status = "cancelled" if stopped else "failed"
                notice = (self._stopped_notice(session) if stopped else
                          TIMED_OUT if isinstance(exc, RpcTimeout) else FAILED)
                # Answers Aelix finished before the failure are still delivered.
                answers = (exc.answers if isinstance(exc, RpcRunFailed) else
                           running.answers if running is not None else [])
            else:
                await self._deliver_turn(session, request, placeholder, turn, before)
                return
            self._run_ended(session)  # a late !stop must not cancel the final notice
            if answers and running is not None and running.session_file is not None:
                self.store.save_session(session.key, running.session_file)
            await self._deliver_answers(session, answers)
            await self._conclude(request, placeholder, notice)
            for other in session.requests.values():
                self.store.finish(other.post_id, status)
        except asyncio.CancelledError:
            # Shutdown: close() (or the next start) tells the requester.
            for other in session.requests.values():
                self.store.finish(other.post_id, "interrupted")
            raise
        finally:
            session.owner = session.running = session.slot_task = None
            session.stopping = None  # a later shutdown is not this requester's cancel
            session.requests = {}

    @staticmethod
    def _stopped_notice(session: Session) -> str:
        return INTERRUPTED if session.stopping == "interrupt" else CANCELLED

    @staticmethod
    def _run_ended(session: Session) -> None:
        session.running, session.last_used = None, time.monotonic()

    def _saw(self, session: Session, request: Request, newest: int) -> None:
        """The thread position the conversation has read up to: the prompt (posts after it,
        steered ones and the bot's answers included, are sorted out by _history next time)."""
        self.store.set_last_seen(session.key, max(newest, request.create_at) or _now_ms())

    async def _compose(self, session: Session, request: Request, rpc: RpcProcess,
                       history: bool) -> tuple[str, list[dict] | None, int]:
        """The prompt text and images of a request, and the newest thread post it includes."""
        block, newest = (await self._history(session, request)) if history else ("", 0)
        vision = rpc.vision
        inbound = await attachments.fetch(self.client, self.config, request.post_id, request.file_ids,
                                          request.files, session.work_dir, vision,
                                          save=bool(self._tools(session.channel_id)))
        shared = self.config.session_scope == "thread" and not request.is_dm
        sender = (await self._usernames([request.user_id])).get(request.user_id, "") if (
            shared or block or inbound.attachments or inbound.skipped) else ""
        text = turn_text(request.text, sender, shared, block, attachments_block(inbound, vision))
        return text, inbound.images or None, newest

    async def _history(self, session: Session, request: Request) -> tuple[str, int]:
        """Thread posts this conversation has not seen, quoted; and the newest one's time.

        Only posts by people allowed to use the bot and by the bot itself are quoted:
        integrations, other bots and other members could otherwise steer a model with tools.
        The conversation's own requests and the bot's posts for it are already known to it."""
        limit = self.config.thread_history_posts
        if request.is_dm or not request.in_thread or limit == 0:
            return "", 0
        fresh = self.store.session_file(session.key) is None  # the model knows nothing yet
        seen = None if fresh else self.store.last_seen(session.key)
        if not fresh and seen is None:
            return "", 0  # a conversation from before 0.3.0: what it saw is unknown
        try:
            posts = await self.client.thread(request.root_id)
        except Exception as exc:
            log.warning("Could not read thread history post=%s (%s)", request.post_id[:8], _describe(exc))
            return "", 0
        candidates, newest = [], 0
        for post in posts:
            at = post.get("create_at") if isinstance(post.get("create_at"), int) else 0
            message, author, post_id = post.get("message"), post.get("user_id"), post.get("id")
            if (post_id == request.post_id or post.get("type") or post.get("delete_at")
                    or not isinstance(message, str) or not isinstance(author, str) or not isinstance(post_id, str)):
                continue
            if request.create_at and at >= request.create_at:
                continue  # newer than the request (or the request itself)
            if seen is not None and at <= seen:
                continue
            newest = max(newest, at)
            props = post.get("props") if isinstance(post.get("props"), dict) else {}
            if any(props.get(name) in ("true", True) for name in INTEGRATION_PROPS):
                continue
            own = author == self.bot_id
            if own and (message in _NOTICES or message.endswith(PROGRESS_MARK + ")")):
                continue
            if not own and (props.get("from_bot") in ("true", True) or not self.authorized(author)):
                continue
            files = post.get("file_ids")
            candidates.append((post_id, at, author, own, message, len(files) if isinstance(files, list) else 0))
        known = self.store.session_posts(session.key, [x[0] for x in candidates])
        chosen = [x[1:] for x in candidates if x[0] not in known]
        kept, budget = [], self.config.thread_history_chars
        for item in reversed(chosen):
            if len(kept) >= limit or budget <= 0:
                break
            text = item[3] if len(item[3]) <= budget else item[3][:budget] + " …"
            budget -= len(text)
            kept.append((*item[:3], text, item[4]))
        kept.reverse()
        names = await self._usernames([x[1] for x in kept])
        history = [HistoryPost(at, names.get(author, author), own, text, files)
                   for at, author, own, text, files in kept]
        return history_block(history, len(chosen) - len(kept)), newest

    async def _progress(self, session: Session, request: Request, placeholder: str, rpc: RpcProcess) -> None:
        """Keep the placeholder showing what the run does, and the typing indicator alive."""
        if self.config.progress == "off":
            return
        shown, typed = PREPARING, -float("inf")
        while True:
            now = time.monotonic()
            if now - typed >= TYPING_INTERVAL:
                typed = now
                try:
                    await self.client.typing(self.bot_id, request.channel_id,
                                             "" if request.is_dm and not request.in_thread else request.root_id)
                except Exception as exc:
                    log.debug("Typing indicator failed (%s)", _describe(exc))
            await asyncio.sleep(min(self.config.progress_interval, TYPING_INTERVAL))
            if time.monotonic() - session.started < self.config.progress_interval:
                continue
            text = self._progress_text(session, rpc)
            if text != shown:
                try:
                    await self.client.patch(placeholder, text)
                    shown = text
                except Exception as exc:
                    log.info("Could not update progress post=%s (%s)", request.post_id[:8], _describe(exc))

    def _progress_text(self, session: Session, rpc: RpcProcess) -> str:
        activity = rpc.activity
        label = _PHASES.get(activity.phase, _PHASES["thinking"])
        if activity.phase == "tool" and activity.tool:
            label += f": `{activity.tool[:60]}`"
        if activity.tools_used:
            label += f" · 도구 호출 {activity.tools_used}번"
        elapsed = int(time.monotonic() - session.started)
        text = f"{label}… ({elapsed // 10 * 10}초 경과 · {PROGRESS_MARK})"
        if self.config.progress == "stream":
            partial = rpc.partial().strip()
            if partial:
                room = self.config.max_post_chars - len(text) - 10
                tail = partial if len(partial) <= room else "…" + partial[-room:]
                text = f"{tail}\n\n{text}"
        return text

    async def _child(self, session: Session) -> RpcProcess:
        """The session's live Aelix child, started or resumed from its transcript on demand.

        A child whose model differs from the session's chosen model is replaced: the
        transcript carries the conversation over."""
        model = self._desired_model(session.key)
        if session.rpc is not None:
            if session.rpc.alive and session.rpc_model == model:
                return session.rpc
            await session.rpc.close()  # a dead child may still hold its process
            session.rpc = None
        if session.retired is not None:  # Aelix refuses a transcript a live process owns
            await session.retired.close()
            session.retired = None
        await self._make_room(session)
        tools = self._tools(session.channel_id)
        place = session.place
        own = self.config.channel(session.channel_id)
        place = replace(place, free_response=not self.config.mention_required(session.channel_id),
                        prompt=own.prompt if session.channel_type != "D" else "")
        prompt_file = session.session_dir / "system-prompt.md"
        write_text(prompt_file, system_prompt(self.config, self.bot_username, place, tools))
        session.rpc = RpcProcess(self.config, session.work_dir, session.session_dir,
                                 session.context_file, self._transcript(session), tools=tools,
                                 model=model, prompt_file=prompt_file,
                                 track_partial=self.config.progress == "stream")
        session.rpc_model = model
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
        if session.stopping is not None and isinstance(exc, RpcRunFailed):
            log.info("Request stopped post=%s session=%s (%s)", request.post_id[:8], session.key[:8],
                     session.stopping)
            return
        log.warning("Request failed post=%s session=%s (%s)",
                    request.post_id[:8], session.key[:8], _describe(exc))
        if self.config.log_aelix_stderr and session.rpc is not None:
            tail = session.rpc.stderr_tail()  # redacted by RpcProcess
            if tail:
                log.warning("Aelix stderr post=%s session=%s:\n%s", request.post_id[:8], session.key[:8], tail)

    # -- delivery -----------------------------------------------------------------

    async def _post(self, request: Request, text: str, file_ids: list[str] | None = None) -> dict:
        """A new post in the request's thread, remembered as the conversation's own."""
        # Without files, the call stays the three-argument form older callers patch.
        post = await self.client.post(request.channel_id, request.root_id, text, *([file_ids] if file_ids else []))
        if isinstance(post.get("id"), str) and post["id"]:
            self.store.add_answer(post["id"], request.session_key)
        return post

    async def _placeholder(self, request: Request) -> str:
        post = await self._post(request, PREPARING)
        placeholder = post.get("id")
        if not isinstance(placeholder, str) or not placeholder:
            raise MattermostError("Mattermost did not return a placeholder post ID")
        self._placeholders[request.post_id] = placeholder
        self.store.set_placeholder(request.post_id, placeholder)
        return placeholder

    async def _deliver_turn(self, session: Session, request: Request, placeholder: str, turn: Turn,
                            before: dict) -> None:
        """Each answer goes to its own request's thread; the last one carries outbox files.
        A request merged into a later answer gets no answer of its own."""
        outgoing = await attachments.outbox_changes(session.work_dir, before, self.config)
        file_ids, notes = await attachments.upload(self.client, request.channel_id, session.work_dir,
                                                   outgoing.files, self.config)
        notes = outgoing.skipped + notes
        answers = turn.answers or [(0, "")]
        statuses: dict[int, str] = {}
        for position, (index, text) in enumerate(answers):
            target = session.requests.get(index, request)
            last = position == len(answers) - 1
            body = text + ("\n\n" + "\n".join(f"- {x}" for x in notes) if last and notes else "")
            statuses[index] = await self._post_answer(target, body, file_ids if last else [])
        failed = any(x != "done" for x in statuses.values())
        answered_here = 0 in statuses or any(
            session.requests.get(i, request).root_id == request.root_id for i in statuses)
        if failed:
            delivered = any(x != "failed" for x in statuses.values())
            await self._conclude(request, placeholder, PARTIAL if delivered else FAILED)
        elif answered_here:
            await self._retire(request, placeholder, DELIVERED)
        else:
            await self._retire(request, placeholder, MERGED, delete=False)
        for index, other in session.requests.items():
            status = statuses.get(index, "failed" if failed else "done")
            self.store.finish(other.post_id, "done" if status == "done" else "failed")

    async def _deliver_answers(self, session: Session, answers: list[tuple[int, str]]) -> None:
        """Answers Aelix completed before a run failed or was stopped."""
        for index, text in answers:
            target = session.requests.get(index)
            if target is not None:
                await self._post_answer(target, text, [])

    async def _post_answer(self, request: Request, answer: str, file_ids: list[str]) -> str:
        """Post an answer as new thread posts, which notify (edits do not); files go with the
        last chunk, at most FILES_PER_POST per post. A failed chunk stops the answer.

        Returns "done", "partial" (some posts went out) or "failed"."""
        chunks = split_message(answer, self.config.max_post_chars) or [EMPTY]
        if not answer.strip() and file_ids:
            chunks = ["파일을 첨부했습니다."]
        groups = [file_ids[i:i + attachments.FILES_PER_POST]
                  for i in range(0, len(file_ids), attachments.FILES_PER_POST)]
        posts = [(chunk, groups[0] if groups and i == len(chunks) - 1 else []) for i, chunk in enumerate(chunks)]
        posts += [(f"첨부파일 ({n + 2}/{len(groups)})", group) for n, group in enumerate(groups[1:])]
        for index, (chunk, files) in enumerate(posts):
            try:
                await self._post(request, chunk, files)
            except Exception as exc:
                log.warning("Could not deliver an answer post=%s chunk=%d/%d (%s)",
                            request.post_id[:8], index + 1, len(posts), _describe(exc))
                return "partial" if index else "failed"
        return "done"

    async def _conclude(self, request: Request, placeholder: str | None, notice: str) -> None:
        """Post a final notice as a new thread post and remove the placeholder; when the
        notice cannot be posted, the placeholder shows it instead."""
        try:
            await self._post(request, notice)
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
            for chunk in split_message(text, self.config.max_post_chars) or [EMPTY]:
                await self._post(request, chunk)
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
        tasks = (*self._tasks, *self._helpers)
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
