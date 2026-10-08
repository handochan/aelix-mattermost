"""Bounded JSONL client for the real `aelix --mode rpc` CLI."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import signal
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .config import Config

log = logging.getLogger(__name__)

_CHUNK = 64 * 1024
_EDGE = 4096  # bytes kept from both ends of an oversized line
_STDERR_KEEP = 16 * 1024
_STDERR_TAIL = 8 * 1024
_FAILED = frozenset({"error", "aborted"})
# Responses put "type" first (rpc_types); events are dataclass-serialised, so "type" is last.
_RESPONSE = re.compile(rb'\s*\{\s*"type"\s*:\s*"response"')
_RESPONSE_ID = re.compile(rb'"id"\s*:\s*"([0-9A-Za-z_\-]{1,128})"')
_EVENT = re.compile(rb'"type"\s*:\s*"([0-9A-Za-z_]{1,64})"\s*\}\s*$')
_ROLE = re.compile(rb'"role"\s*:\s*"([0-9A-Za-z_]{1,32})"\s*\}\s*,\s*"type"\s*:\s*"message_end"\s*\}\s*$')
_SECRET = re.compile(
    r"(?P<bearer>\b(?i:bearer)\s+)[^\s\"',;]+"
    r"|\b(?:sk|pk|rk)-[A-Za-z0-9_\-]{8,}|\bxox[a-z]-[A-Za-z0-9\-]{8,}"
    r"|\b(?:gh[pousr]|github_pat)_[A-Za-z0-9_]{8,}|\bglpat-[A-Za-z0-9_\-]{8,}"
    r"|(?P<run>[A-Za-z0-9+/_\-]{32,}=*)"
)


class RpcError(RuntimeError):
    pass


class RpcTimeout(RpcError):
    pass


class RpcRunFailed(RpcError):
    """Aelix finished the prompt without an answer; the idle child stays usable."""


def _group(pattern: re.Pattern[bytes], data: bytes) -> str | None:
    match = pattern.search(data)
    return match.group(1).decode() if match else None


def _load(line: bytearray | None) -> dict | None:
    if line is None:
        return None
    try:
        value = json.loads(line)
    except (ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def _data(command: str, response: dict) -> dict:
    if response.get("success") is not True:
        raise RpcError(f"Aelix rejected RPC command {command}")
    data = response.get("data", {})
    if not isinstance(data, dict):
        raise RpcError("Aelix returned invalid RPC data")
    return data


def _outcome(message: dict) -> tuple[str | None, str]:
    """(stop_reason, text) of an assistant message as Aelix serialises it."""
    reason = message.get("stop_reason", message.get("stopReason"))
    content = message.get("content")
    text = "".join(x["text"] for x in content if isinstance(x, dict) and x.get("type") == "text"
                   and isinstance(x.get("text"), str)) if isinstance(content, list) else ""
    return (reason if isinstance(reason, str) else None), text


def _hide(match: re.Match[str]) -> str:
    if match.group("bearer"):
        return match.group("bearer") + "[redacted]"
    run = match.group("run")
    if run and (not any(c.isdigit() for c in run) or run.count("/") > 3):
        return run  # words and file paths stay readable
    return "[redacted]"


def _redact(text: str, token: str = "") -> str:
    """Mask credentials that provider errors and tracebacks may print."""
    if len(token) >= 8:  # a short placeholder would mask ordinary letters
        text = text.replace(token, "[redacted]")
    return _SECRET.sub(_hide, text)


def _killpg(pid: int, number: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, number)


async def _exited(process: asyncio.subprocess.Process, seconds: float) -> bool:
    """Poll returncode: wait() also waits for pipes that a descendant may hold open."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while process.returncode is None and loop.time() < deadline:
        await asyncio.sleep(0.05)
    return process.returncode is not None


class _Lines:
    """LF-only framing with a per-line cap; oversized lines keep only their edges."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.buffer = bytearray()
        self.head = self.tail = b""
        self.dropping = False

    def feed(self, chunk: bytes) -> Iterator[tuple[bytearray | None, bytes, bytes]]:
        start = 0
        while (end := chunk.find(b"\n", start)) >= 0:
            self._add(chunk[start:end])
            yield self._take()
            start = end + 1
        self._add(chunk[start:])

    def _add(self, piece: bytes) -> None:
        if self.dropping:
            self.tail = (self.tail + piece)[-_EDGE:]
        elif len(self.buffer) + len(piece) > self.limit:
            self.head = bytes(self.buffer[:_EDGE]) + piece[:max(0, _EDGE - len(self.buffer))]
            self.tail = (bytes(self.buffer[-_EDGE:]) + piece)[-_EDGE:]
            self.buffer, self.dropping = bytearray(), True
        else:
            self.buffer += piece

    def _take(self) -> tuple[bytearray | None, bytes, bytes]:
        if self.dropping:
            result = None, self.head, self.tail
            self.head = self.tail = b""
            self.dropping = False
            return result
        line, self.buffer = self.buffer, bytearray()
        return line, bytes(line[:_EDGE]), bytes(line[-_EDGE:])


class RpcProcess:
    max_line = 64 * 1024 * 1024  # longer lines are dropped; their event type still counts
    start_grace = 2.0  # an accepted prompt starts a run (or keeps streaming) by then
    poll_interval = 0.2  # get_state cadence while Aelix retries, recovers or compacts
    abort_grace = 2.0  # abort RPC plus idle wait before the child is signalled
    term_grace = 5.0  # SIGTERM (CTRL_BREAK on Windows) before SIGKILL
    probe_timeout = 2.0  # get_state of idle()

    def __init__(self, config: Config, work_dir: Path, session_dir: Path,
                 context_file: Path, session_file: Path | None = None) -> None:
        self.config = config
        self.work_dir = work_dir
        self.session_dir = session_dir
        self.context_file = context_file
        self.session_file = session_file
        self.process: asyncio.subprocess.Process | None = None
        self.stray_lines = 0
        self.oversized_lines = 0
        self._tasks: list[asyncio.Task] = []
        self._pending: dict[str, asyncio.Future] = {}
        self._write_lock = asyncio.Lock()
        self._run_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._close_task: asyncio.Task | None = None
        self._policy_ready = asyncio.Event()
        self._wake = asyncio.Event()
        self._nonce = uuid.uuid4().hex
        self._failure: RpcError | None = None
        self._reader_done = False
        self._closed = False
        # Per-prompt run tracking, fed by the stdout reader.
        self._started = self._ended = 0
        self._open = False
        self._verdict: tuple[str | None, str] | None = None
        self._busy = False  # a prompt was accepted and Aelix may still be running it
        self._settling = False  # an answer was returned while Aelix may still be busy
        self._stderr = bytearray()
        self._stderr_cut = self._stderr_done = False

    @property
    def alive(self) -> bool:
        return (self.process is not None and self.process.returncode is None
                and not self._closed and self._failure is None)

    async def idle(self) -> bool:
        """Whether stopping this child interrupts nothing: no prompt in flight and no
        compaction still running after an answer. A dead child is idle."""
        if not self.alive:
            return True
        if self._settling and not self._busy:
            try:
                state = _data("get_state", await self._call("get_state", {}, self.probe_timeout))
            except RpcError:
                return not self.alive
            self._settling = state.get("isStreaming") is True
        return not (self._busy or self._settling)

    def argv(self) -> list[str]:
        args = [*self.config.command, "--mode", "rpc", "--no-agents", "--no-skills",
                "--no-context-files", "--no-extensions", "--no-approve",
                "--session-dir", str(self.session_dir)]
        if self.config.offline:
            args.append("--offline")
        if self.config.model:
            args.extend(["--model", self.config.model])
        if self.session_file and self.session_file.is_file():
            args.extend(["--session", str(self.session_file)])
        if not self.config.allowed_tools:
            args.append("--no-tools")
        else:
            args.extend(["--tools", ",".join(self.config.allowed_tools),
                         "-e", str(Path(__file__).with_name("policy.py"))])
            for extension in self.config.extensions:
                args.extend(["-e", extension])
        return args

    def _environment(self) -> dict[str, str]:
        env = dict(os.environ)
        # The model's shell/tool environment must never inherit the gateway bot credential;
        # an inherited delegation depth would make Aelix block every domain tool.
        for name in (self.config.token_env, "MATTERMOST_TOKEN", "AELIX_SUBAGENT_DEPTH"):
            env.pop(name, None)
        # Ambient MCP servers (agent_dir/mcp.json, a work-dir .aelix/mcp.json or an
        # inherited $AELIX_MCP_CONFIG) start only when aelix.mcp_config names a file.
        mcp = getattr(self.config, "mcp_config", None)
        env["AELIX_MCP_CONFIG"] = str(Path(mcp).resolve()) if mcp else os.devnull
        env["AELIX_MATTERMOST_CONTEXT_FILE"] = str(self.context_file)
        env["AELIX_MATTERMOST_ALLOWED_TOOLS"] = json.dumps(self.config.allowed_tools)
        env["AELIX_MATTERMOST_MAX_TOOL_CALLS"] = str(self.config.max_tool_calls)
        env["AELIX_MATTERMOST_POLICY_NONCE"] = self._nonce
        return env

    def _startup_timeout(self) -> float:
        # Never shorter than an ordinary RPC round trip; 60 s when not configured.
        value = getattr(self.config, "startup_timeout", None) or 60.0
        return max(float(value), float(self.config.rpc_timeout))

    async def start(self) -> dict:
        options: dict[str, Any] = {}
        if os.name == "posix":
            options["start_new_session"] = True
        elif os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        try:
            # A concurrent close must wait for ownership of the spawned child.
            async with self._close_lock:
                if self._closed or self._close_task is not None:
                    raise RpcError("Aelix RPC process was stopped")
                self.process = await asyncio.create_subprocess_exec(
                    *self.argv(), cwd=self.work_dir, env=self._environment(),
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE, **options,
                )
                self._tasks = [asyncio.create_task(self._read_stdout()),
                               asyncio.create_task(self._read_stderr()),
                               asyncio.create_task(self._watch_exit())]
            # A cold start can take far longer than one ordinary RPC round trip.
            deadline = asyncio.get_running_loop().time() + self._startup_timeout()
            try:
                async with asyncio.timeout_at(deadline):
                    state = _data("get_state", await self._call("get_state", {}, None))
            except TimeoutError as exc:
                raise RpcTimeout("Aelix did not answer get_state within startup_timeout") from exc
            if self.config.allowed_tools:
                try:
                    async with asyncio.timeout_at(deadline):
                        await self._policy_acknowledged()
                except TimeoutError as exc:
                    raise RpcError("Aelix did not acknowledge the required tool policy") from exc
            self._remember_session(state)
            return state
        except BaseException:
            await self.close()
            raise

    def _remember_session(self, state: dict) -> None:
        filename = state.get("sessionFile")
        if not isinstance(filename, str) or not filename:
            raise RpcError("Aelix did not provide a persistent sessionFile")
        path = Path(filename).resolve()
        if not path.is_relative_to(self.session_dir.resolve()):
            raise RpcError("Aelix session file is outside its assigned session directory")
        self.session_file = path

    async def request(self, command: str, **payload: Any) -> dict:
        return _data(command, await self._call(command, payload, self.config.rpc_timeout))

    async def _call(self, command: str, payload: dict[str, Any], timeout: float | None,
                    force: bool = False) -> dict:
        """Send one command and return its raw response; `force` serves close()."""
        process = self.process
        if (process is None or process.stdin is None or process.returncode is not None
                or self._reader_done or not (force or self.alive)):
            raise RpcError("Aelix RPC process is unavailable")
        identifier = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self._pending[identifier] = future
        packet = {"id": identifier, "type": command, **payload}
        try:
            async with self._write_lock:
                process.stdin.write((json.dumps(packet, ensure_ascii=False) + "\n").encode())
                await process.stdin.drain()
            async with asyncio.timeout(timeout):
                return await future
        except TimeoutError as exc:
            raise RpcTimeout(f"RPC command timed out: {command}") from exc
        except (BrokenPipeError, ConnectionError) as exc:
            raise RpcError("Aelix RPC pipe closed") from exc
        finally:
            self._pending.pop(identifier, None)

    async def run(self, text: str) -> str:
        async with self._run_lock:
            try:
                async with asyncio.timeout(self.config.run_timeout):
                    await self._submit(text)
                    answer = await self._await_answer()
                    state = await self.request("get_state")
                    self._remember_session(state)
            except RpcRunFailed:
                self._busy = False
                raise
            except TimeoutError as exc:
                await self.close()
                raise RpcTimeout("The Aelix run exceeded its time limit") from exc
            except BaseException:
                # Protocol failure, child death or cancellation: abort the run, stop the child.
                await self.close()
                raise
            self._busy = False
            # Threshold compaction runs after the final agent_end; the next prompt waits.
            self._settling = state.get("isStreaming") is True
            limit = self.config.max_output_chars
            return (answer if len(answer) <= limit
                    else answer[:limit] + "\n\n… 응답 길이 제한으로 일부를 생략했습니다.")

    async def _submit(self, text: str) -> None:
        """Send the prompt once Aelix is idle; wait and resend after a busy rejection."""
        while True:
            if self._settling:
                await self._wait_idle()
            self._started = self._ended = 0
            self._open, self._verdict, self._busy = False, None, True
            response = await self._call("prompt", {"message": text}, self.config.rpc_timeout)
            if response.get("success") is True:
                return
            self._busy = False
            if "is busy" not in str(response.get("error", "")):
                raise RpcRunFailed("Aelix rejected the prompt")
            self._settling = True

    async def _wait_idle(self) -> None:
        while (await self.request("get_state")).get("isStreaming") is True:
            await asyncio.sleep(self.poll_interval)
        self._settling = False

    async def _await_answer(self) -> str:
        """Follow the runs of an accepted prompt (retries, overflow recovery) to a verdict."""
        loop = asyncio.get_running_loop()
        grace = loop.time() + self.start_grace
        while True:
            self._wake.clear()
            self._raise_failure()
            finished = self._ended > 0 and not self._open
            if finished and (answer := self._answer()) is not None:
                return answer
            if self._open or (not finished and loop.time() < grace):
                await self._pause(self.poll_interval)
                continue
            # A failed run may still be retried or recovered, and a prompt may still be
            # starting. Once Aelix reports idle, every event of this prompt has arrived.
            if (await self.request("get_state")).get("isStreaming") is not True:
                if not self._open and self._ended > 0 and (answer := self._answer()) is not None:
                    return answer
                raise self._verdict_error()
            await self._pause(self.poll_interval)

    def _answer(self) -> str | None:
        if self._verdict is None:
            return None
        reason, text = self._verdict
        return text.strip() if reason not in _FAILED and text.strip() else None

    def _verdict_error(self) -> RpcRunFailed:
        if self._open or not self._ended:
            return RpcRunFailed("Aelix went idle without finishing the run" if self._started
                              else "Aelix accepted the prompt but did not start a run")
        if self._verdict is None:
            return RpcRunFailed("The Aelix run ended without a readable assistant message")
        if self._verdict[0] in _FAILED:
            return RpcRunFailed(f"The Aelix run ended with stop reason '{self._verdict[0]}'")
        return RpcRunFailed("Aelix returned an empty final answer")

    async def _pause(self, seconds: float) -> None:
        """Sleep until the reader reports progress or `seconds` pass."""
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(seconds):
                await self._wake.wait()

    def _raise_failure(self) -> None:
        if self._failure is not None:
            raise RpcError(str(self._failure))

    async def _policy_acknowledged(self) -> None:
        while True:
            self._wake.clear()
            if self._policy_ready.is_set():
                return
            self._raise_failure()
            await self._pause(self.poll_interval)

    async def _read_stdout(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        frames = _Lines(self.max_line)
        try:
            # read(), never readline(): no separator limit can wedge or kill the stream.
            while chunk := await self.process.stdout.read(_CHUNK):
                for line, head, tail in frames.feed(chunk):
                    if line is None:
                        self.oversized_lines += 1
                        log.warning("Dropped an Aelix RPC line over %d bytes", self.max_line)
                    self._route(line, head, tail)
        except OSError:
            pass
        finally:
            self._reader_done = True
            self._fail(RpcError("Aelix RPC output ended"))

    def _route(self, line: bytearray | None, head: bytes, tail: bytes) -> None:
        """Dispatch one stdout line; `line` is None when it exceeded max_line."""
        if line is not None and len(line) <= _EDGE and not head.strip():
            return
        if _RESPONSE.match(head):
            self._on_response(_load(line), head)
            return
        kind = _group(_EVENT, tail[-256:])
        if kind is None:  # not the dataclass layout: only a full parse can tell
            packet = _load(line)
            kind = packet.get("type") if packet is not None else None
            if kind == "response":
                self._on_response(packet, head)
            elif isinstance(kind, str):
                self._on_event(kind, packet)
            elif line is not None:  # oversized lines were already counted
                self.stray_lines += 1
                if self.stray_lines == 1:
                    log.warning("Ignored non-JSON Aelix stdout; extensions must print to stderr")
            return
        # Only assistant messages matter; message_update and agent_end are never parsed.
        wanted = kind == "turn_end" or (
            kind == "message_end" and _group(_ROLE, tail[-256:]) in (None, "assistant"))
        self._on_event(kind, (_load(line) or {}) if wanted else None)

    def _on_response(self, packet: dict | None, head: bytes) -> None:
        identifier = packet.get("id") if packet is not None else _group(_RESPONSE_ID, head)
        future = self._pending.get(identifier) if isinstance(identifier, str) else None
        if future is None or future.done():
            return
        if packet is None:
            future.set_exception(RpcError("Aelix sent a malformed or oversized RPC response"))
        else:
            future.set_result(packet)

    def _on_event(self, kind: str, packet: dict | None) -> None:
        if kind == "agent_start":
            self._started += 1
            self._open, self._verdict = True, None
        elif kind == "agent_end":
            self._ended += 1
            self._open = False
        elif kind in ("message_end", "turn_end") and packet is not None:
            # turn_end carries the final (hook-replaced) message; message_end the original.
            message = packet.get("message")
            if not isinstance(message, dict):
                self._verdict = None  # oversized or malformed: this turn's outcome is unknown
            elif message.get("role") == "assistant":
                self._verdict = _outcome(message)
        else:
            return
        self._wake.set()

    async def _read_stderr(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        expected = f"aelix-mattermost-policy-ready:{self._nonce}".encode()
        partial = b""
        try:
            while chunk := await self.process.stderr.read(_CHUNK):
                self._stderr += chunk
                if len(self._stderr) > _STDERR_KEEP:
                    del self._stderr[:-_STDERR_KEEP]
                    self._stderr_cut = True
                *lines, partial = (partial + chunk).split(b"\n")
                if any(line.strip() == expected for line in lines):
                    self._policy_ready.set()
                    self._wake.set()
                if len(partial) > len(expected) + 64:
                    partial = b"\0"  # an overlong line can never be the marker
        except OSError:
            pass
        finally:
            self._stderr_done = True

    def stderr_tail(self) -> str:
        """Recent Aelix stderr: complete lines only, redacted, at most 8 KiB."""
        data = bytes(self._stderr)
        if self._stderr_cut:
            data = data.partition(b"\n")[2]
        if not self._stderr_done:
            data = data[:data.rfind(b"\n") + 1]
        text = _redact(data.decode("utf-8", "replace"), self.config.token).strip()
        return text.encode()[-_STDERR_TAIL:].decode("utf-8", "ignore")

    async def _watch_exit(self) -> None:
        assert self.process is not None
        while self.process.returncode is None:  # not wait(): see _exited()
            await asyncio.sleep(0.1)
        # Let already buffered final stdout packets drain before reporting death.
        await asyncio.wait(self._tasks[:1], timeout=0.5)
        self._fail(RpcError("Aelix RPC process exited"))

    def _fail(self, error: RpcError) -> None:
        if self._failure is None:
            self._failure = error
        for future in tuple(self._pending.values()):
            if not future.done():
                future.set_exception(RpcError(str(error)))
        self._wake.set()

    async def close(self) -> None:
        async with self._close_lock:
            if self._close_task is None:
                self._close_task = asyncio.create_task(self._close())
            cleanup = self._close_task
        # Repeated cancellation of a caller must not abandon process-tree cleanup.
        await asyncio.shield(cleanup)

    async def _close(self) -> None:
        active = self._busy or self._settling
        self._closed = True
        self._fail(RpcError("Aelix RPC process was stopped"))
        process = self.process
        if process is not None:
            if active and process.returncode is None and not self._reader_done:
                await self._abort()
            await self._stop(process)
        current = asyncio.current_task()
        tasks = [task for task in self._tasks if task is not current]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _abort(self) -> None:
        """Let Aelix end the run and its tool process trees, which outlive killpg."""
        with contextlib.suppress(RpcError, OSError, TimeoutError):
            async with asyncio.timeout(self.abort_grace):
                await self._call("abort", {}, None, force=True)
                while True:
                    data = (await self._call("get_state", {}, None, force=True)).get("data")
                    if not isinstance(data, dict) or data.get("isStreaming") is not True:
                        return
                    await asyncio.sleep(0.05)

    async def _stop(self, process: asyncio.subprocess.Process) -> None:
        """Close stdin, ask the group to exit, then SIGKILL whatever is left."""
        if process.stdin is not None:
            with contextlib.suppress(OSError, RuntimeError):
                process.stdin.close()
        # POSIX signals go by group id: Popen.terminate()/kill() may reap the child
        # behind asyncio's watcher.
        if os.name == "posix":
            _killpg(process.pid, signal.SIGTERM)
        elif process.returncode is None:
            with contextlib.suppress(OSError, ValueError):
                process.send_signal(getattr(signal, "CTRL_BREAK_EVENT", signal.SIGTERM))
        if not await _exited(process, self.term_grace):
            if os.name == "posix":
                _killpg(process.pid, signal.SIGKILL)
            else:
                if os.name == "nt":
                    killer = await asyncio.create_subprocess_exec(
                        "taskkill", "/PID", str(process.pid), "/T", "/F",
                        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                    )
                    await killer.wait()
                with contextlib.suppress(ProcessLookupError, OSError):
                    process.kill()
            await _exited(process, 5)
        if os.name == "posix":
            # Also end same-group grandchildren when the parent exits cooperatively.
            _killpg(process.pid, signal.SIGKILL)
        # Release our pipe ends even if a descendant in another session holds them.
        transport = getattr(process, "_transport", None)
        if transport is not None:
            with contextlib.suppress(Exception):
                transport.close()
