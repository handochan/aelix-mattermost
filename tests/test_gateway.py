import asyncio
import json
import os
import re
import sqlite3
import stat
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import aiohttp
# A double that follows the real server's REST and WebSocket rules; the WebSocket
# transport tests live in test_mattermost.py.
from mm_fixture import MattermostFixture

from aelix_mattermost.config import Config, ConfigError, load_config
from aelix_mattermost.gateway import (
    CANCELLED, DELIVERED, FAILED, PARTIAL, PREPARING, RESTARTED, STOPPED, TIMED_OUT, Gateway,
)
from aelix_mattermost.instance import instance_lock
from aelix_mattermost.mattermost import AuthenticationError, MattermostClient, MattermostError, split_message
from aelix_mattermost.routing import route_event
from aelix_mattermost.rpc import RpcError, RpcProcess, RpcTimeout
from aelix_mattermost.storage import Store, write_context

FAKE = Path(__file__).with_name("fake_aelix.py").resolve()
EXAMPLE = Path(__file__).resolve().parent.parent / "config.example.toml"
# Spelled out, not imported: every bot post and edit must carry exactly these (F3).
PROPS = {"unsafe_links": "true", "from_bot": "true"}


def event(post_id="p1", user="u1", channel="c1", kind="O", text="@aelix hello", root=""):
    return {"event": "posted", "data": {"channel_type": kind, "post": json.dumps({
        "id": post_id, "user_id": user, "channel_id": channel, "message": text,
        "root_id": root, "type": "", "delete_at": 0,
    })}}


def prompted(session, text):
    """Whether fake_aelix received `text` as a prompt; it appends prompts to its session file."""
    return any(text in path.read_text() for path in session.session_dir.glob("*.jsonl"))


def replies(server):
    """Messages of the bot's posts other than placeholders, in creation order."""
    return [x["message"] for x in server.posts if x["message"] != PREPARING]


def placeholder_ids(server):
    return [x["id"] for x in server.posts if x["message"] == PREPARING]


async def until(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition was not reached in time")
        await asyncio.sleep(0.01)


def config(directory: Path, **kwargs):
    return replace(Config(url="http://127.0.0.1:8065", token="test-secret", allowed_users=("u1", "u2"),
                          allow_insecure_http=True, state_dir=directory / "state",
                          work_dir=directory / "work", command=(sys.executable, str(FAKE)),
                          rpc_timeout=1, run_timeout=3), **kwargs).validate()


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = config(Path(self.temp.name))

    def route(self, value, configuration=None):
        return route_event(value, configuration or self.config, "bot", "aelix")

    def test_dm_needs_no_mention(self):
        request = self.route(event(kind="D", text="hello"))
        self.assertEqual(request.text, "hello")
        self.assertTrue(request.is_dm)

    def test_all_shared_channel_types_are_mention_gated(self):
        for kind in ("O", "P", "G"):
            with self.subTest(kind=kind):
                self.assertIsNone(self.route(event(kind=kind, text="hello")))
                self.assertEqual(self.route(event(kind=kind)).text, "hello")

    def test_mention_boundaries(self):
        for text in ("@aelix-other hello", "@aelix2 hello", "email@aelix hello", "@@aelix hello"):
            self.assertIsNone(self.route(event(text=text)))
        # The server strips the trailing ":" of "@aelix:", so it is part of the mention.
        self.assertEqual(self.route(event(text="@AELIX: hello")).text, "hello")

    def test_users_and_channel_allowlists(self):
        self.assertIsNone(self.route(event(user="stranger")))
        limited = replace(self.config, allowed_channels=("allowed",))
        self.assertIsNone(self.route(event(), limited))
        self.assertIsNotNone(self.route(event(kind="D", text="hello"), limited))

    def test_bot_system_malformed_deleted_unknown_events(self):
        self.assertIsNone(self.route(event(user="bot")))
        self.assertIsNone(self.route({"event": "typing"}))
        self.assertIsNone(self.route({"event": "posted", "data": {"post": "oops"}}))
        for field, value in (("type", "system_join_channel"), ("delete_at", 100)):
            item = event()
            post = json.loads(item["data"]["post"])
            post[field] = value
            item["data"]["post"] = json.dumps(post)
            self.assertIsNone(self.route(item))
        self.assertIsNone(self.route(event(kind="unknown")))

    def test_empty_and_oversized_prompts(self):
        self.assertIsNone(self.route(event(text="@aelix")))
        self.assertIsNone(self.route(event(text="@aelix " + "a" * 20001)))

    def test_user_sessions_isolate_users_threads_and_servers(self):
        first = self.route(event(root="thread"))
        follow = self.route(event(post_id="p2", root="thread"))
        other_user = self.route(event(user="u2", root="thread"))
        other_thread = self.route(event(root="other"))
        other_server = self.route(event(root="thread"), replace(self.config, url="http://other.test"))
        self.assertEqual(first.session_key, follow.session_key)
        self.assertEqual(len({first.session_key, other_user.session_key, other_thread.session_key,
                              other_server.session_key}), 4)

    def test_shared_thread_is_explicit(self):
        shared = replace(self.config, session_scope="thread")
        self.assertEqual(self.route(event(root="t"), shared).session_key,
                         self.route(event(root="t", user="u2"), shared).session_key)

    def test_dm_context_survives_new_top_level_messages(self):
        self.assertEqual(self.route(event(kind="D", text="one")).session_key,
                         self.route(event(kind="D", post_id="p2", text="two")).session_key)

    def test_unicode_chunking_preserves_text_and_length(self):
        text = "한국어🙂\n" * 1500
        chunks = split_message(text, 3500)
        self.assertEqual("".join(chunks), text)
        self.assertTrue(all(len(x) <= 3500 for x in chunks))

    def test_newline_at_exact_split_boundary(self):
        text = "a" * 100 + "\n" + "b" * 100
        chunks = split_message(text, 100)
        self.assertEqual("".join(chunks), text)
        self.assertTrue(all(len(x) <= 100 for x in chunks))


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def load(self, mattermost="", aelix="", gateway="", env=None):
        path = self.root / "config.toml"
        path.write_text('[mattermost]\nurl="https://chat.test"\nallowed_users=["u1"]\n' + mattermost
                        + "\n[aelix]\n" + aelix + "\n[gateway]\n" + gateway + "\n")
        with patch.dict(os.environ, {"MATTERMOST_TOKEN": "env-token", **(env or {})}):
            return load_config(path)

    def test_fail_closed_without_users_or_token_and_require_https(self):
        for changes in ({"allowed_users": ()}, {"token": ""}, {"allow_insecure_http": False}):
            with self.subTest(changes=changes), self.assertRaises(ConfigError):
                config(Path("/tmp"), **changes)

    def test_unknown_settings_are_rejected(self):
        path = self.root / "config.toml"
        path.write_text('[mattermost]\nurl="https://chat.test"\nallowed_user=["u1"]\n')
        with self.assertRaises(ConfigError):
            load_config(path)

    def test_paths_resolve_against_configuration_and_token_is_not_repr(self):
        with patch.dict(os.environ, {"BOT_TEST_TOKEN": "secret"}):
            path = self.root / "config.toml"
            path.write_text('[mattermost]\nurl="https://chat.test"\ntoken_env="BOT_TEST_TOKEN"\n'
                            'allowed_users=["u1"]\n[aelix]\nwork_dir="jobs"\n')
            value = load_config(path)
        # Configured paths are absolute and symlink-free (macOS: /var is /private/var).
        self.assertEqual(value.work_dir, self.root.resolve() / "jobs")
        self.assertNotIn("secret", repr(value))

    def test_inbound_credentials_and_invalid_url_rejected(self):
        for url in ("https://user:password@chat.test", "https://chat.test?token=x", "file:///tmp"):
            with self.subTest(url=url), self.assertRaises(ConfigError):
                config(Path("/tmp"), url=url)

    def test_token_file_takes_precedence_over_the_environment(self):
        (self.root / "secrets").mkdir()
        (self.root / "secrets" / "token").write_text("  file-token\r\n\n")
        value = self.load('token_file="secrets/token"\ntoken_env="BOT_TEST_TOKEN"', env={"BOT_TEST_TOKEN": "x"})
        self.assertEqual(value.token, "file-token")
        self.assertEqual(value.token_file, (self.root / "secrets" / "token").resolve())
        self.assertNotIn("file-token", repr(value))
        self.assertEqual(self.load().token, "env-token")
        self.assertIsNone(self.load().token_file)

    def test_token_file_must_exist_and_hold_a_token(self):
        (self.root / "empty").write_text(" \n\n")
        (self.root / "two-lines").write_text("token\nmore\n")
        for setting, message in (('token_file="missing"', "Cannot read"), ('token_file="empty"', "empty"),
                                 ('token_file="two-lines"', "valid bot token"), ("token_file=1", "path")):
            with self.subTest(setting=setting), self.assertRaisesRegex(ConfigError, message):
                self.load(setting)

    def test_mcp_config_is_resolved_and_must_exist(self):
        self.assertIsNone(self.load().mcp_config)
        (self.root / "mcp.json").write_text("{}")
        self.assertEqual(self.load(aelix='mcp_config="mcp.json"').mcp_config, (self.root / "mcp.json").resolve())
        with self.assertRaisesRegex(ConfigError, "mcp_config"):
            self.load(aelix='mcp_config="absent.json"')

    def test_process_limits_startup_timeout_and_stderr_logging(self):
        value = self.load()
        self.assertEqual((value.max_live_processes, value.startup_timeout, value.log_aelix_stderr), (8, 60, False))
        value = self.load(gateway="max_live_processes=4\nstartup_timeout=90.5\nlog_aelix_stderr=true")
        self.assertEqual((value.max_live_processes, value.startup_timeout, value.log_aelix_stderr), (4, 90.5, True))
        for setting in ("max_live_processes=2", "max_live_processes=0", "max_live_processes=4.5",
                        "max_live_processes=true", "startup_timeout=0", "startup_timeout=-1",
                        'startup_timeout="60"', 'log_aelix_stderr="yes"', "max_live_procs=4"):
            with self.subTest(setting=setting), self.assertRaises(ConfigError):
                self.load(gateway=setting)  # max_concurrent_runs defaults to 3

    def test_example_configuration_is_valid(self):
        with patch.dict(os.environ, {"MATTERMOST_TOKEN": "example-token"}):
            value = load_config(EXAMPLE)
        self.assertEqual(value.token, "example-token")
        self.assertEqual((value.max_live_processes, value.startup_timeout), (8, 60))

    def test_commented_example_settings_are_valid(self):
        lines = []
        for line in EXAMPLE.read_text(encoding="utf-8").splitlines():
            match = re.fullmatch(r"# (\w+) = (.+)", line)
            if match and match[2].startswith('"/'):  # a path: point it at a real file
                (self.root / match[1]).write_text("file-token\n")
                line = f'{match[1]} = "{self.root / match[1]}"'
            elif match:
                line = f"{match[1]} = {match[2]}"
            lines.append(line)
        path = self.root / "config.toml"
        path.write_text("\n".join(lines), encoding="utf-8")
        value = load_config(path)
        self.assertEqual(value.token, "file-token")
        self.assertEqual(value.mcp_config, (self.root / "mcp_config").resolve())
        self.assertEqual(value.model, "internal/my-model")

    def test_store_persists_dedup_and_marks_interrupted(self):
        path = self.root
        store = Store(path)
        self.assertTrue(store.claim("post"))
        self.assertFalse(store.claim("post"))
        store.save_session("session", path / "session.jsonl")
        store.close()
        store = Store(path)
        self.assertFalse(store.claim("post"))
        self.assertEqual(store.session_file("session"), path / "session.jsonl")
        self.assertEqual(store.db.execute("SELECT status FROM posts").fetchone()[0], "interrupted")
        store.reset_session("session")
        self.assertIsNone(store.session_file("session"))
        store.close()

    def test_single_instance_lock(self):
        with instance_lock(self.root):
            with self.assertRaises(RuntimeError):
                with instance_lock(self.root):
                    pass
        with instance_lock(self.root):
            pass


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_version_0_database_is_migrated_in_place(self):
        db = sqlite3.connect(self.root / "gateway.db")  # the 0.1.0 schema, user_version 0
        db.executescript("CREATE TABLE sessions (key TEXT PRIMARY KEY, file TEXT NOT NULL);"
                         "CREATE TABLE posts (id TEXT PRIMARY KEY, status TEXT NOT NULL, at REAL NOT NULL);")
        db.execute("INSERT INTO posts VALUES ('old', 'accepted', ?)", (time.time(),))
        db.execute("INSERT INTO sessions VALUES ('key', '/sessions/a.jsonl')")
        db.commit()
        db.close()
        store = Store(self.root)
        self.assertEqual(store.db.execute("PRAGMA user_version").fetchone()[0], 1)
        self.assertEqual(store.db.execute("SELECT status FROM posts WHERE id='old'").fetchone()[0], "interrupted")
        self.assertEqual(store.session_file("key"), Path("/sessions/a.jsonl"))
        self.assertFalse(store.claim("old"))
        self.assertTrue(store.claim("new"))
        store.set_placeholder("new", "holder")
        self.assertEqual(store.placeholders(), [("new", "holder")])
        store.close()
        store = Store(self.root)  # reopening a current database changes nothing
        self.assertEqual(store.placeholders(), [("new", "holder")])
        store.set_placeholder("new", None)
        self.assertEqual(store.placeholders(), [])
        store.close()

    def test_newer_schema_is_refused(self):
        store = Store(self.root)
        store.db.execute("PRAGMA user_version=99")
        store.close()
        with self.assertRaisesRegex(RuntimeError, "newer"):
            Store(self.root)

    def test_prune_forgets_post_ids_older_than_dedup_days(self):
        store = Store(self.root, dedup_days=1)
        store.claim("old")
        store.claim("new")
        store.db.execute("UPDATE posts SET at=? WHERE id='old'", (time.time() - 2 * 86400,))
        store.db.commit()
        self.assertEqual(store.prune(), 1)
        self.assertTrue(store.claim("old"))
        self.assertFalse(store.claim("new"))
        store.close()


class RpcTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = config(self.root)
        self.clients = []

    async def asyncTearDown(self):
        for client in self.clients:
            await client.close()

    def client(self, configuration=None, session_file=None):
        work, directory = self.root / "cwd", self.root / "sessions"
        work.mkdir(exist_ok=True)
        directory.mkdir(exist_ok=True)
        context = directory / "context.json"
        write_context(context, {"server": "server", "post_id": "post", "channel_id": "c",
                                "user_id": "u1", "root_id": "r"})
        client = RpcProcess(configuration or self.config, work, directory, context, session_file)
        self.clients.append(client)
        return client

    def settings(self, **values):
        """self.config plus fields an older Config may not define yet (startup_timeout)."""
        from types import SimpleNamespace
        try:
            return replace(self.config, **values)
        except TypeError:
            return SimpleNamespace(**{**vars(self.config), **values})

    async def test_prompt_and_persistent_resume(self):
        client = self.client()
        await client.start()
        self.assertEqual(await client.run("hello"), "turn 1: hello")
        self.assertEqual(await client.run("followup"), "turn 2: followup")
        saved = client.session_file
        await client.close()
        second = self.client(session_file=saved)
        await second.start()
        self.assertEqual(await second.run("resumed"), "turn 3: resumed")

    async def test_gateway_bot_token_is_removed_from_child_environment(self):
        with patch.dict(os.environ, {"MATTERMOST_TOKEN": "test-secret"}):
            state = await self.client().start()
        self.assertFalse(state["tokenVisible"])

    async def test_error_never_returns_stale_previous_answer(self):
        client = self.client()
        await client.start()
        await client.run("good")
        with self.assertRaises(RpcError):
            await client.run("__error__")
        # A finished error turn leaves the idle child (and its conversation) usable.
        self.assertTrue(client.alive)
        self.assertEqual(await client.run("next"), "turn 3: next")

    async def test_timeout_stops_the_child(self):
        client = self.client(replace(self.config, run_timeout=0.1))
        await client.start()
        with self.assertRaises(RpcTimeout):
            await client.run("__hang__")
        self.assertIsNotNone(client.process.returncode)

    async def test_close_during_spawn_cannot_leave_a_child_running(self):
        client = self.client()
        spawned = asyncio.Event()
        release = asyncio.Event()
        original = asyncio.create_subprocess_exec

        async def delayed_spawn(*args, **kwargs):
            process = await original(*args, **kwargs)
            spawned.set()
            await release.wait()
            return process

        with patch("aelix_mattermost.rpc.asyncio.create_subprocess_exec", delayed_spawn):
            starting = asyncio.create_task(client.start())
            await asyncio.wait_for(spawned.wait(), 2)
            closing = asyncio.create_task(client.close())
            await asyncio.sleep(0.01)
            release.set()
            await asyncio.wait_for(asyncio.gather(starting, closing, return_exceptions=True), 2)
        self.assertIsNotNone(client.process.returncode)

    async def test_child_exit_fails_promptly_and_stray_output_is_skipped(self):
        client = self.client()
        await client.start()
        with self.assertRaises(RpcError):
            await asyncio.wait_for(client.run("__exit__"), 2)
        # Like Aelix's own client, a stray non-JSON stdout line is skipped, not fatal.
        client = self.client()
        await client.start()
        self.assertEqual(await asyncio.wait_for(client.run("__malformed__"), 2), "turn 1: __malformed__")

    async def test_required_tool_policy_handshake(self):
        client = self.client(replace(self.config, allowed_tools=("read",)))
        await client.start()
        self.assertTrue(client._policy_ready.is_set())
        bad = self.settings(allowed_tools=("read",), rpc_timeout=0.1, startup_timeout=0.3,
                            command=(*self.config.command, "--missing-policy"))
        with self.assertRaises(RpcError):
            await self.client(bad).start()

    async def test_session_path_cannot_escape_assigned_directory(self):
        bad = replace(self.config, command=(*self.config.command, "--wrong-session"))
        with self.assertRaises(RpcError):
            await self.client(bad).start()

    async def test_rpc_failure_is_not_accepted(self):
        client = self.client()
        await client.start()
        with self.assertRaises(RpcError):
            await client.request("unsupported")

    async def test_final_output_is_bounded(self):
        client = self.client(replace(self.config, max_output_chars=20))
        await client.start()
        answer = await client.run("a" * 100)
        self.assertIn("응답 길이 제한", answer)




class EndToEndTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.server = MattermostFixture()
        await self.server.start()
        self.config = config(self.root, url=self.server.url)
        self.client = await MattermostClient(self.config).__aenter__()
        self.client.reconnect_delay = 0.1
        self.store = Store(self.config.state_dir)
        self.gateway = Gateway(self.config, self.client, self.store, "bot", "aelix")
        self.runs = []

    async def asyncTearDown(self):
        for run in self.runs:  # before the client, store and state directory go away
            await self.stop(run)
        await self.gateway.close()
        await self.client.__aexit__()
        self.store.close()
        await self.server.close()
        self.temp.cleanup()

    async def regateway(self, **changes):
        """Replace the gateway with one built from a changed configuration."""
        await self.gateway.close()
        self.config = config(self.root, url=self.server.url, **changes)
        self.gateway = Gateway(self.config, self.client, self.store, "bot", "aelix")

    def status(self, post_id):
        row = self.store.db.execute("SELECT status FROM posts WHERE id=?", (post_id,)).fetchone()
        return row[0] if row else None

    async def prompted_session(self, text):
        """The session whose Aelix child has received `text`."""
        await until(lambda: any(prompted(s, text) for s in self.gateway.sessions.values()))
        return next(s for s in self.gateway.sessions.values() if prompted(s, text))

    async def serve(self):
        """Run the gateway against the fixture's WebSocket until the test ends."""
        task = asyncio.create_task(self.gateway.run())
        self.runs.append(task)
        return task

    @staticmethod
    async def stop(task):
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    # Delivery (F11): answers and final notices are new thread posts; placeholders go away.

    async def test_dm_and_group_mentions_produce_threaded_bot_replies(self):
        await self.gateway.handle(event(kind="D", text="DM question"))
        await self.gateway.handle(event(post_id="p2", kind="G", text="@aelix group question"))
        await self.gateway.drain()
        self.assertEqual(sorted(replies(self.server)), ["turn 1: DM question", "turn 1: group question"])
        self.assertEqual({x["root_id"] for x in self.server.posts}, {"p1", "p2"})
        # New posts notify; an edited placeholder would not. The placeholders are deleted.
        self.assertEqual(sorted(x["id"] for x in self.server.deletes), sorted(placeholder_ids(self.server)))
        self.assertEqual(self.server.patches, [])
        self.assertEqual([x["props"] for x in self.server.posts], [PROPS] * 4)
        self.assertEqual(self.store.placeholders(), [])

    async def test_duplicates_do_not_rerun_agent(self):
        await self.gateway.handle(event())
        await self.gateway.handle(event())
        await self.gateway.drain()
        self.assertEqual(replies(self.server), ["turn 1: hello"])
        self.assertEqual(len(self.server.posts), 2)

    async def test_long_answers_are_new_posts_in_order(self):
        await self.regateway(max_post_chars=100)
        await self.gateway.handle(event(kind="D", text="x" * 250))
        await self.gateway.drain()
        chunks = replies(self.server)
        self.assertEqual("".join(chunks), "turn 1: " + "x" * 250)
        self.assertTrue(len(chunks) >= 3 and all(len(x) <= 100 for x in chunks))
        self.assertEqual([x["id"] for x in self.server.deletes], placeholder_ids(self.server))
        self.assertEqual(self.server.patches, [])
        self.assertEqual(self.status("p1"), "done")

    async def test_placeholder_that_cannot_be_deleted_points_to_the_answer(self):
        self.server.fail("DELETE", "posts/", status=400)
        await self.gateway.handle(event(kind="D", text="hello"))
        await self.gateway.drain()
        self.assertEqual(replies(self.server), ["turn 1: hello"])
        self.assertEqual([(x["id"], x["message"]) for x in self.server.patches],
                         [(placeholder_ids(self.server)[0], "응답을 아래 스레드에 게시했습니다.")])
        # A patch replaces every prop: both are resent, and the stored post keeps them.
        self.assertEqual(self.server.patches[0]["props"], PROPS)
        self.assertEqual(self.server.stored[placeholder_ids(self.server)[0]]["props"], PROPS)
        self.assertEqual(self.store.placeholders(), [])

    async def test_a_failed_chunk_never_touches_delivered_chunks(self):
        await self.regateway(max_post_chars=100)
        original, chunks = self.client.post, []

        async def post(channel_id, root_id, text):
            if text.startswith(("turn", "y")):
                chunks.append(text)
                if len(chunks) == 2:
                    raise MattermostError("Mattermost HTTP 500", 500)
            return await original(channel_id, root_id, text)

        with patch.object(self.client, "post", post):
            await self.gateway.handle(event(kind="D", text="y" * 250))
            await self.gateway.drain()
        self.assertEqual(replies(self.server), [chunks[0], PARTIAL])
        self.assertEqual(self.server.patches, [])
        self.assertEqual([x["id"] for x in self.server.deletes], placeholder_ids(self.server))
        self.assertEqual(self.status("p1"), "failed")

    async def test_notice_falls_back_to_the_placeholder_when_it_cannot_be_posted(self):
        original = self.client.post

        async def post(channel_id, root_id, text):
            if "완료하지 못했습니다" in text:
                raise MattermostError("Mattermost HTTP 503", 503)
            return await original(channel_id, root_id, text)

        with patch.object(self.client, "post", post):
            await self.gateway.handle(event(text="@aelix __error__"))
            await self.gateway.drain()
        self.assertEqual(replies(self.server), [])
        self.assertEqual([x["id"] for x in self.server.patches], placeholder_ids(self.server))
        self.assertIn("완료하지 못했습니다", self.server.patches[0]["message"])
        self.assertEqual(self.server.deletes, [])

    async def test_timeout_notice_is_a_new_post(self):
        await self.regateway(run_timeout=0.5)
        await self.gateway.handle(event(text="@aelix __hang__"))
        await self.gateway.drain()
        self.assertEqual(replies(self.server), ["실행 시간 제한을 초과하여 요청을 중단했습니다."])
        self.assertEqual([x["id"] for x in self.server.deletes], placeholder_ids(self.server))
        self.assertEqual(self.status("p1"), "failed")

    async def test_same_session_is_serialized_and_users_are_isolated(self):
        await self.gateway.handle(event(root="r", text="@aelix __slow__ first"))
        await self.gateway.handle(event(post_id="p2", root="r", text="@aelix second"))
        await self.gateway.handle(event(post_id="p3", user="u2", root="r", text="@aelix other user"))
        await self.gateway.drain()
        answers = replies(self.server)
        self.assertIn("turn 2: second", answers)
        self.assertIn("turn 1: other user", answers)

    async def test_restart_resumes_and_reset_starts_new_context(self):
        await self.gateway.handle(event(kind="D", text="one"))
        await self.gateway.drain()
        await self.gateway.close()
        self.gateway = Gateway(self.config, self.client, self.store, "bot", "aelix")
        await self.gateway.handle(event(post_id="p2", kind="D", text="two"))
        await self.gateway.drain()
        self.assertEqual(replies(self.server)[-1], "turn 2: two")
        await self.gateway.handle(event(post_id="p3", kind="D", text="!reset"))
        await self.gateway.handle(event(post_id="p4", kind="D", text="fresh"))
        await self.gateway.drain()
        self.assertEqual(replies(self.server)[-1], "turn 1: fresh")

    async def test_errors_do_not_disclose_provider_details(self):
        await self.gateway.handle(event(text="@aelix __error__"))
        await self.gateway.drain()
        self.assertNotIn("provider-secret", json.dumps(self.server.posts + self.server.patches))
        self.assertIn("완료하지 못했습니다", replies(self.server)[-1])
        self.assertEqual([x["id"] for x in self.server.deletes], placeholder_ids(self.server))

    async def test_only_run_owner_can_cancel_a_shared_thread(self):
        self.gateway.config = replace(self.config, session_scope="thread")
        await self.gateway.handle(event(root="r", text="@aelix __hang__"))
        for _ in range(100):
            sessions = list(self.gateway.sessions.values())
            if sessions and sessions[0].rpc is not None and sessions[0].rpc.alive:
                break
            await asyncio.sleep(0.01)
        await self.gateway.handle(event(post_id="p2", user="u2", root="r", text="@aelix !cancel"))
        self.assertIn("취소할 내 실행", self.server.posts[-1]["message"])
        await self.gateway.handle(event(post_id="p3", root="r", text="@aelix !cancel"))
        await self.gateway.drain()
        self.assertEqual(replies(self.server)[-1], "요청을 취소했습니다.")
        self.assertEqual([x["id"] for x in self.server.deletes], placeholder_ids(self.server))
        self.assertEqual(self.status("p1"), "cancelled")

    async def test_cancel_while_preparing_does_not_start_an_agent(self):
        preparing = asyncio.Event()
        original, posted = self.client.post, []

        async def post(channel_id, root_id, text):
            posted.append(text)
            if text == PREPARING:
                preparing.set()
                await asyncio.Future()  # the placeholder request never completes
            return await original(channel_id, root_id, text)

        with patch.object(self.client, "post", post):
            await self.gateway.handle(event(root="r"))
            await asyncio.wait_for(preparing.wait(), 2)
            await self.gateway.handle(event(post_id="p2", root="r", text="@aelix !cancel"))
            await asyncio.wait_for(self.gateway.drain(), 2)
        session = next(iter(self.gateway.sessions.values()))
        self.assertIsNone(session.rpc)
        self.assertEqual(posted, [PREPARING, "요청을 취소했습니다."])
        self.assertEqual(self.status("p1"), "cancelled")

    async def test_queue_admission_is_bounded(self):
        self.gateway.config = replace(self.config, max_queue_per_session=1)
        for number in range(4):
            await self.gateway.handle(event(post_id=f"p{number}", root="r", text="@aelix __slow__ request"))
        await self.gateway.drain()
        answers = replies(self.server)
        self.assertEqual(sum(x.startswith("turn") for x in answers), 2)
        self.assertEqual(sum("현재 요청이 많습니다" in x for x in answers), 2)

    async def test_error_reply_cannot_clear_the_next_shared_run_owner(self):
        self.gateway.config = replace(self.config, session_scope="thread")
        original = self.client.post

        async def slow_notice(channel_id, root_id, text):
            if "완료하지 못했습니다" in text:
                await asyncio.sleep(0.05)  # widen the window in which a stray reset could land
            return await original(channel_id, root_id, text)

        with patch.object(self.client, "post", slow_notice):
            await self.gateway.handle(event(root="r", text="@aelix __error__"))
            await self.gateway.handle(event(post_id="p2", user="u2", root="r", text="@aelix __hang__"))
            for _ in range(200):
                session = next(iter(self.gateway.sessions.values()))
                # The failed run's child stays alive, so a live child no longer shows that the
                # second request has its placeholder; its prompt reaching Aelix does.
                if session.owner == "u2" and prompted(session, "__hang__"):
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(session.owner, "u2")
            await self.gateway.handle(event(post_id="p3", user="u2", root="r", text="@aelix !cancel"))
            await self.gateway.drain()
        answers = replies(self.server)
        self.assertIn("완료하지 못했습니다", answers[0])
        self.assertEqual(answers[-1], "요청을 취소했습니다.")

    async def test_cancel_during_the_final_notice_cannot_cancel_it(self):
        await self.regateway(run_timeout=0.5)
        original, noticing = self.client.post, asyncio.Event()

        async def slow_notice(channel_id, root_id, text):
            if text == TIMED_OUT:
                noticing.set()
                await asyncio.sleep(0.3)  # a slow Mattermost; the requester gives up meanwhile
            return await original(channel_id, root_id, text)

        with patch.object(self.client, "post", slow_notice):
            await self.gateway.handle(event(kind="D", text="__hang__"))
            await asyncio.wait_for(noticing.wait(), 10)
            await self.gateway.handle(event(post_id="p2", kind="D", text="!cancel"))
            await self.gateway.drain()
        self.assertCountEqual(replies(self.server), ["이 대화에 취소할 내 실행 요청이 없습니다.", TIMED_OUT])
        self.assertEqual([x["id"] for x in self.server.deletes], placeholder_ids(self.server))
        self.assertEqual((self.status("p1"), self.store.placeholders()), ("failed", []))

    async def test_shutdown_after_an_earlier_cancel_is_not_a_cancel(self):
        await self.regateway(max_concurrent_runs=1, max_live_processes=1)
        await self.gateway.handle(event(post_id="x1", kind="D", channel="dmX", text="__hang__"))
        x = await self.prompted_session("__hang__")
        await self.gateway.handle(event(post_id="x2", kind="D", channel="dmX", text="!cancel"))
        await self.gateway.drain()
        await self.gateway.handle(event(post_id="y1", kind="D", channel="dmY", text="__hang__"))
        await until(lambda: any(s is not x and prompted(s, "__hang__") for s in self.gateway.sessions.values()))
        await self.gateway.handle(event(post_id="x3", kind="D", channel="dmX", text="next"))
        await until(lambda: x.lock.locked())  # x3 waits for the only run slot
        before = len(self.server.posts)
        await self.gateway.close()
        self.assertEqual(self.server.posts[before:], [])  # no "cancelled" notice for x3
        self.assertEqual((self.status("x1"), self.status("x3")), ("cancelled", "interrupted"))

    # Interrupted requests (F11): restart and shutdown notices on the placeholders.

    async def test_restart_reports_requests_the_previous_process_left_unfinished(self):
        kept = await self.client.post("c1", "p1", PREPARING)
        gone = await self.client.post("c1", "p2", PREPARING)
        await self.client.delete_post(gone["id"])  # deleted meanwhile: nobody is left to tell
        for post_id, holder in (("p1", kept), ("p2", gone)):
            self.store.claim(post_id)
            self.store.set_placeholder(post_id, holder["id"])
        self.store.close()
        self.store = Store(self.config.state_dir)  # the next process: accepted -> interrupted
        await self.regateway()
        await self.serve()
        await until(lambda: self.store.placeholders() == [])
        self.assertEqual([(x["id"], x["message"]) for x in self.server.patches], [(kept["id"], RESTARTED)])
        self.assertEqual(self.status("p1"), "interrupted")

    async def test_restart_edits_left_placeholders_to_their_outcome(self):
        # Requests that finished while Mattermost refused the placeholder edit.
        holders = {}
        for post_id, status in (("p1", "done"), ("p2", "failed"), ("p3", "cancelled"), ("p4", "accepted")):
            holders[post_id] = (await self.client.post("c1", post_id, PREPARING))["id"]
            self.store.claim(post_id)
            self.store.set_placeholder(post_id, holders[post_id])
            self.store.finish(post_id, status)
        self.store.close()
        self.store = Store(self.config.state_dir)
        await self.regateway()
        await self.serve()
        await until(lambda: self.store.placeholders() == [])
        self.assertEqual({x["id"]: x["message"] for x in self.server.patches},
                         {holders["p1"]: DELIVERED, holders["p2"]: FAILED, holders["p3"]: CANCELLED,
                          holders["p4"]: RESTARTED})

    def outage_in(self, method: str):
        """Make every post, delete and patch Gateway.<method> attempts fail with 503."""
        original = getattr(self.gateway, method)

        async def failing(*args, **kwargs):
            for verb in ("POST", "DELETE", "PUT"):
                self.server.fail(verb, "posts", status=503, times=3, headers={"Retry-After": "0"})
            try:
                await original(*args, **kwargs)
            finally:
                self.server.failures.clear()  # Mattermost is back

        return patch.object(self.gateway, method, failing)

    async def test_a_failure_notice_waits_out_a_mattermost_outage(self):
        with self.outage_in("_conclude"):  # neither the notice nor the placeholder edit works
            await self.gateway.handle(event(kind="D", text="__error__"))
            await self.gateway.drain()
        holder = placeholder_ids(self.server)[0]
        self.assertEqual(replies(self.server), [])
        self.assertEqual(self.store.placeholders(), [("p1", holder)])  # kept, not forgotten
        await self.gateway.close()
        self.assertEqual([(x["id"], x["message"]) for x in self.server.patches], [(holder, FAILED)])
        self.assertEqual(self.store.placeholders(), [])

    async def test_a_delivered_answer_keeps_its_placeholder_until_it_can_point_to_it(self):
        with self.outage_in("_retire"):  # the answer is posted; deleting or editing fails
            await self.gateway.handle(event(kind="D", text="hello"))
            await self.gateway.drain()
        holder = placeholder_ids(self.server)[0]
        self.assertEqual(replies(self.server), ["turn 1: hello"])
        self.assertEqual((self.status("p1"), self.store.placeholders()), ("done", [("p1", holder)]))
        await self.gateway.close()  # not "stopped, ask again": the answer is right there
        self.assertEqual([(x["id"], x["message"]) for x in self.server.patches], [(holder, DELIVERED)])

    async def test_restart_notice_waits_for_the_next_start_when_mattermost_is_failing(self):
        holder = await self.client.post("c1", "p1", PREPARING)
        self.store.claim("p1")
        self.store.set_placeholder("p1", holder["id"])
        await self.regateway()
        self.server.fail("PUT", "posts/", status=429, headers={"Retry-After": "0"}, times=3)
        await self.serve()
        await until(lambda: not self.server.failures)
        await asyncio.sleep(0.1)
        self.assertEqual(self.store.placeholders(), [("p1", holder["id"])])

    async def test_a_post_admitted_during_close_starts_nothing(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.gateway._get_session

        async def slow_get_session(key):
            entered.set()
            await release.wait()
            return await original(key)

        with patch.object(self.gateway, "_get_session", slow_get_session):
            handling = asyncio.create_task(self.gateway.handle(event(kind="D", text="late")))
            await entered.wait()
            closing = asyncio.create_task(self.gateway.close())
            await asyncio.sleep(0.05)
            release.set()
            await asyncio.gather(handling, closing)
        await asyncio.sleep(0.2)
        self.assertEqual(self.gateway._tasks, set())
        self.assertEqual(self.server.posts, [])
        self.assertEqual(self.status("p1"), "interrupted")

    async def test_shutdown_reports_running_requests_and_stops_their_children(self):
        await self.gateway.handle(event(text="@aelix __hang__"))
        session = await self.prompted_session("__hang__")
        await self.gateway.close()
        self.assertEqual([(x["id"], x["message"]) for x in self.server.patches],
                         [(placeholder_ids(self.server)[0], STOPPED)])
        self.assertEqual(self.store.placeholders(), [])
        self.assertEqual(self.status("p1"), "interrupted")
        self.assertIsNotNone(session.rpc.process.returncode)

    # Event intake (F7): a reader task feeds a bounded queue; one consumer handles in order.

    async def test_events_are_read_while_an_earlier_one_is_handled(self):
        release, handled = asyncio.Event(), []

        async def slow_handle(item):
            handled.append(json.loads(item["data"]["post"])["id"])
            await release.wait()

        self.server.websocket_events = [event(post_id="p1")]
        with patch.object(self.gateway, "handle", slow_handle):
            await self.serve()
            await until(lambda: handled)
            seen = self.client.last_event_at
            await self.server.push_event(event(post_id="p2"))
            # The reader takes p2 off the WebSocket although p1 is still being handled.
            await until(lambda: self.client.last_event_at != seen, timeout=3)
            release.set()
            await until(lambda: len(handled) == 2)
        self.assertEqual(handled, ["p1", "p2"])

    async def test_slow_handling_never_stalls_the_websocket(self):
        # aiohttp up to at least 3.10 drops a WebSocket whose PONG is not read within
        # heartbeat/2, so with a short heartbeat a reader that waits for handle() reconnects.
        connect = aiohttp.ClientSession.ws_connect

        def short_heartbeat(session, *args, **kwargs):
            return connect(session, *args, **{**kwargs, "heartbeat": 0.2})

        release, handled = asyncio.Event(), []

        async def slow_handle(item):
            handled.append(json.loads(item["data"]["post"])["id"])
            if len(handled) == 1:
                await release.wait()

        self.server.websocket_events = [event(post_id="p1"), event(post_id="p2")]
        with patch.object(aiohttp.ClientSession, "ws_connect", short_heartbeat), \
                patch.object(self.gateway, "handle", slow_handle):
            run = await self.serve()
            await until(lambda: handled)
            await asyncio.sleep(1.0)  # five heartbeats while the first event is still handled
            release.set()
            await until(lambda: len(handled) == 2)
            await asyncio.sleep(0.3)
            await self.stop(run)
        self.assertEqual(handled, ["p1", "p2"])
        self.assertEqual(self.server.connections, 1)

    async def test_a_failing_event_does_not_stop_the_gateway(self):
        handled = []

        async def flaky_handle(item):
            handled.append(json.loads(item["data"]["post"])["id"])
            if len(handled) == 1:
                raise RuntimeError("boom")

        self.server.websocket_events = [event(post_id="p1"), event(post_id="p2")]
        with patch.object(self.gateway, "handle", flaky_handle), \
                self.assertLogs("aelix_mattermost.gateway", "WARNING") as logs:
            run = await self.serve()
            await until(lambda: len(handled) == 2)
            self.assertFalse(run.done())
        self.assertIn("Could not handle a Mattermost event (RuntimeError)", "\n".join(logs.output))

    async def test_a_full_event_queue_drops_posts_with_a_warning(self):
        self.gateway.queue_size = 1
        release, handled = asyncio.Event(), []

        async def slow_handle(item):
            handled.append(json.loads(item["data"]["post"])["id"])
            await release.wait()

        self.server.websocket_events = [event(post_id="p0")]
        with patch.object(self.gateway, "handle", slow_handle), \
                self.assertLogs("aelix_mattermost.gateway", "WARNING") as logs:
            await self.serve()
            await until(lambda: handled)  # the consumer is busy with p0
            for number in (1, 2, 3):
                await self.server.push_event(event(post_id=f"p{number}"))
            await until(lambda: any("Event queue is full" in x for x in logs.output))
            release.set()
            await until(lambda: len(handled) == 2)
            await asyncio.sleep(0.2)
        self.assertEqual(handled, ["p0", "p1"])

    async def test_rejected_token_ends_run(self):
        self.server.auth_error = True
        with self.assertRaises(AuthenticationError):
            await asyncio.wait_for(self.gateway.run(), 10)

    async def test_websocket_posts_are_answered(self):
        await self.serve()
        await until(lambda: self.client.connected)
        await self.server.push_event(event(kind="D", text="over the socket"))
        await until(lambda: "turn 1: over the socket" in replies(self.server))

    # Diagnosability (F8).

    async def test_failures_are_logged_with_short_ids_and_bounded_reasons(self):
        with self.assertLogs("aelix_mattermost.gateway", "WARNING") as logs:
            await self.gateway.handle(event(post_id="p1abcdefghij", text="@aelix __error__"))
            await self.gateway.drain()
        key = next(iter(self.gateway.sessions))
        failures = [x for x in logs.output if "Request failed" in x]
        self.assertEqual(len(failures), 1)
        self.assertIn(f"Request failed post=p1abcdef session={key[:8]} (RpcRunFailed: ", failures[0])
        self.assertNotIn("provider-secret", "\n".join(logs.output))
        self.assertFalse(any("Aelix stderr" in x for x in logs.output))

    async def test_aelix_stderr_is_not_logged_by_default(self):
        self.assertFalse(self.config.log_aelix_stderr)
        noise = "provider said: quota exceeded for this key"
        with patch.dict(os.environ, {"FAKE_STDERR": noise}), \
                self.assertLogs("aelix_mattermost", "DEBUG") as logs:
            await self.gateway.handle(event(text="@aelix __error__"))
            await self.gateway.drain()
        self.assertTrue(any("Request failed post=p1 session=" in x for x in logs.output))
        self.assertEqual([x for x in logs.output if "Aelix stderr" in x or "quota exceeded" in x], [])

    async def test_aelix_stderr_tail_is_logged_only_when_enabled(self):
        await self.regateway(log_aelix_stderr=True)
        noise = "provider said: Authorization: Bearer sk-live-abcdefgh12345678"
        with patch.dict(os.environ, {"FAKE_STDERR": noise}), \
                self.assertLogs("aelix_mattermost.gateway", "WARNING") as logs:
            await self.gateway.handle(event(text="@aelix __error__"))
            await self.gateway.drain()
        tails = [x for x in logs.output if "Aelix stderr post=p1 session=" in x]
        self.assertEqual(len(tails), 1)
        self.assertIn("provider said: Authorization: Bearer [redacted]", tails[0])
        self.assertNotIn("sk-live", "\n".join(logs.output))

    # Process limits (F13) and shutdown (F20).

    async def test_live_children_are_capped_by_closing_the_least_recently_used(self):
        await self.regateway(max_live_processes=2, max_concurrent_runs=1)
        for number in range(3):
            await self.gateway.handle(event(post_id=f"p{number}", kind="D", channel=f"dm{number}", text="hi"))
            await self.gateway.drain()
        sessions = list(self.gateway.sessions.values())
        self.assertEqual([s.rpc is not None and s.rpc.alive for s in sessions], [False, True, True])
        await self.gateway.handle(event(post_id="p3", kind="D", channel="dm0", text="again"))
        await self.gateway.drain()
        self.assertEqual(replies(self.server)[-1], "turn 2: again")  # resumed from its transcript
        self.assertEqual([s.rpc is not None and s.rpc.alive for s in sessions], [True, False, True])

    async def test_children_of_busy_sessions_are_never_closed(self):
        await self.regateway(max_live_processes=2, max_concurrent_runs=2)
        await self.gateway.handle(event(post_id="p1", kind="D", channel="dm1", text="__hang__"))
        busy = await self.prompted_session("__hang__")
        await self.gateway.handle(event(post_id="p2", kind="D", channel="dm2", text="idle"))
        await until(lambda: len(self.gateway._tasks) == 1)
        await self.gateway.handle(event(post_id="p3", kind="D", channel="dm3", text="third"))
        await until(lambda: len(self.gateway._tasks) == 1)
        sessions = list(self.gateway.sessions.values())
        self.assertEqual([s.rpc is not None and s.rpc.alive for s in sessions], [True, False, True])
        self.assertIs(sessions[0], busy)
        self.assertIn("turn 1: third", replies(self.server))
        await self.gateway.handle(event(post_id="p4", kind="D", channel="dm1", text="!cancel"))
        await self.gateway.drain()

    def processes(self):
        """Aelix child processes that have not exited, evicted ones still closing included."""
        return sum(rpc.process is not None and rpc.process.returncode is None
                   for s in self.gateway.sessions.values() for rpc in (s.rpc, getattr(s, "retired", None))
                   if rpc is not None)

    async def test_requests_waiting_for_a_run_slot_keep_no_child(self):
        await self.regateway(max_live_processes=2, max_concurrent_runs=2)
        peak, sampling = 0, True

        async def sample():
            nonlocal peak
            while sampling:
                peak = max(peak, self.processes())
                await asyncio.sleep(0.005)

        sampler = asyncio.create_task(sample())
        await self.gateway.handle(event(post_id="a1", kind="D", channel="dmA", text="__hang__"))
        await self.gateway.handle(event(post_id="b1", kind="D", channel="dmB", text="first"))
        await self.gateway.handle(event(post_id="b2", kind="D", channel="dmB", text="second"))
        await self.gateway.handle(event(post_id="c1", kind="D", channel="dmC", text="third"))
        # c1 gets b1's run slot while b2 waits for one: b's idle child makes room for c's.
        await until(lambda: "turn 1: third" in replies(self.server))
        await until(lambda: "turn 2: second" in replies(self.server))  # resumed from its transcript
        await self.gateway.handle(event(post_id="a2", kind="D", channel="dmA", text="!cancel"))
        await self.gateway.drain()
        sampling = False
        await sampler
        self.assertEqual(peak, 2)
        self.assertEqual([self.status(x) for x in ("b1", "b2", "c1", "a1")], ["done"] * 3 + ["cancelled"])

    async def test_an_evicted_child_exits_before_its_session_starts_another(self):
        # Aelix (and fake_aelix) refuse to open a transcript that a live process still owns.
        await self.regateway(max_live_processes=2, max_concurrent_runs=2)
        original = RpcProcess.close

        async def slow_eviction(rpc):
            if any(getattr(s, "retired", None) is rpc for s in self.gateway.sessions.values()):
                await asyncio.sleep(1.0)  # an evicted child that takes its time to exit
            await original(rpc)

        with patch.object(RpcProcess, "close", slow_eviction):
            await self.gateway.handle(event(post_id="a1", kind="D", channel="dmA", text="__hang__"))
            await self.gateway.handle(event(post_id="b1", kind="D", channel="dmB", text="first"))
            await self.gateway.handle(event(post_id="b2", kind="D", channel="dmB", text="second"))
            await self.gateway.handle(event(post_id="c1", kind="D", channel="dmC", text="third"))
            await until(lambda: any(getattr(s, "retired", None) for s in self.gateway.sessions.values()))
            # Free a run slot for b2 while b's evicted child is still exiting.
            await self.gateway.handle(event(post_id="a2", kind="D", channel="dmA", text="!cancel"))
            await self.gateway.drain()
        self.assertIn("turn 2: second", replies(self.server))
        self.assertEqual([self.status(x) for x in ("b2", "c1")], ["done", "done"])

    async def test_a_child_is_evicted_only_once_aelix_has_finished_compacting(self):
        await self.regateway(max_live_processes=3, max_concurrent_runs=2)
        compacted = self.root / "compacted"  # fake_aelix compacts until this file exists
        with patch.dict(os.environ, {"FAKE_COMPACTION_UNTIL": str(compacted)}):
            await self.gateway.handle(event(post_id="a1", kind="D", channel="dmA", text="__hang__"))
            await self.gateway.handle(event(post_id="b1", kind="D", channel="dmB", text="__compact__ b"))
            await self.gateway.handle(event(post_id="d1", kind="D", channel="dmD", text="delta question"))
            await self.gateway.handle(event(post_id="c1", kind="D", channel="dmC", text="charlie question"))
            await until(lambda: "turn 1: charlie question" in replies(self.server))
        compacting = await self.prompted_session("__compact__ b")
        newer = await self.prompted_session("delta question")
        # b's child is the least recently used, but Aelix is still compacting its transcript.
        self.assertTrue(compacting.rpc is not None and compacting.rpc.alive)
        self.assertIsNone(newer.rpc)
        child = compacting.rpc
        compacted.touch()
        while (await child.request("get_state")).get("isStreaming"):
            await asyncio.sleep(0.05)
        await self.gateway.handle(event(post_id="e1", kind="D", channel="dmE", text="echo question"))
        await until(lambda: "turn 1: echo question" in replies(self.server))
        self.assertIsNone(compacting.rpc)  # compaction done: now it is the one to stop
        await self.gateway.handle(event(post_id="a2", kind="D", channel="dmA", text="!cancel"))
        await self.gateway.drain()

    async def test_close_stops_children_concurrently(self):
        for number in range(3):
            await self.gateway.handle(event(post_id=f"p{number}", kind="D", channel=f"dm{number}", text="hi"))
        await self.gateway.drain()
        children = [s.rpc for s in self.gateway.sessions.values()]
        original = RpcProcess.close

        async def slow_close(rpc):
            await asyncio.sleep(1.0)
            await original(rpc)

        with patch.object(RpcProcess, "close", slow_close):
            started = time.monotonic()
            await self.gateway.close()
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 2.5)  # one after another would take at least 3 s
        self.assertTrue(all(rpc.process.returncode is not None for rpc in children))

    # Stored transcripts (F14).

    async def test_unusable_transcript_mapping_starts_a_new_conversation(self):
        await self.gateway.handle(event(kind="D", text="one"))
        await self.gateway.drain()
        key = next(iter(self.gateway.sessions))
        await self.regateway()
        self.store.save_session(key, self.root / "elsewhere" / "missing.jsonl")
        with self.assertLogs("aelix_mattermost.gateway", "WARNING") as logs:
            await self.gateway.handle(event(post_id="p2", kind="D", text="two"))
            await self.gateway.drain()
        self.assertEqual(replies(self.server)[-1], "turn 1: two")
        self.assertIn("Dropped an unusable stored transcript", "\n".join(logs.output))
        session = self.gateway.sessions[key]
        self.assertTrue(self.store.session_file(key).is_relative_to(session.session_dir))

    async def test_moved_state_directory_resumes_the_transcript_by_name(self):
        await self.gateway.handle(event(kind="D", text="one"))
        await self.gateway.drain()
        key = next(iter(self.gateway.sessions))
        stored = self.store.session_file(key)
        await self.regateway()
        self.store.save_session(key, Path("/old/state/sessions") / key / stored.name)
        await self.gateway.handle(event(post_id="p2", kind="D", text="two"))
        await self.gateway.drain()
        self.assertEqual(replies(self.server)[-1], "turn 2: two")
        self.assertEqual(self.store.session_file(key), stored)

    # Background upkeep: dedup pruning (F19) and health.json (F26).

    async def test_old_post_ids_are_pruned_while_running(self):
        self.store.claim("ancient")
        self.store.db.execute("UPDATE posts SET at=0 WHERE id='ancient'")
        self.store.db.commit()
        self.gateway.config = replace(self.config, idle_timeout=0.05)
        self.gateway.prune_interval = 0.1
        await self.serve()
        await until(lambda: self.status("ancient") is None)

    async def test_health_file_reports_the_websocket(self):
        self.gateway.health_interval = 0.05
        path = self.config.state_dir / "health.json"
        run = await self.serve()
        await until(lambda: path.exists() and json.loads(path.read_text())["websocket_connected"])
        data = json.loads(path.read_text())
        await self.stop(run)
        self.assertEqual(set(data), {"version", "pid", "updated_at", "websocket_connected",
                                     "connected_since", "last_event_at"})
        self.assertEqual((data["version"], data["pid"]), (1, os.getpid()))
        self.assertLess(abs(time.time() - data["updated_at"]), 5)
        self.assertIsInstance(data["connected_since"], float)
        self.assertIsInstance(data["last_event_at"], float)
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        await self.gateway.close()
        closed = json.loads(path.read_text())
        self.assertFalse(closed["websocket_connected"])
        self.assertIsNone(closed["connected_since"])
        self.assertEqual(list(path.parent.glob(".health.json.*")), [])


if __name__ == "__main__":
    unittest.main()
