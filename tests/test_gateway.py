import asyncio
import contextlib
import json
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from aiohttp import web

from aelix_mattermost.config import Config, ConfigError, load_config
from aelix_mattermost.gateway import Gateway
from aelix_mattermost.instance import instance_lock
from aelix_mattermost.mattermost import AuthenticationError, MattermostClient, split_message
from aelix_mattermost.routing import route_event
from aelix_mattermost.rpc import RpcError, RpcProcess, RpcTimeout
from aelix_mattermost.storage import Store, write_context

FAKE = Path(__file__).with_name("fake_aelix.py").resolve()


def event(post_id="p1", user="u1", channel="c1", kind="O", text="@aelix hello", root=""):
    return {"event": "posted", "data": {"channel_type": kind, "post": json.dumps({
        "id": post_id, "user_id": user, "channel_id": channel, "message": text,
        "root_id": root, "type": "", "delete_at": 0,
    })}}


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
        self.assertEqual(self.route(event(text="@AELIX: hello")).text, ": hello")

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
    def test_fail_closed_without_users_or_token_and_require_https(self):
        for changes in ({"allowed_users": ()}, {"token": ""}, {"allow_insecure_http": False}):
            with self.subTest(changes=changes), self.assertRaises(ConfigError):
                config(Path("/tmp"), **changes)

    def test_unknown_settings_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text('[mattermost]\nurl="https://chat.test"\nallowed_user=["u1"]\n')
            with self.assertRaises(ConfigError):
                load_config(path)

    def test_paths_resolve_against_configuration_and_token_is_not_repr(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"BOT_TEST_TOKEN": "secret"}):
            path = Path(directory) / "config.toml"
            path.write_text('[mattermost]\nurl="https://chat.test"\ntoken_env="BOT_TEST_TOKEN"\n'
                            'allowed_users=["u1"]\n[aelix]\nwork_dir="jobs"\n')
            value = load_config(path)
            self.assertEqual(value.work_dir, Path(directory) / "jobs")
            self.assertNotIn("secret", repr(value))

    def test_inbound_credentials_and_invalid_url_rejected(self):
        for url in ("https://user:password@chat.test", "https://chat.test?token=x", "file:///tmp"):
            with self.subTest(url=url), self.assertRaises(ConfigError):
                config(Path("/tmp"), url=url)

    def test_store_persists_dedup_and_marks_interrupted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
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
        with tempfile.TemporaryDirectory() as directory:
            with instance_lock(Path(directory)):
                with self.assertRaises(RuntimeError):
                    with instance_lock(Path(directory)):
                        pass
            with instance_lock(Path(directory)):
                pass


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
        self.assertFalse(client.alive)

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

    async def test_child_exit_and_malformed_json_fail_promptly(self):
        for text in ("__exit__", "__malformed__"):
            client = self.client()
            await client.start()
            with self.subTest(text=text), self.assertRaises(RpcError):
                await asyncio.wait_for(client.run(text), 2)

    async def test_required_tool_policy_handshake(self):
        client = self.client(replace(self.config, allowed_tools=("read",)))
        await client.start()
        self.assertTrue(client._policy_ready.is_set())
        bad = replace(self.config, allowed_tools=("read",), rpc_timeout=0.1,
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


class MattermostFixture:
    def __init__(self):
        self.posts = []
        self.patches = []
        self.connections = 0
        self.websocket_events = []
        self.auth_error = False
        self.patch_delay = 0

    async def start(self):
        app = web.Application()
        app.router.add_get("/api/v4/users/me", self.me)
        app.router.add_post("/api/v4/posts", self.post)
        app.router.add_put("/api/v4/posts/{id}/patch", self.patch)
        app.router.add_get("/api/v4/websocket", self.websocket)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"

    async def close(self):
        await self.runner.cleanup()

    def authorized(self, request):
        return request.headers.get("Authorization") == "Bearer test-secret"

    async def me(self, request):
        if not self.authorized(request):
            return web.json_response({}, status=401)
        return web.json_response({"id": "bot", "username": "aelix", "is_bot": True})

    async def post(self, request):
        if not self.authorized(request):
            return web.json_response({}, status=401)
        value = await request.json()
        self.posts.append(value)
        return web.json_response({"id": "reply-" + str(len(self.posts)), **value})

    async def patch(self, request):
        value = await request.json()
        if self.patch_delay:
            await asyncio.sleep(self.patch_delay)
        self.patches.append({"id": request.match_info["id"], **value})
        return web.json_response(value)

    async def websocket(self, request):
        websocket = web.WebSocketResponse()
        await websocket.prepare(request)
        challenge = await websocket.receive_json()
        assert challenge["action"] == "authentication_challenge"
        assert challenge["data"]["token"] == "test-secret"
        self.connections += 1
        await websocket.send_json({"event": "hello"})
        await websocket.send_json({"seq_reply": 1, "status": "FAIL" if self.auth_error else "OK"})
        for value in self.websocket_events:
            await websocket.send_json(value)
        await websocket.close()
        return websocket


class EndToEndTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.server = MattermostFixture()
        await self.server.start()
        self.config = config(self.root, url=self.server.url)
        self.client = await MattermostClient(self.config).__aenter__()
        self.store = Store(self.config.state_dir)
        self.gateway = Gateway(self.config, self.client, self.store, "bot", "aelix")

    async def asyncTearDown(self):
        await self.gateway.close()
        await self.client.__aexit__()
        self.store.close()
        await self.server.close()
        self.temp.cleanup()

    async def test_dm_and_group_mentions_produce_threaded_bot_replies(self):
        await self.gateway.handle(event(kind="D", text="DM question"))
        await self.gateway.handle(event(post_id="p2", kind="G", text="@aelix group question"))
        await self.gateway.drain()
        self.assertEqual(len(self.server.patches), 2)
        self.assertEqual({x["root_id"] for x in self.server.posts}, {"p1", "p2"})
        self.assertTrue(all(x["props"]["disable_mentions"] for x in self.server.posts + self.server.patches))

    async def test_duplicates_do_not_rerun_agent(self):
        await self.gateway.handle(event())
        await self.gateway.handle(event())
        await self.gateway.drain()
        self.assertEqual(len(self.server.posts), 1)
        self.assertEqual(len(self.server.patches), 1)

    async def test_same_session_is_serialized_and_users_are_isolated(self):
        await self.gateway.handle(event(root="r", text="@aelix __slow__ first"))
        await self.gateway.handle(event(post_id="p2", root="r", text="@aelix second"))
        await self.gateway.handle(event(post_id="p3", user="u2", root="r", text="@aelix other user"))
        await self.gateway.drain()
        answers = [x["message"] for x in self.server.patches]
        self.assertIn("turn 2: second", answers)
        self.assertIn("turn 1: other user", answers)

    async def test_restart_resumes_and_reset_starts_new_context(self):
        await self.gateway.handle(event(kind="D", text="one"))
        await self.gateway.drain()
        await self.gateway.close()
        self.gateway = Gateway(self.config, self.client, self.store, "bot", "aelix")
        await self.gateway.handle(event(post_id="p2", kind="D", text="two"))
        await self.gateway.drain()
        self.assertEqual(self.server.patches[-1]["message"], "turn 2: two")
        await self.gateway.handle(event(post_id="p3", kind="D", text="!reset"))
        await self.gateway.handle(event(post_id="p4", kind="D", text="fresh"))
        await self.gateway.drain()
        self.assertEqual(self.server.patches[-1]["message"], "turn 1: fresh")

    async def test_errors_do_not_disclose_provider_details(self):
        await self.gateway.handle(event(text="@aelix __error__"))
        await self.gateway.drain()
        self.assertNotIn("provider-secret", json.dumps(self.server.patches))
        self.assertIn("완료하지 못했습니다", self.server.patches[-1]["message"])

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
        self.assertEqual(self.server.patches[-1]["message"], "요청을 취소했습니다.")

    async def test_cancel_while_preparing_does_not_start_an_agent(self):
        preparing = asyncio.Event()

        async def waiting_post(*_args, **_kwargs):
            preparing.set()
            await asyncio.Future()

        with patch.object(self.client, "post", waiting_post):
            await self.gateway.handle(event(root="r"))
            await asyncio.wait_for(preparing.wait(), 2)
            # Error delivery has its own transport, so it remains observable while
            # the original placeholder request is deliberately suspended.
            replies = []

            async def reply(*args):
                replies.append(args[2])

            with patch.object(self.client, "reply", reply):
                await self.gateway.handle(event(post_id="p2", root="r", text="@aelix !cancel"))
                await asyncio.wait_for(self.gateway.drain(), 2)
        session = next(iter(self.gateway.sessions.values()))
        self.assertIsNone(session.rpc)
        self.assertEqual(replies, ["요청을 취소했습니다."])
        self.assertEqual(self.store.db.execute("SELECT status FROM posts WHERE id='p1'").fetchone()[0], "cancelled")

    async def test_queue_admission_is_bounded(self):
        self.gateway.config = replace(self.config, max_queue_per_session=1)
        for number in range(4):
            await self.gateway.handle(event(post_id=f"p{number}", root="r", text="@aelix __slow__ request"))
        await self.gateway.drain()
        self.assertEqual(len(self.server.patches), 2)
        self.assertEqual(sum("현재 요청이 많습니다" in x["message"] for x in self.server.posts), 2)

    async def test_error_reply_cannot_clear_the_next_shared_run_owner(self):
        self.gateway.config = replace(self.config, session_scope="thread")
        self.server.patch_delay = 0.05
        await self.gateway.handle(event(root="r", text="@aelix __error__"))
        await self.gateway.handle(event(post_id="p2", user="u2", root="r", text="@aelix __hang__"))
        for _ in range(200):
            session = next(iter(self.gateway.sessions.values()))
            if self.server.patches and session.owner == "u2" and session.rpc is not None and session.rpc.alive:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(session.owner, "u2")
        await self.gateway.handle(event(post_id="p3", user="u2", root="r", text="@aelix !cancel"))
        await self.gateway.drain()
        self.assertEqual(self.server.patches[-1]["message"], "요청을 취소했습니다.")

    async def test_websocket_authentication_and_reconnect_duplicate(self):
        self.server.websocket_events = [event()]
        iterator = self.client.events()
        first = await asyncio.wait_for(anext(iterator), 2)
        await self.gateway.handle(first)
        second = await asyncio.wait_for(anext(iterator), 3)
        await self.gateway.handle(second)
        await self.gateway.drain()
        await iterator.aclose()
        self.assertEqual(self.server.connections, 2)
        self.assertEqual(len(self.server.patches), 1)

    async def test_websocket_auth_failure_is_terminal(self):
        self.server.auth_error = True
        iterator = self.client.events()
        with self.assertRaises(AuthenticationError):
            await anext(iterator)
        await iterator.aclose()


if __name__ == "__main__":
    unittest.main()
