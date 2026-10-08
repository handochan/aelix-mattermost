import asyncio
import os
import sys
import tempfile
import time
import unittest
from dataclasses import fields, is_dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from aelix_mattermost.config import Config
from aelix_mattermost.rpc import RpcError, RpcProcess, RpcTimeout
from aelix_mattermost.storage import write_context

FAKE = Path(__file__).with_name("fake_aelix.py").resolve()


def settings(base, **values):
    """`base` with overrides, including fields an older Config does not define yet."""
    if not is_dataclass(base):
        return SimpleNamespace(**{**vars(base), **values})
    names = {item.name for item in fields(base)}
    if set(values) <= names:
        return replace(base, **values)
    return SimpleNamespace(**{**{name: getattr(base, name) for name in names}, **values})


def running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class RpcBehaviourTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = Config(url="http://127.0.0.1:8065", token="test-secret", allowed_users=("u1",),
                             allow_insecure_http=True, state_dir=self.root / "state",
                             work_dir=self.root / "work", command=(sys.executable, str(FAKE)),
                             rpc_timeout=2, run_timeout=10).validate()
        self.clients = []

    async def asyncTearDown(self):
        for client in self.clients:
            await client.close()

    def client(self, configuration=None, *flags: str) -> RpcProcess:
        configuration = configuration or self.config
        if flags:
            configuration = settings(configuration, command=(*configuration.command, *flags))
        work, sessions = self.root / "cwd", self.root / f"sessions{len(self.clients)}"
        work.mkdir(exist_ok=True)
        sessions.mkdir()
        context = sessions / "context.json"
        write_context(context, {"server": "s", "post_id": "p", "channel_id": "c", "user_id": "u1",
                                "root_id": "r"})
        client = RpcProcess(configuration, work, sessions, context)
        self.clients.append(client)
        return client

    async def started(self, configuration=None, *flags: str, max_line: int | None = None) -> RpcProcess:
        client = self.client(configuration, *flags)
        if max_line is not None:
            client.max_line = max_line
        await client.start()
        return client

    # F2: multi-run prompts, busy windows and prompts that never start.

    async def test_auto_retry_answer_is_returned(self):
        client = await self.started()
        self.assertEqual(await client.run("__retry__ q"), "turn 1: __retry__ q")
        self.assertEqual(await client.run("next"), "turn 2: next")

    async def test_overflow_recovery_answer_is_returned(self):
        client = await self.started()
        self.assertEqual(await client.run("__overflow__ q"), "turn 1: __overflow__ q")
        self.assertTrue(client.alive)

    async def test_answer_precedes_threshold_compaction_and_next_prompt_waits(self):
        client = await self.started()
        began = time.monotonic()
        self.assertEqual(await client.run("__compact__ q"), "turn 1: __compact__ q")
        self.assertLess(time.monotonic() - began, 0.5)  # not delayed by the 0.6 s compaction
        self.assertEqual(await client.run("follow-up"), "turn 2: follow-up")
        self.assertGreaterEqual(time.monotonic() - began, 0.6)

    async def test_busy_rejection_is_retried_once_idle(self):
        client = await self.started()
        await client.run("__compact__ q")
        client._settling = False  # as if the prompt raced into the compaction window
        self.assertEqual(await client.run("follow-up"), "turn 2: follow-up")

    async def test_error_verdict_keeps_the_idle_child(self):
        client = await self.started()
        with self.assertRaises(RpcError) as caught:
            await client.run("__error__")
        self.assertNotIsInstance(caught.exception, RpcTimeout)
        self.assertNotIn("provider-secret", str(caught.exception))
        self.assertTrue(client.alive)
        self.assertEqual(await client.run("again"), "turn 2: again")

    async def test_accepted_prompt_that_never_starts_fails_fast(self):
        client = await self.started()
        client.start_grace = 0.1
        began = time.monotonic()
        with self.assertRaises(RpcError):
            await client.run("__nostart__")
        self.assertLess(time.monotonic() - began, 2)
        self.assertTrue(client.alive)

    # F5/F18: framing, oversized lines, stray output, reader death.

    async def test_huge_tool_output_and_agent_end_keep_the_answer(self):
        # 3 MiB lines: parsed under the default cap, dropped but still typed under a 1 MiB cap.
        for cap in (RpcProcess.max_line, 1024 * 1024):
            with self.subTest(cap=cap):
                client = await self.started(max_line=cap)
                self.assertEqual(await client.run("__huge__"), "turn 1: __huge__")
                self.assertEqual(client.oversized_lines > 0, cap < RpcProcess.max_line)

    async def test_oversized_response_fails_only_its_request(self):
        client = await self.started(max_line=1024 * 1024)
        await client.run("__huge__")
        began = time.monotonic()
        with self.assertRaises(RpcError):
            await client.request("get_messages")
        self.assertLess(time.monotonic() - began, 1)
        self.assertTrue(client.alive)
        self.assertEqual(await client.run("after"), "turn 2: after")

    async def test_stray_stdout_is_skipped(self):
        # Counted and reported once, never with the text an extension printed.
        with self.assertLogs(level="DEBUG") as logs:
            client = await self.started(None, "--noisy")
            self.assertEqual(await client.run("__malformed__"), "turn 1: __malformed__")
        self.assertEqual(client.stray_lines, 2)
        messages = [record.getMessage() for record in logs.records]
        self.assertEqual(sum("Ignored non-JSON Aelix stdout" in x for x in messages), 1)
        printed = ("domain extension loaded", "connecting to backend", "not JSON")
        self.assertEqual([x for x in messages if any(text in x for text in printed)], [])

    async def test_message_updates_are_not_parsed(self):
        client = await self.started()
        self.assertEqual(await client.run("__stream__"), "turn 1: __stream__")
        self.assertEqual(client.stray_lines, 0)

    async def test_child_exit_or_stdout_end_fails_promptly_and_marks_it_dead(self):
        for text in ("__exit__", "__close_stdout__"):
            with self.subTest(text=text):
                client = await self.started()
                with self.assertRaises(RpcError):
                    await asyncio.wait_for(client.run(text), 2)
                self.assertFalse(client.alive)

    async def test_close_does_not_wait_for_pipes_held_by_descendants(self):
        client = await self.started()
        running_task = asyncio.create_task(client.run("__holder__"))
        holder = client.session_dir / "holder.pid"
        for _ in range(200):
            if holder.exists() and holder.read_text():
                break
            await asyncio.sleep(0.01)
        pid = int(holder.read_text())
        self.addCleanup(lambda: running(pid) and os.kill(pid, 9))
        began = time.monotonic()
        running_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(running_task, 10)
        # Process.wait() would only resolve once the descendant closed the pipes.
        self.assertLess(time.monotonic() - began, 1.5)
        self.assertIsNotNone(client.process.returncode)

    # F9: abort before signals, because tool process trees outlive killpg.

    async def test_cancel_and_timeout_stop_tool_process_trees(self):
        for trigger in ("cancel", "timeout"):
            with self.subTest(trigger=trigger):
                client = await self.started(settings(self.config, run_timeout=1.5))
                pid_file = client.session_dir / "tool.pid"
                running_task = asyncio.create_task(client.run("__tool__"))
                for _ in range(200):
                    if pid_file.exists() and pid_file.read_text():
                        break
                    await asyncio.sleep(0.01)
                pid = int(pid_file.read_text())
                self.addCleanup(lambda pid=pid: running(pid) and os.kill(pid, 9))
                self.assertTrue(running(pid))
                if trigger == "cancel":
                    running_task.cancel()
                with self.assertRaises((asyncio.CancelledError, RpcTimeout)):
                    await running_task
                self.assertFalse(running(pid))
                self.assertIsNotNone(client.process.returncode)

    # F10/F22: child environment, argv and startup budget.

    async def test_child_environment_excludes_ambient_configuration(self):
        inherited = {"MATTERMOST_TOKEN": "test-secret", "AELIX_SUBAGENT_DEPTH": "1",
                     "AELIX_MCP_CONFIG": str(self.root / "ambient-mcp.json")}
        with patch.dict(os.environ, inherited):
            client = await self.started()
        state = await client.request("get_state")
        probe = state["probe"]
        self.assertFalse(state["tokenVisible"])
        self.assertIsNone(probe["subagentDepth"])
        self.assertEqual(probe["mcpConfig"], os.devnull)
        self.assertIn("--no-approve", probe["argv"])
        chosen = self.root / "mcp.json"
        client = await self.started(settings(self.config, mcp_config=chosen))
        state = await client.request("get_state")
        self.assertEqual(state["probe"]["mcpConfig"], str(chosen.resolve()))

    async def test_startup_has_its_own_budget(self):
        slow = settings(self.config, rpc_timeout=0.2)
        client = await self.started(slow, "--slow-start", "0.6")
        self.assertIsNotNone(client.session_file)
        with self.assertRaises(RpcTimeout):
            await self.started(settings(slow, startup_timeout=0.3), "--slow-start", "0.6")

    # stderr: chunked draining and a redacted, bounded tail.

    async def test_stderr_is_drained_in_chunks_and_bounded(self):
        tools = settings(self.config, allowed_tools=("read",))
        client = await self.started(tools, "--stderr-flood")  # a 2 MiB line, then the policy line
        self.assertTrue(client._policy_ready.is_set())
        tail = client.stderr_tail()
        self.assertLessEqual(len(tail.encode()), 8192)
        self.assertTrue(tail.startswith("aelix-mattermost-policy-ready:"), tail[:80])

    async def test_stderr_tail_is_redacted(self):
        secrets = ("Authorization: Bearer abc.DEF-123 key=sk-proj-AbCdEf123456 "
                   "bot test-secret run 0123456789abcdef0123456789abcdef42 ghp_ABCDEFGHIJ0123456789")
        with patch.dict(os.environ, {"FAKE_STDERR": "provider failed: " + secrets}):
            client = await self.started()
        tail = client.stderr_tail()
        self.assertIn("provider failed", tail)
        self.assertIn("[redacted]", tail)
        for secret in ("abc.DEF-123", "sk-proj", "test-secret", "0123456789abcdef", "ghp_"):
            self.assertNotIn(secret, tail)
        self.assertEqual(self.client().stderr_tail(), "")


if __name__ == "__main__":
    unittest.main()
