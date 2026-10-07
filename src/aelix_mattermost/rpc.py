"""Bounded JSONL client for the real `aelix --mode rpc` CLI."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import subprocess
import uuid
from pathlib import Path
from typing import Any

from .config import Config


class RpcError(RuntimeError):
    pass


class RpcTimeout(RpcError):
    pass


class RpcProcess:
    def __init__(self, config: Config, work_dir: Path, session_dir: Path,
                 context_file: Path, session_file: Path | None = None) -> None:
        self.config = config
        self.work_dir = work_dir
        self.session_dir = session_dir
        self.context_file = context_file
        self.session_file = session_file
        self.process: asyncio.subprocess.Process | None = None
        self._tasks: list[asyncio.Task] = []
        self._pending: dict[str, asyncio.Future] = {}
        self._write_lock = asyncio.Lock()
        self._run_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._close_task: asyncio.Task | None = None
        self._finished: asyncio.Future | None = None
        self._policy_ready = asyncio.Event()
        self._nonce = uuid.uuid4().hex
        self._assistant_text: str | None = None
        self._assistant_error = False
        self._closed = False

    @property
    def alive(self) -> bool:
        return self.process is not None and self.process.returncode is None and not self._closed

    def argv(self) -> list[str]:
        args = [*self.config.command, "--mode", "rpc", "--no-agents", "--no-skills",
                "--no-context-files", "--no-extensions", "--session-dir", str(self.session_dir)]
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

    async def start(self) -> dict:
        env = dict(os.environ)
        # The model's shell/tool environment must never inherit the gateway bot credential.
        env.pop(self.config.token_env, None)
        env.pop("MATTERMOST_TOKEN", None)
        env["AELIX_MATTERMOST_CONTEXT_FILE"] = str(self.context_file)
        env["AELIX_MATTERMOST_ALLOWED_TOOLS"] = json.dumps(self.config.allowed_tools)
        env["AELIX_MATTERMOST_MAX_TOOL_CALLS"] = str(self.config.max_tool_calls)
        env["AELIX_MATTERMOST_POLICY_NONCE"] = self._nonce
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
                    *self.argv(), cwd=self.work_dir, env=env,
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE, limit=1024 * 1024, **options,
                )
                self._tasks = [asyncio.create_task(self._read_stdout()),
                               asyncio.create_task(self._read_stderr()),
                               asyncio.create_task(self._watch_exit())]
            state = await self.request("get_state")
            if self.config.allowed_tools:
                try:
                    await asyncio.wait_for(self._policy_ready.wait(), self.config.rpc_timeout)
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
        if not self.alive or self.process is None or self.process.stdin is None:
            raise RpcError("Aelix RPC process is unavailable")
        identifier = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self._pending[identifier] = future
        packet = {"id": identifier, "type": command, **payload}
        try:
            async with self._write_lock:
                self.process.stdin.write((json.dumps(packet, ensure_ascii=False) + "\n").encode())
                await self.process.stdin.drain()
            response = await asyncio.wait_for(future, self.config.rpc_timeout)
            if response.get("success") is not True:
                raise RpcError(f"Aelix rejected RPC command {command}")
            data = response.get("data", {})
            if not isinstance(data, dict):
                raise RpcError("Aelix returned invalid RPC data")
            return data
        except TimeoutError as exc:
            raise RpcTimeout(f"RPC command timed out: {command}") from exc
        except (BrokenPipeError, ConnectionError) as exc:
            raise RpcError("Aelix RPC pipe closed") from exc
        finally:
            self._pending.pop(identifier, None)

    async def run(self, text: str) -> str:
        async with self._run_lock:
            self._finished = asyncio.get_running_loop().create_future()
            self._assistant_text = None
            self._assistant_error = False
            try:
                async with asyncio.timeout(self.config.run_timeout):
                    await self.request("prompt", message=text)
                    await self._finished
                    if self._assistant_error or self._assistant_text is None:
                        raise RpcError("The current Aelix turn did not finish with an answer")
                    result = await self.request("get_last_assistant_text")
                    answer = result.get("text")
                    if not isinstance(answer, str) or not answer.strip():
                        raise RpcError("Aelix returned an empty final answer")
                    state = await self.request("get_state")
                    self._remember_session(state)
                    limit = self.config.max_output_chars
                    return (answer if len(answer) <= limit
                            else answer[:limit] + "\n\n… 응답 길이 제한으로 일부를 생략했습니다.")
            except TimeoutError as exc:
                await self.close()
                raise RpcTimeout("The Aelix run exceeded its time limit") from exc
            except BaseException:
                # Dispose after errors/cancellation: an uncorrelated late agent_end must
                # never resolve the next turn's waiter on the same child.
                await self.close()
                raise
            finally:
                if self._finished is not None:
                    if not self._finished.done():
                        self._finished.cancel()
                    elif not self._finished.cancelled():
                        self._finished.exception()
                self._finished = None

    async def _read_stdout(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        try:
            while line := await self.process.stdout.readline():
                packet = json.loads(line)
                if not isinstance(packet, dict):
                    raise RpcError("Invalid RPC packet")
                if packet.get("type") == "response":
                    future = self._pending.get(packet.get("id"))
                    if future is not None and not future.done():
                        future.set_result(packet)
                else:
                    self._on_event(packet)
        except (ValueError, TypeError, OSError, RpcError):
            self._fail(RpcError("Malformed or oversized Aelix RPC output"))
        except asyncio.CancelledError:
            raise

    def _on_event(self, event: dict) -> None:
        if self._finished is None:
            return
        if event.get("type") == "message_end":
            message = event.get("message", {})
            if isinstance(message, dict) and message.get("role") == "assistant":
                reason = message.get("stopReason", message.get("stop_reason"))
                if reason in {"error", "aborted"}:
                    self._assistant_error = True
                content = message.get("content", [])
                if isinstance(content, list):
                    text = "".join(x.get("text", "") for x in content
                                   if isinstance(x, dict) and x.get("type") == "text"
                                   and isinstance(x.get("text"), str))
                    self._assistant_text = text or None
        if event.get("type") == "agent_end" and not self._finished.done():
            self._finished.set_result(None)

    async def _read_stderr(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        expected = f"aelix-mattermost-policy-ready:{self._nonce}"
        try:
            while line := await self.process.stderr.readline():
                if line.decode("utf-8", errors="replace").strip() == expected:
                    self._policy_ready.set()
                # Provider errors may contain prompts/credentials; do not log stderr.
        except (ValueError, OSError):
            self._fail(RpcError("Invalid Aelix stderr stream"))

    async def _watch_exit(self) -> None:
        assert self.process is not None
        await self.process.wait()
        # Let already buffered final stdout packets drain before reporting death.
        if self._tasks:
            with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(self._tasks[0]), 0.5)
        self._fail(RpcError("Aelix RPC process exited"))

    def _fail(self, error: RpcError) -> None:
        for future in tuple(self._pending.values()):
            if not future.done():
                future.set_exception(error)
        if self._finished is not None and not self._finished.done():
            self._finished.set_exception(error)

    async def close(self) -> None:
        async with self._close_lock:
            if self._close_task is None:
                self._close_task = asyncio.create_task(self._close())
            cleanup = self._close_task
        # Repeated cancellation of a caller must not abandon process-tree cleanup.
        await asyncio.shield(cleanup)

    async def _close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._fail(RpcError("Aelix RPC process was stopped"))
        process = self.process
        if process is not None:
            if os.name == "posix":
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGTERM)
            elif process.returncode is None:
                if os.name == "nt":
                    killer = await asyncio.create_subprocess_exec(
                        "taskkill", "/PID", str(process.pid), "/T", "/F",
                        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                    )
                    await killer.wait()
                else:
                    process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 2)
            except TimeoutError:
                if process.returncode is None:
                    process.kill()
                await process.wait()
            if os.name == "posix":
                # Also end same-group grandchildren when the parent exits cooperatively.
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
        current = asyncio.current_task()
        tasks = [task for task in self._tasks if task is not current]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
